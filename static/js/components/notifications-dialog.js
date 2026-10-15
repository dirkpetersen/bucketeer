import { h } from "../dom.js";
import { icon } from "../icons.js";
import { getBucketNotifications, putBucketNotifications } from "../api.js";
import { toast } from "./toast.js";
import { nameSpan } from "./confirm-dialog.js";

// notificationsDialog({ bucket }) -> Promise<{ error? } | { changed }>
// Bucket notifications (Advanced only; the server does not care about the toggle). The user picks a
// NAME from the administrator's list of endpoints, never an address. Notifications that point
// elsewhere (other topics, queues, Lambda) are "external": shown read-only, kept as they are and
// removable. The etag the list was loaded with travels with Save; a 409 keeps the user's draft.
let seq = 0;
const MAX = 100;
const CREATED = "s3:ObjectCreated:";
const REMOVED = "s3:ObjectRemoved:";
const GROUPS = [
  { title: "When a file is added", any: { value: `${CREATED}*`, label: "Any new or replaced file" }, parts: [
    { value: `${CREATED}Put`, label: "Uploaded in one piece" },
    { value: `${CREATED}Post`, label: "Uploaded with a web form" },
    { value: `${CREATED}Copy`, label: "Copied from another place" },
    { value: `${CREATED}CompleteMultipartUpload`, label: "A large upload finished" },
  ] },
  { title: "When a file is deleted", any: { value: `${REMOVED}*`, label: "Any deletion" }, parts: [
    { value: `${REMOVED}Delete`, label: "Permanently deleted" },
    { value: `${REMOVED}DeleteMarkerCreated`, label: "Hidden by a delete marker (versioned buckets)" },
  ] },
];
const KNOWN = new Set(GROUPS.flatMap((g) => [g.any.value, ...g.parts.map((p) => p.value)]));
const EVENT_NAMES = Object.fromEntries(GROUPS.flatMap((g) => [[g.any.value, g.any.label], ...g.parts.map((p) => [p.value, p.label])]));
const CONTROL = /[\x00-\x1f\x7f]/;

const copy = (x) => JSON.parse(JSON.stringify(x));
const clean = (n) => (n.managed === false
  ? { id: n.id, external: true }
  : { id: n.id, endpoint: n.endpoint, events: n.events, prefix: n.prefix || null, suffix: n.suffix || null });
const same = (a, b) => JSON.stringify(a.map(clean)) === JSON.stringify(b.map(clean));

