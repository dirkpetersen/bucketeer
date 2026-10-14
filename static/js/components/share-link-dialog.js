import { h } from "../dom.js";
import { icon } from "../icons.js";
import { listBucketObjects, createShareLink } from "../api.js";
import { formatBytes } from "../format.js";
import { copyText } from "../clipboard.js";
import { toast } from "./toast.js";
import { nameSpan } from "./confirm-dialog.js";

// shareLinkDialog({ bucket }) -> Promise<void>
// Pick one file by typing its key (prefix autocomplete from a read-only, one-page listing:
// this is not a file browser), pick how long the link lasts (7 days at most), generate a
// presigned download link. The link is kept in memory only, never in storage or attributes,
// and is dropped when the key or the duration changes or the dialog closes.
let seq = 0;
const DEBOUNCE_MS = 220;
const EXPIRIES = [
  { label: "1 hour", seconds: 3600 },
  { label: "1 day", seconds: 86400 },
  { label: "7 days", seconds: 604800 },
];

export function shareLinkDialog({ bucket }) {
  const id = `sl${++seq}`;
  let options = []; // [{ id, kind: "folder" | "file", value, el }]
  let active = -1;
  let ticket = 0; // drops answers of superseded autocomplete requests
  let timer = 0;
  let busy = false;
  let link = null; // { url, expiresAt } while shown

  const error = h("p", { class: "qd-error", role: "alert", hidden: true });
  const showError = (m) => { error.textContent = m; error.hidden = !m; };

  const input = h("input", {
    class: "input slk-key", id: `${id}-key`, type: "text", role: "combobox", autocomplete: "off", autocapitalize: "off",
    spellcheck: "false", placeholder: "reports/2026/summary.pdf", "aria-autocomplete": "list", "aria-expanded": "false",
    "aria-controls": `${id}-list`, "aria-describedby": `${id}-hint`, maxlength: 1024,
  });
  const list = h("ul", { class: "slk-list", id: `${id}-list`, role: "listbox", "aria-label": "Matching files and folders", hidden: true });
  const status = h("p", { class: "slk-status", role: "status", "aria-live": "polite" });
  const select = h("select", { class: "input slk-exp", id: `${id}-exp`, "aria-label": "Link lasts" },
    EXPIRIES.map((e, i) => h("option", { value: String(e.seconds), selected: i === 1, text: e.label })));
  const go = h("button", { class: "btn btn-primary", type: "submit", disabled: true }, icon("check"), "Create link");
  const close = h("button", { class: "btn", type: "button", text: "Close" });

  const resultWrap = h("div", { class: "slk-result", hidden: true });

  function setOpen(open) {
    list.hidden = !open || options.length === 0;
    input.setAttribute("aria-expanded", String(!list.hidden));
    if (list.hidden) { active = -1; input.removeAttribute("aria-activedescendant"); }
  }
  function setActive(i) {
    if (!options.length) return;
    active = (i + options.length) % options.length;
    options.forEach((o, n) => o.el.setAttribute("aria-selected", String(n === active)));
    const cur = options[active];
    input.setAttribute("aria-activedescendant", cur.id);
    cur.el.scrollIntoView({ block: "nearest" });
  }

  function render(res) {
    options = [];
    const items = [];
    const add = (kind, value, label, meta) => {
      const oid = `${id}-o${options.length}`;
      const el = h("li", { class: "slk-opt", id: oid, role: "option", "aria-selected": "false", "data-kind": kind },
        icon(kind === "folder" ? "right" : "download", "icon slk-ic"),
        h("span", { class: "slk-name", text: label }),
        meta && h("span", { class: "slk-meta", text: meta }));
      el.addEventListener("mousedown", (e) => e.preventDefault()); // keep focus in the field
      el.addEventListener("click", () => choose(options.find((o) => o.id === oid)));
      options.push({ id: oid, kind, value, el });
      items.push(el);
    };
    for (const p of res.prefixes) add("folder", p.prefix, p.prefix.slice(res.prefix.length) || p.prefix);
    for (const e of res.entries) add("file", e.key, e.key.slice(res.prefix.length) || e.key, formatBytes(e.size));
    list.replaceChildren(...items);
    status.textContent = items.length
      ? `${items.length} ${items.length === 1 ? "match" : "matches"}${res.truncated ? ", keep typing to narrow them down" : ""}.`
      : "No files or folders start with that.";
    setOpen(items.length > 0);
    if (items.length) setActive(0);
  }

  async function suggest() {
    const mine = ++ticket;
    try {
      const res = await listBucketObjects(bucket, input.value);
      if (mine !== ticket) return;
      render(res);
    } catch (err) {
      if (mine !== ticket) return;
      if (err.status === 401) { dlg.close(); return; }
      options = [];
      setOpen(false);
      status.textContent = "";
      showError(err.message || "Could not look up file names.");
    }
  }
  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(suggest, DEBOUNCE_MS);
  }

  function choose(opt) {
    if (!opt) return;
    input.value = opt.value;
    resetLink();
    syncGo();
    if (opt.kind === "folder") {
      suggest(); // folders expand in place
    } else {
      ticket++;
      options = [];
      setOpen(false);
      status.textContent = "";
    }
    input.focus();
  }

  function syncGo() { go.disabled = busy || input.value.trim() === "" || input.value.endsWith("/"); }
  function resetLink() {
    link = null;
    resultWrap.hidden = true;
    resultWrap.replaceChildren();
  }

  input.addEventListener("input", () => { showError(""); resetLink(); syncGo(); schedule(); });
  input.addEventListener("focus", () => { if (!options.length && !input.value) suggest(); else if (options.length) setOpen(true); });
  input.addEventListener("blur", () => setOpen(false));
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (list.hidden) { if (options.length) setOpen(true); else suggest(); }
      if (options.length) setActive(e.key === "ArrowDown" ? active + 1 : active - 1);
    } else if (e.key === "Enter" && !list.hidden && active >= 0) {
      e.preventDefault();
      choose(options[active]);
    } else if (e.key === "Escape" && !list.hidden) {
      e.preventDefault();
      e.stopPropagation(); // closes the list, not the dialog
      setOpen(false);
    }
  });
  select.addEventListener("change", resetLink);

  function showLink(res) {
    link = res;
    const when = new Date(res.expiresAt);
    const field = h("input", {
      class: "input slk-url", id: `${id}-url`, type: "text", readonly: true, spellcheck: "false",
      "aria-label": "Share link",
    });
    field.value = res.url; // a property, never an attribute
    field.addEventListener("click", () => field.select());
    const copy = h("button", { class: "btn btn-sm", type: "button" }, icon("copy"), h("span", { text: "Copy" }));
    copy.addEventListener("click", async () => {
      const ok = await copyText(link.url);
      toast[ok ? "show" : "error"](ok ? "Link copied." : "Could not copy. Select the link and copy it by hand.");
    });
    resultWrap.replaceChildren(
      h("label", { class: "slk-lbl", for: field.id, text: "Your link" }),
      h("div", { class: "slk-url-row" }, field, copy),
      h("p", { class: "slk-expiry" }, "Works until ",
        h("time", { datetime: res.expiresAt, text: Number.isNaN(when.getTime()) ? res.expiresAt : when.toLocaleString(undefined, { dateStyle: "full", timeStyle: "short" }) }),
        ". Anyone who has the link can download this file until then."));
    resultWrap.hidden = false;
  }

  const form = h("form", { class: "slk-form", novalidate: true },
    h("div", { class: "field" },
      h("label", { for: input.id, text: "File name (key)" }),
      input,
      h("p", { class: "slk-hint", id: `${id}-hint`, text: "Start typing to see matching files. Choose a folder to look inside it." }),
      list, status),
    h("div", { class: "field" },
      h("label", { for: select.id, text: "Link works for" }), select),
    h("p", { class: "lcd-tip", role: "note" }, icon("alert", "icon"),
      "The link is signed with your access key. It stops working when that key is rotated or deleted, or when your account is suspended."),
    error, resultWrap,
    h("div", { class: "cd-actions" }, close, go));

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (busy || go.disabled) return;
    busy = true;
    showError("");
    resetLink();
    setOpen(false);
    go.disabled = true;
    go.replaceChildren(h("span", { class: "spinner", "aria-hidden": "true" }), "Creating…");
    try {
      showLink(await createShareLink(bucket, input.value, Number(select.value)));
      resultWrap.querySelector("button").focus();
    } catch (err) {
      if (err.status === 401) { dlg.close(); return; }
      showError(err.message || "Could not create the link.");
    } finally {
      busy = false;
      go.replaceChildren(icon("check"), "Create link");
      syncGo();
    }
  });

  const dlg = h("dialog", { class: "cd qd lcd slk", "aria-labelledby": `${id}-t`, "aria-describedby": `${id}-m` },
    h("div", { class: "lcd-body" },
      h("div", { class: "qd-head" },
        h("h2", { id: `${id}-t` }, "Share a file from ", nameSpan(bucket)),
        h("p", { class: "cd-msg", id: `${id}-m`, text: "Create a temporary download link for one file. Links last 7 days at most." })),
      form));
  close.addEventListener("click", () => dlg.close());
  dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });

  return new Promise((resolve) => {
    dlg.addEventListener("close", () => {
      clearTimeout(timer);
      ticket++;
      link = null;
      dlg.remove();
      resolve();
    });
    document.body.append(dlg);
    dlg.showModal();
    input.focus();
  });
}
