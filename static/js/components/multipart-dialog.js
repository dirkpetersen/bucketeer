import { h } from "../dom.js";
import { icon } from "../icons.js";
import { listMultipartUploads, abortMultipartUploads } from "../api.js";
import { toast } from "./toast.js";
import { confirmDialog, nameSpan } from "./confirm-dialog.js";

// multipartDialog({ bucket }) -> Promise<{ error? } | { changed }>
// Lists the bucket's unfinished (multipart) uploads, one page at a time, and aborts the ones the
// user picks, or every one older than N days. Advanced only (the server does not care about the
// toggle). This is not a file browser: it shows unfinished uploads and nothing else.
let seq = 0;
const PAGE = 50;
const MAX_ROUNDS = 20; // "abort all older than" repeats while the server says more remain
const idOf = (u) => `${u.key}\u0000${u.uploadId}`;
const plural = (n, one, many) => `${n.toLocaleString()} ${n === 1 ? one : many}`;

function ago(iso) {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "unknown";
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (s < 90) return "just now";
  const units = [[60, "minute"], [3600, "hour"], [86400, "day"], [86400 * 30, "month"], [86400 * 365, "year"]];
  let best = ["minute", 60];
  for (const [secs, name] of units) if (s >= secs) best = [name, secs];
  const n = Math.round(s / best[1]);
  return `${n} ${best[0]}${n === 1 ? "" : "s"} ago`;
}

const exact = (iso) => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
};