export async function notificationsDialog({ bucket }) {
  const id = `nt${++seq}`;
  let state;
  try { state = await getBucketNotifications(bucket); } catch (err) { return { error: err }; }
  let items = copy(state.notifications);
  let busy = false;
  let changed = false;
  let draft = null;

  const error = h("p", { class: "qd-error", role: "alert", hidden: true });
  const showError = (m) => { error.textContent = m; error.hidden = !m; };
  const cards = h("ol", { class: "nt-list" });
  const addBtn = h("button", { class: "btn btn-sm", type: "button" }, icon("plus"), "Add notification");
  const save = h("button", { class: "btn btn-primary", type: "submit" }, icon("check"), "Save notifications");
  const revert = h("button", { class: "btn", type: "button", text: "Discard changes" });
  const close = h("button", { class: "btn", type: "button", text: "Close" });
  const mine = h("textarea", { class: "pol-text pol-mine", readonly: true, rows: 6, "aria-label": "Your unsaved notifications, JSON" });
  const useMine = h("button", { class: "btn btn-sm", type: "button", text: "Restore my version" });
  const copyMine = h("button", { class: "btn btn-sm", type: "button" }, icon("copy"), "Copy my notifications");
  const conflict = h("section", { class: "pol-conflict", hidden: true, role: "alert" },
    h("b", { text: "The notifications changed while you were editing" }),
    h("p", { text: "The editor now shows the current ones. Your unsaved version is kept below: restore it to save it over the current ones, or copy it." }),
    mine, h("div", { class: "pol-tools" }, useMine, copyMine));
  useMine.addEventListener("click", () => {
    // external ones that no longer exist cannot be restored
    items = draft.map((n) => (n.external ? items.find((x) => x.id === n.id && x.managed === false) : { ...n, managed: true, kind: "topic" })).filter(Boolean);
    conflict.hidden = true;
    render();
  });
  copyMine.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(mine.value); toast.show("Copied your notifications."); } catch { toast.error("Could not copy. Select the text and copy it."); }
  });

  const noEndpoints = () => !state.endpoints.length;
  const empty = h("p", { class: "cors-empty" });
  const dirty = () => !same(items, state.notifications);
  const localProblem = (n) => {
    if (n.managed === false) return null;
    if (!n.endpoint) return "Choose where to send it.";
    if (!n.events.length) return "Choose at least one event.";
    if ((n.prefix && CONTROL.test(n.prefix)) || (n.suffix && CONTROL.test(n.suffix))) return "Names cannot contain control characters.";
    return null;
  };
  function update() {
    const bad = items.some(localProblem);
    save.disabled = busy || !dirty() || bad;
    revert.disabled = busy || !dirty();
    addBtn.disabled = busy || noEndpoints() || items.length >= MAX;
    empty.hidden = items.length > 0;
    empty.textContent = noEndpoints()
      ? "Your administrator hasn't set up any notification endpoints yet. Once they add one, you can send events for this bucket to it here."
      : "No notifications. Add one to tell another system when files are added to or deleted from this bucket.";
  }

  const nextId = () => {
    let n = items.length + 1;
    while (items.some((x) => x.id === `notification-${n}`)) n++;
    return `notification-${n}`;
  };

  function externalCard(n, i) {
    const del = h("button", { class: "btn btn-ghost btn-sm", type: "button", "aria-label": `Remove notification ${n.id}` }, icon("trash"), "Remove");
    del.addEventListener("click", () => { items.splice(i, 1); render(); });
    return h("li", { class: "nt-card nt-external" },
      h("div", { class: "cors-rule-h" }, h("b", { text: n.id }), h("span", { class: "chip", text: "Set up elsewhere" }), h("span", { class: "pol-spacer" }), del),
      h("p", { class: "cors-hint", text: `Sends to ${n.kind === "topic" ? "topic" : n.kind} ${n.topic}. Bucketeer keeps it as it is; you can only remove it here.` }),
      h("p", { class: "nt-ev", text: n.events.map((e) => EVENT_NAMES[e] || e).join(", ") || "No events" }),
      n.prefix || n.suffix ? h("p", { class: "cors-hint", text: [n.prefix && `Names starting with ${n.prefix}`, n.suffix && `ending with ${n.suffix}`].filter(Boolean).join(", ") }) : null);
  }

  function card(n, i) {
    if (n.managed === false) return externalCard(n, i);
    const cid = `${id}-n${i}`;
    const sel = h("select", { class: "input", id: `${cid}-e` },
      h("option", { value: "", text: "Choose an endpoint…" }),
      ...state.endpoints.map((e) => h("option", { value: e.name, text: `${e.name} (${e.type})` })));
    // an endpoint that is no longer offered stays visible so nothing silently changes
    if (n.endpoint && !state.endpoints.some((e) => e.name === n.endpoint)) sel.append(h("option", { value: n.endpoint, text: n.endpoint }));
    sel.value = n.endpoint || "";
    sel.addEventListener("change", () => { n.endpoint = sel.value || null; update(); });

    const boxes = [];
    const events = h("div", { class: "nt-groups" }, GROUPS.map((g) => {
      const anyBox = h("input", { type: "checkbox", checked: n.events.includes(g.any.value) });
      const parts = g.parts.map((p) => {
        const box = h("input", { type: "checkbox", checked: n.events.includes(p.value) });
        box.addEventListener("change", () => {
          n.events = box.checked ? [...n.events, p.value] : n.events.filter((x) => x !== p.value);
          update();
        });
        boxes.push(box);
        return h("label", { class: "qd-check" }, box, h("span", { text: p.label }));
      });
      const sync = () => { g.parts.forEach((p, k) => { parts[k].querySelector("input").disabled = anyBox.checked; }); };
      anyBox.addEventListener("change", () => {
        const drop = new Set(g.parts.map((p) => p.value));
        n.events = anyBox.checked ? [...n.events.filter((x) => !drop.has(x)), g.any.value] : n.events.filter((x) => x !== g.any.value);
        if (anyBox.checked) g.parts.forEach((p, k) => { parts[k].querySelector("input").checked = false; });
        sync();
        update();
      });
      sync();
      return h("fieldset", { class: "cors-methods nt-group" }, h("legend", { text: g.title }),
        h("label", { class: "qd-check" }, anyBox, h("span", { text: g.any.label })), ...parts);
    }));
    // events stored by someone else that this editor has no checkbox for stay and are shown
    const odd = n.events.filter((e) => !KNOWN.has(e));
    const oddNote = odd.length ? h("p", { class: "cors-hint", text: `Also set (kept as is): ${odd.join(", ")}` }) : null;

    const prefix = h("input", { class: "input", type: "text", id: `${cid}-p`, placeholder: "images/", autocomplete: "off", spellcheck: false, value: n.prefix || "" });
    const suffix = h("input", { class: "input", type: "text", id: `${cid}-s`, placeholder: ".csv", autocomplete: "off", spellcheck: false, value: n.suffix || "" });
    prefix.addEventListener("input", () => { n.prefix = prefix.value || null; update(); });
    suffix.addEventListener("input", () => { n.suffix = suffix.value || null; update(); });
    const nerr = h("p", { class: "set-err cors-rule-err", role: "alert", hidden: true });
    const del = h("button", { class: "btn btn-ghost btn-sm", type: "button", "aria-label": `Remove notification ${n.id}` }, icon("trash"), "Remove");
    del.addEventListener("click", () => { items.splice(i, 1); render(); });
    return h("li", { class: "nt-card" },
      h("div", { class: "cors-rule-h" }, h("b", { text: n.id }), h("span", { class: "pol-spacer" }), del),
      h("div", { class: "field" }, h("label", { for: `${cid}-e`, text: "Send to" }), sel,
        h("small", { class: "cors-hint", text: "Only endpoints your administrator has approved are listed." })),
      events, oddNote,
      h("div", { class: "cors-grid" },
        h("div", { class: "field" }, h("label", { for: `${cid}-p`, text: "Only names starting with (optional)" }), prefix),
        h("div", { class: "field" }, h("label", { for: `${cid}-s`, text: "Only names ending with (optional)" }), suffix)),
      nerr);
  }

  function render() {
    cards.replaceChildren(...items.map(card));
    showError("");
    update();
  }

  function showFieldErrors(errors) {
    const general = [];
    const per = new Map();
    for (const e of errors) {
      if (e.item && cards.children[e.item - 1]) per.set(e.item, [...(per.get(e.item) || []), e.message]);
      else general.push(e.message);
    }
    for (const [k, msgs] of per) {
      const slot = cards.children[k - 1].querySelector(".cors-rule-err");
      if (!slot) { general.push(...msgs); continue; }
      slot.replaceChildren(...msgs.map((m) => h("span", { class: "cors-err-line", text: m })));
      slot.hidden = false;
    }
    if (general.length) showError(general.join(" "));
  }

  addBtn.addEventListener("click", () => {
    items.push({ id: nextId(), endpoint: state.endpoints.length === 1 ? state.endpoints[0].name : null, managed: true, kind: "topic", events: [`${CREATED}*`], prefix: null, suffix: null });
    render();
    cards.lastElementChild.querySelector("select")?.focus();
  });
  revert.addEventListener("click", () => { items = copy(state.notifications); conflict.hidden = true; render(); });

  const form = h("form", { class: "pol-form cors-form", novalidate: true },
    conflict,
    h("p", { class: "lcd-tip", role: "note" }, icon("shield", "icon"),
      h("span", { text: "Events are sent to one of the endpoints your administrator approved. You choose which events and which file names; the address itself cannot be changed here." })),
    h("div", { class: "pol-bar" }, h("div", { class: "pol-tools" }, addBtn)),
    empty, cards, error,
    h("div", { class: "cd-actions pol-actions" }, revert, h("span", { class: "pol-spacer" }), close, save));

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (busy || save.disabled) return;
    busy = true;
    showError("");
    cards.querySelectorAll(".cors-rule-err").forEach((x) => { x.hidden = true; x.replaceChildren(); });
    save.replaceChildren(h("span", { class: "spinner", "aria-hidden": "true" }), "Saving…");
    update();
    try {
      const res = await putBucketNotifications(bucket, { notifications: items.map(clean), etag: state.etag });
      state = res;
      items = copy(res.notifications);
      changed = changed || res.changed;
      render();
      toast.show(res.changed ? `Notifications saved for ${bucket}.` : "Nothing to change.");
    } catch (err) {
      if (err.status === 401) { dlg.close(); return; }
      if (err.status === 409 && err.body?.etagMismatch) {
        draft = items.map((n) => (n.managed === false ? { id: n.id, external: true } : clean(n)));
        mine.value = JSON.stringify(draft, null, 2);
        state = err.body.current;
        items = copy(state.notifications);
        conflict.hidden = false;
        render();
      } else if (err.status === 422 && err.body?.errors) {
        showFieldErrors(err.body.errors);
        if (err.body.errors.some((x) => x.item)) showError("Fix the marked notifications and save again.");
      } else showError(err.message || "Could not save the notifications.");
    } finally {
      busy = false;
      save.replaceChildren(icon("check"), "Save notifications");
      update();
    }
  });

  const dlg = h("dialog", { class: "cd qd pol cors nt", "aria-labelledby": `${id}-t`, "aria-describedby": `${id}-m` },
    h("div", { class: "pol-body" },
      h("div", { class: "qd-head" },
        h("h2", { id: `${id}-t` }, "Notifications of ", nameSpan(bucket)),
        h("p", { class: "cd-msg", id: `${id}-m`, text: "Tell another system when files are added to or deleted from this bucket. This is an Advanced tool." })),
      form));
  render();
  close.addEventListener("click", () => dlg.close());
  dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });

  return new Promise((resolve) => {
    dlg.addEventListener("close", () => { dlg.remove(); resolve({ changed }); });
    document.body.append(dlg);
    dlg.showModal();
    close.focus();
  });
}
