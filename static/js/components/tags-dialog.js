import { h } from "../dom.js";
import { icon } from "../icons.js";
import { getBucketTags, putBucketTags } from "../api.js";
import { toast } from "./toast.js";
import { nameSpan } from "./confirm-dialog.js";

// tagsDialog({ bucket }) -> Promise<{ error? } | { changed }>
// Editor for the bucket's tags (Advanced only; the server does not care about the toggle).
// Tags managed by Bucketeer (bucketeer-*) are shown read only with a lock and are never sent.
// The etag the tags were loaded with travels with Save, so a change made elsewhere shows up as a
// 409 and the user's draft is kept for comparison. The server is authoritative for every rule.
let seq = 0;
const MAX_TAGS = 50;
const KEY_MAX = 128;
const VALUE_MAX = 256;
const CHARS = /^[\p{L}\p{N}_ +\-=.:/@]*$/u; // letters, digits, space and + - = . _ : / @

const copy = (rows) => rows.map((r) => ({ key: r.key, value: r.value }));
const sameTags = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const MANAGED_HINT = { "bucketeer-budget": "Edit the budget number on the bucket (More, Change budget number)." };

// The same rules as the server, for instant feedback while typing.
// A tag equal to one already stored is not flagged for character/length/aws: rules (the storage
// service accepts tags outside AWS's rules; they must not block unrelated saves).
function checkRow(r, all, stored = []) {
  const out = {};
  const keep = stored.some((s) => s.key === r.key && s.value === r.value);
  if (/^bucketeer-/i.test(r.key)) out.key = "bucketeer- keys are managed by Bucketeer.";
  else if (all.filter((x) => x.key === r.key).length > 1) out.key = "This key is used twice.";
  else if (keep) return out;
  else if (!r.key) out.key = "Enter a key.";
  else if (r.key.length > KEY_MAX) out.key = `At most ${KEY_MAX} characters.`;
  else if (!CHARS.test(r.key)) out.key = "Letters, digits, spaces and + - = . _ : / @ only.";
  else if (/^aws:/i.test(r.key)) out.key = "Keys starting with aws: are reserved.";
  if (r.value.length > VALUE_MAX) out.value = `At most ${VALUE_MAX} characters.`;
  else if (!CHARS.test(r.value)) out.value = "Letters, digits, spaces and + - = . _ : / @ only.";
  return out;
}