export async function multipartDialog({ bucket }) {
  const id = `mpu${++seq}`;
  let uploads = [];
  let next = null; // {keyMarker, uploadIdMarker} while more pages exist
  let prefix = "";
  let busy = false;
  let working = null; // which abort button shows the spinner: "sel" | "old"
  let loaded = false;
  let changed = false;
  const selected = new Set();
  let rowSeq = 0;

  const error = h("p", { class: "qd-error", role: "alert", hidden: true });
  const showError = (m) => { error.textContent = m; error.hidden = !m; };
  const status = h("p", { class: "mp-status", role: "status", "aria-live": "polite", hidden: true });
  const say = (m) => { status.textContent = m; status.hidden = !m; };

  const filter = h("input", { class: "input mono-input", id: `${id}-p`, type: "text", placeholder: "Only keys starting with…", autocomplete: "off", autocapitalize: "off", spellcheck: false, maxlength: 400, "aria-label": "Filter unfinished uploads by key prefix" });
  const find = h("button", { class: "btn", type: "submit" }, icon("search"), "Filter");
  const reload = h("button", { class: "btn btn-ghost btn-icon", type: "button", "aria-label": "Refresh the list", title: "Refresh" }, icon("refresh"));
  const filterForm = h("form", { class: "mp-filter", role: "search" }, filter, find, reload);

  const list = h("ul", { class: "mp-list" });
  const all = h("input", { type: "checkbox", id: `${id}-all`, "aria-label": "Select all uploads shown" });
  const allLabel = h("label", { class: "mp-all", for: `${id}-all` }, all, h("span", { text: "Select all shown" }));
  const shown = h("span", { class: "chip" });
  const head = h("div", { class: "mp-head" }, allLabel, shown);
  const more = h("button", { class: "btn btn-sm", type: "button", text: "Load more" });
  const moreRow = h("div", { class: "mp-more", hidden: true }, more);
  const loadingRow = h("div", { class: "skel mp-skel", "aria-hidden": "true" }, h("div", { class: "skel-line w60" }), h("div", { class: "skel-line w40" }), h("div", { class: "skel-line w60" }));
  const empty = h("div", { class: "mp-empty", hidden: true },
    icon("check", "icon mp-empty-ic"),
    h("b", { class: "mp-empty-t" }),
    h("p", { class: "mp-empty-m" }));

  const abortSel = h("button", { class: "btn btn-danger", type: "button" }, icon("trash"), "Abort selected");
  const days = h("input", { class: "input mp-days", id: `${id}-d`, type: "number", min: 1, max: 36500, step: 1, value: 7, inputmode: "numeric", "aria-label": "Days" });
  const abortOld = h("button", { class: "btn", type: "button", text: "Abort all older…" });
  const olderRow = h("div", { class: "mp-older" },
    h("label", { for: `${id}-d`, text: "Abort every unfinished upload older than" }), days, h("span", { text: "days" }), abortOld);
  const close = h("button", { class: "btn", type: "button", text: "Close" });
  const hint = h("p", { class: "mp-hint" }, icon("refresh", "icon"),
    h("span", {}, "To clean up automatically, use the ", h("b", { text: "Lifecycle" }), " rule “abort incomplete multipart uploads” in this bucket’s More menu."));

  function update() {
    const n = selected.size;
    const spin = h("span", { class: "spinner", "aria-hidden": "true" });
    abortSel.replaceChildren(working === "sel" ? spin : icon("trash"), working === "sel" ? "Aborting…" : n ? `Abort ${plural(n, "upload", "uploads")}` : "Abort selected");
    abortOld.replaceChildren(...(working === "old" ? [h("span", { class: "spinner", "aria-hidden": "true" }), "Aborting…"] : ["Abort all older…"]));
    abortSel.disabled = busy || n === 0;
    abortOld.disabled = busy || !loaded;
    days.disabled = busy;
    find.disabled = busy;
    reload.disabled = busy;
    more.disabled = busy;
    all.checked = uploads.length > 0 && uploads.every((u) => selected.has(idOf(u)));
    all.indeterminate = !all.checked && n > 0;
    all.disabled = busy || !uploads.length;
    head.hidden = !uploads.length;
    shown.textContent = `${uploads.length.toLocaleString()} shown${next ? ", more available" : ""}`;
    moreRow.hidden = !next;
    list.hidden = !uploads.length;
    loadingRow.hidden = loaded;
    empty.hidden = !loaded || uploads.length > 0;
    if (loaded && !uploads.length) {
      empty.querySelector(".mp-empty-t").textContent = prefix ? "No unfinished uploads with that prefix" : "No unfinished uploads";
      empty.querySelector(".mp-empty-m").textContent = prefix
        ? "Clear the filter to see every unfinished upload in this bucket."
        : "An unfinished upload is a large file transfer that was started but never completed or cancelled. Its uploaded pieces stay in the bucket, use storage and count against your quota until they are aborted. Nothing is wasted here.";
    }
  }

  function item(u) {
    const key = idOf(u);
    const cb = h("input", { type: "checkbox", id: `${id}-c${++rowSeq}`, checked: selected.has(key), "aria-label": `Select ${u.key}` });
    cb.addEventListener("change", () => { if (cb.checked) selected.add(key); else selected.delete(key); update(); });
    const meta = h("span", { class: "mp-meta" },
      h("time", { datetime: u.initiated || "", title: exact(u.initiated), text: `Started ${ago(u.initiated)}` }),
      exact(u.initiated) ? h("span", { class: "mp-exact", text: exact(u.initiated) }) : null,
      u.storageClass && u.storageClass !== "STANDARD" ? h("span", { class: "chip", text: u.storageClass }) : null);
    return h("li", { class: "mp-item" },
      h("label", { class: "mp-pick", for: cb.id }, cb,
        h("span", { class: "mp-main" }, h("code", { class: "mp-key", text: u.key }), meta)));
  }

  function render() {
    list.replaceChildren(...uploads.map(item));
    update();
  }

  async function load({ append = false } = {}) {
    busy = true;
    showError("");
    update();
    try {
      const res = await listMultipartUploads(bucket, {
        prefix, limit: PAGE,
        keyMarker: append && next ? next.keyMarker : "",
        uploadIdMarker: append && next ? next.uploadIdMarker : "",
      });
      uploads = append ? uploads.concat(res.uploads) : res.uploads;
      next = res.truncated && res.nextKeyMarker ? { keyMarker: res.nextKeyMarker, uploadIdMarker: res.nextUploadIdMarker || "" } : null;
      if (!append) selected.clear();
      loaded = true;
    } catch (err) {
      if (err.status === 401) { dlg.close(); return; }
      loaded = true;
      showError(err.message || "Could not list the unfinished uploads.");
    } finally {
      busy = false;
      render();
    }
  }

  // Remove finished rows from the list without another round trip.
  function drop(refs) {
    const gone = new Set(refs.map(idOf));
    uploads = uploads.filter((u) => !gone.has(idOf(u)));
    for (const g of gone) selected.delete(g);
  }

  function summary(t) {
    const parts = [];
    if (t.aborted) parts.push(`aborted ${plural(t.aborted, "upload", "uploads")}`);
    if (t.notFound) parts.push(`${plural(t.notFound, "was", "were")} already gone`);
    if (t.failed) parts.push(`${t.failed} failed`);
    return parts.length ? `${parts[0][0].toUpperCase()}${parts[0].slice(1)}${parts.slice(1).map((p) => `, ${p}`).join("")}.` : "Nothing to abort.";
  }

  async function run(fn, which) {
    busy = true;
    showError("");
    say("");
    working = which;
    update();
    try { return await fn(); } finally { busy = false; working = null; }
  }

  abortSel.addEventListener("click", async () => {
    const picked = uploads.filter((u) => selected.has(idOf(u))).map((u) => ({ key: u.key, uploadId: u.uploadId }));
    if (!picked.length || busy) return;
    const ok = await confirmDialog({
      title: "Abort the selected uploads?",
      message: `${plural(picked.length, "unfinished upload", "unfinished uploads")} will be cancelled and the pieces already uploaded are deleted. A tool that is still uploading to one of them will fail. This cannot be undone.`,
      confirmLabel: `Abort ${plural(picked.length, "upload", "uploads")}`, danger: true,
    });
    if (!ok) return;
    const totals = { aborted: 0, notFound: 0, failed: 0 };
    const failures = [];
    try {
      await run(async () => {
        for (let i = 0; i < picked.length; i += 100) {
          const res = await abortMultipartUploads(bucket, { uploads: picked.slice(i, i + 100) });
          totals.aborted += res.aborted.length;
          totals.notFound += res.notFound.length;
          totals.failed += res.failed.length;
          failures.push(...res.failed);
          drop([...res.aborted, ...res.notFound]);
          if (res.aborted.length) changed = true;
          render();
        }
      }, "sel");
    } catch (err) {
      if (err.status === 401) { dlg.close(); return; }
      showError(err.message || "Could not abort the uploads.");
    }
    say(summary(totals));
    if (failures.length) showError(`Could not abort ${failures.length} upload${failures.length === 1 ? "" : "s"} (${[...new Set(failures.map((f) => f.error))].join(", ")}). They are still selected; try again.`);
    update();
  });

  abortOld.addEventListener("click", async () => {
    const n = Number(days.value);
    if (!Number.isInteger(n) || n < 1 || n > 36500) { showError("Enter a whole number of days from 1 to 36500."); days.focus(); return; }
    showError("");
    say("");
    const body = { olderThanDays: n, ...(prefix ? { prefix } : {}) };
    let count;
    try {
      busy = true; update();
      try {
        await abortMultipartUploads(bucket, body);
        count = { count: 0 }; // cannot happen: no confirm was sent
      } catch (err) {
        if (err.status === 409 && err.body?.requiresConfirmation) count = err.body;
        else throw err;
      } finally { busy = false; update(); }
    } catch (err) {
      if (err.status === 401) { dlg.close(); return; }
      showError(err.message || "Could not check the uploads.");
      return;
    }
    if (!count.count && count.truncated) { say(`The first uploads scanned were all newer than ${plural(n, "day", "days")}, but the bucket has more, and those may include older ones. Narrow the list with a prefix filter and try again.`); return; }
    if (!count.count) { say(`No unfinished uploads are older than ${plural(n, "day", "days")}${prefix ? " with that prefix" : ""}.`); return; }
    const many = `${count.count.toLocaleString()}${count.truncated ? " or more" : ""}`;
    const ok = await confirmDialog({
      title: `Abort ${many} old uploads?`,
      message: `Every unfinished upload started more than ${plural(n, "day", "days")} ago${prefix ? ` with keys starting with “${prefix}”` : ""} will be cancelled and its uploaded pieces deleted. This cannot be undone.`,
      confirmLabel: `Abort ${many} uploads`, danger: true,
    });
    if (!ok) return;
    const totals = { aborted: 0, notFound: 0, failed: 0 };
    try {
      await run(async () => {
        for (let round = 1; round <= MAX_ROUNDS; round++) {
          const res = await abortMultipartUploads(bucket, { ...body, confirm: true });
          totals.aborted += res.aborted.length;
          totals.notFound += res.notFound.length;
          totals.failed += res.failed.length;
          if (res.aborted.length) changed = true;
          say(`Aborted ${totals.aborted.toLocaleString()} so far…`);
          // Stop when done, or when a round made no progress (everything left fails).
          if (!res.truncated || !res.aborted.length) break;
        }
      }, "old");
    } catch (err) {
      if (err.status === 401) { dlg.close(); return; }
      showError(err.message || "Could not abort the uploads.");
    }
    say(summary(totals));
    if (totals.failed) showError(`${totals.failed} upload${totals.failed === 1 ? "" : "s"} could not be aborted. Try again, or ask an administrator if it keeps failing.`);
    await load();
  });

  filterForm.addEventListener("submit", (e) => { e.preventDefault(); prefix = filter.value; say(""); load(); });
  reload.addEventListener("click", () => { say(""); load(); });
  more.addEventListener("click", () => load({ append: true }));
  all.addEventListener("change", () => {
    for (const u of uploads) { if (all.checked) selected.add(idOf(u)); else selected.delete(idOf(u)); }
    render();
  });

  const dlg = h("dialog", { class: "cd qd pol mp", "aria-labelledby": `${id}-t`, "aria-describedby": `${id}-m` },
    h("div", { class: "pol-body" },
      h("div", { class: "qd-head" },
        h("h2", { id: `${id}-t` }, "Unfinished uploads in ", nameSpan(bucket)),
        h("p", { class: "cd-msg", id: `${id}-m`, text: "Large uploads are sent in pieces. If a transfer is interrupted, the pieces stay behind and use storage. Review them here and abort the ones you no longer need." })),
      filterForm,
      head, loadingRow, empty, list, moreRow,
      status, error,
      olderRow,
      hint,
      h("div", { class: "cd-actions pol-actions" }, h("span", { class: "pol-spacer" }), close, abortSel)));
  close.addEventListener("click", () => dlg.close());
  dlg.addEventListener("cancel", (e) => { if (busy) e.preventDefault(); });
  dlg.addEventListener("click", (e) => { if (e.target === dlg && !busy) dlg.close(); });
  update();

  return new Promise((resolve) => {
    dlg.addEventListener("close", () => { dlg.remove(); if (changed) toast.show(`Unfinished uploads of ${bucket} cleaned up.`); resolve({ changed }); });
    document.body.append(dlg);
    dlg.showModal();
    filter.focus();
    load();
  });
}