export async function tagsDialog({ bucket }) {
  const id = `tags${++seq}`;
  let state;
  try { state = await getBucketTags(bucket); } catch (err) { return { error: err }; }
  let managed = state.tags.filter((t) => t.managed);
  let tags = copy(state.tags.filter((t) => !t.managed));
  let original = copy(tags);
  let busy = false;
  let changed = false;
  let touched = new Set(); // rows whose errors are shown (typed in, or reported by the server)
  let serverErrs = new Map();

  const error = h("p", { class: "qd-error", role: "alert", hidden: true });
  const showError = (m) => { error.textContent = m; error.hidden = !m; };
  const list = h("ol", { class: "tg-rows" });
  const managedList = h("ul", { class: "tg-managed", "aria-label": "Tags managed by Bucketeer" });
  const empty = h("p", { class: "cors-empty", text: "No tags of your own yet. Tags are labels such as project=genomics that show up in reports." });
  const count = h("span", { class: "chip" });
  const addBtn = h("button", { class: "btn btn-sm", type: "button" }, icon("plus"), "Add tag");
  const save = h("button", { class: "btn btn-primary", type: "submit" }, icon("check"), "Save tags");
  const revert = h("button", { class: "btn", type: "button", text: "Discard changes" });
  const close = h("button", { class: "btn", type: "button", text: "Close" });
  const mine = h("textarea", { class: "pol-text pol-mine", readonly: true, rows: 6, "aria-label": "Your unsaved tags" });
  const useMine = h("button", { class: "btn btn-sm", type: "button", text: "Restore my version" });
  const copyMine = h("button", { class: "btn btn-sm", type: "button" }, icon("copy"), "Copy my tags");
  const conflict = h("section", { class: "pol-conflict", hidden: true, role: "alert" },
    h("b", { text: "The tags changed while you were editing" }),
    h("p", { text: "The editor now shows the current tags. Your unsaved version is kept below: restore it to save it over the current tags, or copy it." }),
    mine, h("div", { class: "pol-tools" }, useMine, copyMine));
  let draft = null;
  useMine.addEventListener("click", () => { tags = copy(draft); conflict.hidden = true; render(); });
  copyMine.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(mine.value); toast.show("Copied your tags."); } catch { toast.error("Could not copy. Select the text and copy it."); }
  });

  const total = () => managed.length + tags.length;
  const dirty = () => !sameTags(tags, original);
  const anyError = () => tags.some((r) => Object.keys(checkRow(r, tags, original)).length);

  // Row messages are repainted in place so typing never loses focus.
  function paintErrors() {
    [...list.children].forEach((li, i) => {
      const r = tags[i];
      const found = touched.has(i) ? checkRow(r, tags, original) : {};
      const srv = serverErrs.get(i) || {};
      for (const f of ["key", "value"]) {
        const msg = found[f] || srv[f] || "";
        const slot = li.querySelector(`.tg-err-${f}`);
        slot.textContent = msg;
        slot.hidden = !msg;
        li.querySelector(`.tg-${f}`).toggleAttribute("aria-invalid", !!msg);
      }
    });
  }

  function update() {
    save.disabled = busy || !dirty();
    revert.disabled = busy || !dirty();
    addBtn.disabled = busy || total() >= MAX_TAGS;
    empty.hidden = tags.length > 0;
    count.textContent = `${total()} of ${MAX_TAGS} tags`;
    paintErrors();
  }

  function row(r, i) {
    const rid = `${id}-r${i}`;
    const key = h("input", { class: "input mono-input tg-key", id: `${rid}-k`, type: "text", value: r.key, placeholder: "key", autocomplete: "off", autocapitalize: "off", spellcheck: false, maxlength: 200, "aria-label": `Tag ${i + 1} key`, "aria-describedby": `${rid}-ke` });
    const val = h("input", { class: "input mono-input tg-value", id: `${rid}-v`, type: "text", value: r.value, placeholder: "value (optional)", autocomplete: "off", autocapitalize: "off", spellcheck: false, maxlength: 300, "aria-label": `Tag ${i + 1} value`, "aria-describedby": `${rid}-ve` });
    const edit = (f, el) => el.addEventListener("input", () => { r[f] = el.value; touched.add(i); serverErrs.delete(i); showError(""); update(); });
    edit("key", key);
    edit("value", val);
    const del = h("button", { class: "btn btn-ghost btn-sm tg-del", type: "button", "aria-label": `Remove tag ${r.key || i + 1}` }, icon("trash"), h("span", { class: "tg-del-l", text: "Remove" }));
    del.addEventListener("click", () => { tags.splice(i, 1); touched = new Set(); serverErrs = new Map(); render(); });
    return h("li", { class: "tg-row" },
      h("div", { class: "tg-cell" }, key, h("p", { class: "set-err tg-err-key", id: `${rid}-ke`, role: "alert", hidden: true })),
      h("div", { class: "tg-cell" }, val, h("p", { class: "set-err tg-err-value", id: `${rid}-ve`, role: "alert", hidden: true })),
      del);
  }

  function render() {
    managedList.replaceChildren(...managed.map((t) => h("li", { class: "tg-m" },
      icon("lock", "icon tg-lock"),
      h("code", { class: "tg-m-k", text: t.key }), h("code", { class: "tg-m-v", text: t.value }),
      h("span", { class: "tg-m-h", text: MANAGED_HINT[t.key] || "Managed by Bucketeer. It cannot be edited here." }))));
    managedList.hidden = !managed.length;
    mlabel.hidden = !managed.length;
    list.replaceChildren(...tags.map(row));
    showError("");
    update();
  }

  addBtn.addEventListener("click", () => { tags.push({ key: "", value: "" }); render(); list.lastElementChild.querySelector("input").focus(); });
  revert.addEventListener("click", () => { tags = copy(original); touched = new Set(); serverErrs = new Map(); conflict.hidden = true; render(); });

  const mlabel = h("p", { class: "pol-lbl", text: "Managed by Bucketeer" });
  const form = h("form", { class: "pol-form tg-form", novalidate: true },
    conflict,
    mlabel, managedList,
    h("div", { class: "pol-bar" }, h("p", { class: "pol-lbl", text: "Your tags" }), h("div", { class: "pol-tools" }, count, addBtn)),
    empty, list, error,
    h("div", { class: "cd-actions pol-actions" }, revert, h("span", { class: "pol-spacer" }), close, save));

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (busy || save.disabled) return;
    touched = new Set(tags.map((_, i) => i));
    if (anyError()) { paintErrors(); showError("Fix the marked tags and save again."); list.querySelector("[aria-invalid]")?.focus(); return; }
    busy = true;
    showError("");
    save.replaceChildren(h("span", { class: "spinner", "aria-hidden": "true" }), "Saving…");
    update();
    try {
      const res = await putBucketTags(bucket, { tags: copy(tags), etag: state.etag });
      state = res;
      managed = res.tags.filter((t) => t.managed);
      original = copy(res.tags.filter((t) => !t.managed));
      tags = copy(original);
      touched = new Set();
      serverErrs = new Map();
      changed = changed || res.changed;
      render();
      toast.show(res.changed ? `Tags saved for ${bucket}.` : "Nothing to change.");
    } catch (err) {
      if (err.status === 401) { dlg.close(); return; }
      if (err.status === 409 && err.body?.etagMismatch) {
        draft = copy(tags);
        mine.value = JSON.stringify(draft, null, 2);
        state = err.body.current;
        managed = state.tags.filter((t) => t.managed);
        original = copy(state.tags.filter((t) => !t.managed));
        tags = copy(original);
        touched = new Set();
        serverErrs = new Map();
        conflict.hidden = false;
        render();
      } else if (err.status === 422 && err.body?.errors) {
        serverErrs = new Map();
        for (const x of err.body.errors) {
          if (x.tag && tags[x.tag - 1]) serverErrs.set(x.tag - 1, { ...serverErrs.get(x.tag - 1), [x.field === "value" ? "value" : "key"]: x.message });
        }
        update();
        showError(err.body.errors.some((x) => x.tag) ? "Fix the marked tags and save again." : err.message);
      } else showError(err.message || "Could not save the tags.");
    } finally {
      busy = false;
      save.replaceChildren(icon("check"), "Save tags");
      update();
    }
  });

  const dlg = h("dialog", { class: "cd qd pol tg", "aria-labelledby": `${id}-t`, "aria-describedby": `${id}-m` },
    h("div", { class: "pol-body" },
      h("div", { class: "qd-head" },
        h("h2", { id: `${id}-t` }, "Tags of ", nameSpan(bucket)),
        h("p", { class: "cd-msg", id: `${id}-m`, text: "Free-form labels on the bucket, for your own bookkeeping or for reports. An Advanced tool." })),
      form));
  render();
  close.addEventListener("click", () => dlg.close());
  dlg.addEventListener("cancel", (e) => { if (busy) e.preventDefault(); });
  dlg.addEventListener("click", (e) => { if (e.target === dlg && !busy) dlg.close(); });

  return new Promise((resolve) => {
    dlg.addEventListener("close", () => { dlg.remove(); resolve({ changed }); });
    document.body.append(dlg);
    dlg.showModal();
    close.focus();
  });
}
