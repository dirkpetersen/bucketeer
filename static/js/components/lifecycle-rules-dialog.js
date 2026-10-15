import { h } from "../dom.js";
import { icon } from "../icons.js";
import { getLifecycleRules, putLifecycleRules } from "../api.js";
import { toast } from "./toast.js";
import { confirmDialog, nameSpan } from "./confirm-dialog.js";

// lifecycleRulesDialog({ bucket }) -> Promise<{ error? } | { changed }>
// The full lifecycle editor (Advanced only; the server does not care about the toggle). It edits the
// WHOLE configuration, including the three simple rules Bucketeer manages: those are locked until the
// user presses "Edit Bucketeer rule", and saving a change to one asks again. Rules that already have
// transitions (moving files to another storage class) are shown read-only: Bucketeer cannot create
// or change transitions yet and passes them back untouched. The etag the rules were loaded with
// travels with Save, so a change made elsewhere shows up as a 409 and the draft is kept.
let seq = 0;
const MAX_RULES = 1000;
const MAX_DAYS = 36500;
const MAX_NEWER = 100;
const MAX_TAGS = 10;
const TAG_CHARS = /^[\p{L}\p{N}_ +\-=.:/@]*$/u;
const UNITS = [["B", 1], ["KiB", 1024], ["MiB", 1024 ** 2], ["GiB", 1024 ** 3], ["TiB", 1024 ** 4]];

const copy = (v) => JSON.parse(JSON.stringify(v));
const isNum = (n) => typeof n === "number" && Number.isFinite(n);
const clean = (r) => {
  const f = r.filter || {};
  const filter = {};
  if (f.prefix) filter.prefix = f.prefix;
  if (f.tags?.length) filter.tags = f.tags.map((t) => ({ key: t.key, value: t.value }));
  if (f.sizeGreaterThan != null) filter.sizeGreaterThan = f.sizeGreaterThan;
  if (f.sizeLessThan != null) filter.sizeLessThan = f.sizeLessThan;
  return {
    id: r.id, status: r.status, filter,
    expiration: r.expiration && Object.keys(r.expiration).length ? r.expiration : null,
    noncurrentExpiration: r.noncurrentExpiration && Object.keys(r.noncurrentExpiration).length ? r.noncurrentExpiration : null,
    abortMultipartDays: r.abortMultipartDays ?? null,
    transitions: r.transitions ?? null,
  };
};
const key = (r) => JSON.stringify(clean(r));

const fmtBytes = (n) => {
  for (let i = UNITS.length - 1; i >= 0; i--) if (n >= UNITS[i][1] && n % UNITS[i][1] === 0) return `${n / UNITS[i][1]} ${UNITS[i][0]}`;
  return `${n} B`;
};

// "logs/" tagged team=bio and larger than 1 MiB, in words.
function scopeText(f) {
  const bits = [];
  if (f.prefix) bits.push(`whose name starts with “${f.prefix}”`);
  if (f.tags?.length) bits.push(`tagged ${f.tags.map((t) => `${t.key}=${t.value}`).join(" and ")}`);
  if (isNum(f.sizeGreaterThan) && isNum(f.sizeLessThan)) bits.push(`between ${fmtBytes(f.sizeGreaterThan)} and ${fmtBytes(f.sizeLessThan)}`);
  else if (isNum(f.sizeGreaterThan)) bits.push(`larger than ${fmtBytes(f.sizeGreaterThan)}`);
  else if (isNum(f.sizeLessThan)) bits.push(`smaller than ${fmtBytes(f.sizeLessThan)}`);
  return bits.length ? `files ${bits.join(", ")}` : "every file in the bucket";
}
function describe(r) {
  const acts = [];
  const e = r.expiration || {};
  if (isNum(e.days)) acts.push(`delete them ${e.days} day${e.days === 1 ? "" : "s"} after they were created`);
  else if (e.date) acts.push(`delete them on ${e.date.slice(0, 10)}`);
  else if (e.expiredObjectDeleteMarker) acts.push("remove delete markers that hide nothing any more");
  const n = r.noncurrentExpiration;
  if (n && isNum(n.days)) {
    acts.push(`delete old versions ${n.days} day${n.days === 1 ? "" : "s"} after they were replaced${isNum(n.newerNoncurrentVersions) ? `, always keeping the newest ${n.newerNoncurrentVersions}` : ""}`);
  }
  if (isNum(r.abortMultipartDays)) acts.push(`discard unfinished uploads after ${r.abortMultipartDays} day${r.abortMultipartDays === 1 ? "" : "s"}`);
  if (r.transitions) acts.push("move files to another storage class (set outside Bucketeer)");
  if (!acts.length) return "This rule does nothing yet. Choose an action below.";
  return `${r.status === "Disabled" ? "Paused. When switched on: for" : "For"} ${scopeText(r.filter || {})}: ${acts.join("; ")}.`;
}

const blank = (rules) => {
  let n = rules.length + 1;
  while (rules.some((r) => r.id === `rule-${n}`)) n++;
  return { id: `rule-${n}`, status: "Enabled", filter: {}, expiration: null, noncurrentExpiration: null, abortMultipartDays: 7, transitions: null, managed: false };
};

function numberField({ label, value, placeholder, onChange, className = "lcr-num", id }) {
  const input = h("input", { class: `input ${className}`, type: "text", inputmode: "numeric", autocomplete: "off", id, placeholder, "aria-label": label, value: value == null ? "" : String(value) });
  input.addEventListener("input", () => {
    const v = input.value.trim();
    onChange(v === "" ? undefined : /^\d+$/.test(v) ? Number(v) : NaN);
  });
  return input;
}

// Size with a unit: whole numbers only, so the byte count is exact.
function sizeField({ label, bytes, onChange }) {
  let unit = 0;
  if (bytes != null && bytes > 0) for (let i = UNITS.length - 1; i >= 0; i--) if (bytes % UNITS[i][1] === 0) { unit = i; break; }
  const input = h("input", { class: "input lcr-num", type: "text", inputmode: "numeric", autocomplete: "off", "aria-label": label, placeholder: "no limit", value: bytes == null ? "" : String(bytes / UNITS[unit][1]) });
  const sel = h("select", { class: "input lcr-unit", "aria-label": `${label}, unit` }, UNITS.map(([u], i) => h("option", { value: String(i), text: u, selected: i === unit })));
  const fire = () => {
    const v = input.value.trim();
    onChange(v === "" ? undefined : /^\d+$/.test(v) ? Number(v) * UNITS[Number(sel.value)][1] : NaN);
  };
  input.addEventListener("input", fire);
  sel.addEventListener("change", fire);
  return h("span", { class: "lcr-size" }, input, sel);
}

export async function lifecycleRulesDialog({ bucket }) {
  const id = `lcr${++seq}`;
  let state;
  try { state = await getLifecycleRules(bucket); } catch (err) { return { error: err }; }
  let rules = copy(state.rules);
  let busy = false;
  let changed = false;
  let draft = null;
  let versioning = state.versioning;
  const origins = () => new Map(state.rules.map((r) => [r.id, key(r)]));

  const error = h("p", { class: "qd-error", role: "alert", hidden: true });
  const showError = (m) => { error.textContent = m; error.hidden = !m; };
  const cards = h("ol", { class: "lcr-rules" });
  const empty = h("p", { class: "cors-empty", text: "No lifecycle rules. Files and old versions are kept until you delete them." });
  const jsonView = h("pre", { class: "cors-json", hidden: true, tabindex: "0", "aria-label": "Lifecycle rules as JSON" });
  const jsonBtn = h("button", { class: "btn btn-sm", type: "button", "aria-pressed": "false", text: "View JSON" });
  const addBtn = h("button", { class: "btn btn-sm", type: "button" }, icon("plus"), "Add rule");
  const save = h("button", { class: "btn btn-primary", type: "submit" }, icon("check"), "Save rules");
  const revert = h("button", { class: "btn", type: "button", text: "Discard changes" });
  const close = h("button", { class: "btn", type: "button", text: "Close" });
  const mine = h("textarea", { class: "pol-text pol-mine", readonly: true, rows: 6, "aria-label": "Your unsaved rules, JSON" });
  const useMine = h("button", { class: "btn btn-sm", type: "button", text: "Restore my version" });
  const copyMine = h("button", { class: "btn btn-sm", type: "button" }, icon("copy"), "Copy my rules");
  const conflict = h("section", { class: "pol-conflict", hidden: true, role: "alert" },
    h("b", { text: "The rules changed while you were editing" }),
    h("p", { text: "The editor now shows the current rules. Your unsaved version is kept below: restore it to save it over the current rules, or copy it." }),
    mine, h("div", { class: "pol-tools" }, useMine, copyMine));
  useMine.addEventListener("click", () => { rules = copy(draft).map((r) => ({ ...r, managed: state.rules.some((s) => s.id === r.id && s.managed) })); conflict.hidden = true; render(); });
  copyMine.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(mine.value); toast.show("Copied your rules."); } catch { toast.error("Could not copy. Select the text and copy it."); }
  });

  // What is wrong with a rule, before the server says it. Rules left as stored are never flagged.
  function problems(r) {
    const out = [];
    const o = origins();
    if (o.get(r.id) === key(r)) return out;
    if (!r.id) out.push("Give the rule a name.");
    else if (r.id.length > 255) out.push("The name can be at most 255 characters.");
    else if (!r.managed && r.id.startsWith("bucketeer-")) out.push("Names starting with bucketeer- are reserved for rules Bucketeer manages.");
    if (rules.filter((x) => x.id === r.id).length > 1) out.push("Another rule has the same name.");
    const f = r.filter || {};
    const days = (d, what) => { if (d !== undefined && (!isNum(d) || d < 1 || d > MAX_DAYS)) out.push(`${what}: enter a whole number of days from 1 to ${MAX_DAYS}.`); };
    const e = r.expiration || {};
    if (Object.hasOwn(e, "days")) {
      if (e.days === undefined) out.push("Delete files after: enter the number of days."); else days(e.days, "Delete files after");
    }
    if (Object.hasOwn(e, "date") && !/^\d{4}-\d{2}-\d{2}T00:00:00Z$/.test(e.date || "")) out.push("Delete files on: pick a date.");
    const n = r.noncurrentExpiration;
    if (n) {
      if (n.days === undefined) out.push("Delete old versions: enter the number of days."); else days(n.days, "Delete old versions after");
      if (n.newerNoncurrentVersions !== undefined && (!isNum(n.newerNoncurrentVersions) || n.newerNoncurrentVersions < 1 || n.newerNoncurrentVersions > MAX_NEWER)) out.push(`Versions to keep: a whole number from 1 to ${MAX_NEWER}.`);
    }
    if (r.abortMultipartDays === undefined) out.push("Unfinished uploads: enter the number of days."); else if (r.abortMultipartDays != null) days(r.abortMultipartDays, "Unfinished uploads");
    for (const k of ["sizeGreaterThan", "sizeLessThan"]) if (Object.hasOwn(f, k) && f[k] !== undefined && !isNum(f[k])) out.push("Sizes must be whole numbers.");
    if (isNum(f.sizeGreaterThan) && isNum(f.sizeLessThan) && f.sizeGreaterThan >= f.sizeLessThan) out.push("The minimum size must be smaller than the maximum.");
    if ((f.tags?.length || 0) > MAX_TAGS) out.push(`A rule can match at most ${MAX_TAGS} tags.`);
    const hasTagOrSize = f.tags?.length || isNum(f.sizeGreaterThan) || isNum(f.sizeLessThan);
    if (hasTagOrSize && isNum(r.abortMultipartDays)) out.push("Cleaning up unfinished uploads can only be limited by a prefix. Use a separate rule.");
    if (hasTagOrSize && e.expiredObjectDeleteMarker) out.push("Removing delete markers can only be limited by a prefix. Use a separate rule.");
    if (!(Object.keys(e).length || n || isNum(r.abortMultipartDays) || r.transitions)) out.push("Choose at least one action.");
    return out;
  }
  function advice(r) {
    if (r.status !== "Enabled") return [];
    const out = [];
    const e = r.expiration || {};
    const f = r.filter || {};
    const open = !f.prefix && !f.tags?.length && !isNum(f.sizeGreaterThan) && !isNum(f.sizeLessThan);
    if (open && (isNum(e.days) || e.date || r.noncurrentExpiration)) out.push("No filter: this rule applies to every file in the bucket.");
    if (r.noncurrentExpiration && versioning === "off") out.push("Versioning is off, so there are no old versions yet. The rule applies once you turn versioning on.");
    if (e.date && e.date < new Date().toISOString().slice(0, 10)) out.push("This date has passed, so matching files go at the next clean-up.");
    return out;
  }

  const dirty = () => JSON.stringify(rules.map(clean)) !== JSON.stringify(state.rules.map(clean));
  const syncers = [];
  function update() {
    const bad = rules.some((r) => problems(r).length);
    save.disabled = busy || !dirty() || bad;
    revert.disabled = busy || !dirty();
    addBtn.disabled = busy || rules.length >= MAX_RULES;
    empty.hidden = rules.length > 0;
    jsonView.textContent = JSON.stringify(rules.map(clean), null, 2);
    syncers.forEach((fn) => fn());
  }

  function tagEditor(r, onChange) {
    r.filter.tags ||= [];
    const list = h("ul", { class: "cors-chips", "aria-label": "Tags the file must have" });
    const k = h("input", { class: "input lcr-tagk", type: "text", placeholder: "tag key", autocomplete: "off", spellcheck: false, "aria-label": "Tag key" });
    const v = h("input", { class: "input lcr-tagv", type: "text", placeholder: "value", autocomplete: "off", spellcheck: false, "aria-label": "Tag value" });
    const add = h("button", { class: "btn btn-sm", type: "button" }, icon("plus"), "Add tag");
    const err = h("p", { class: "set-err", role: "alert", hidden: true });
    const paint = () => {
      list.replaceChildren(...r.filter.tags.map((t, i) => h("li", { class: "cors-chip" },
        h("span", { class: "cors-chip-t mono", text: `${t.key}=${t.value}` }),
        h("button", { class: "cors-chip-x", type: "button", "aria-label": `Remove tag ${t.key}`, on: { click: () => { r.filter.tags.splice(i, 1); paint(); onChange(); } } }, icon("x")))));
      list.hidden = !r.filter.tags.length;
    };
    const submit = () => {
      const key_ = k.value.trim();
      const val = v.value.trim();
      if (!key_ && !val) return true;
      const msg = !key_ ? "Enter a tag key." : key_.length > 128 || val.length > 256 || !TAG_CHARS.test(key_) || !TAG_CHARS.test(val) ? "Tags use letters, digits, spaces and + - = . _ : / @ (key up to 128, value up to 256)." : r.filter.tags.some((t) => t.key === key_) ? "A tag with that key is already listed." : r.filter.tags.length >= MAX_TAGS ? `At most ${MAX_TAGS} tags.` : "";
      err.textContent = msg;
      err.hidden = !msg;
      if (msg) return false;
      r.filter.tags.push({ key: key_, value: val });
      k.value = "";
      v.value = "";
      paint();
      onChange();
      return true;
    };
    add.addEventListener("click", () => { submit(); k.focus(); });
    for (const el of [k, v]) el.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); submit(); } });
    paint();
    return { el: h("div", { class: "lcr-tags" }, list, h("div", { class: "lcr-tagrow" }, k, h("span", { class: "lcr-eq", "aria-hidden": "true", text: "=" }), v, add), err), flush: submit };
  }

  let flushers = [];

  function ruleCard(r, i) {
    const rid = `${id}-r${i}`;
    r.filter ||= {};
    const transitions = !!r.transitions;
    const MK = { "bucketeer-expire-current": "cur", "bucketeer-expire-noncurrent": "old", "bucketeer-abort-multipart": "abort" };
    const kind = r.managed ? MK[r.id] : null;
    const frozen = () => transitions || (r.managed && (!r._unlocked || !kind));
    const title = h("b", { class: "lcr-title", text: r.id || "New rule" });
    const summary = h("p", { class: "lcr-sum", "aria-live": "polite" });
    const perr = h("p", { class: "set-err cors-rule-err", role: "alert", hidden: true });
    const problem = h("p", { class: "set-err lcr-problems", hidden: true });
    const warn = h("p", { class: "lcd-tip acl-warn lcr-warn", role: "note", hidden: true }, icon("alert", "icon"), h("span"));

    const name = h("input", { class: "input", type: "text", id: `${rid}-n`, autocomplete: "off", spellcheck: false, value: r.id, readOnly: r.managed });
    name.addEventListener("input", () => { r.id = name.value; title.textContent = r.id || "New rule"; update(); });
    const swLabel = h("span", { class: "switch-label", text: r.status === "Enabled" ? "Enabled" : "Paused" });
    const sw = h("button", { class: "switch lcr-sw", type: "button", role: "switch", "aria-checked": String(r.status === "Enabled"), "aria-label": `Rule ${r.id} enabled` },
      swLabel, h("span", { class: "switch-track", "aria-hidden": "true" }));
    sw.addEventListener("click", () => { r.status = r.status === "Enabled" ? "Disabled" : "Enabled"; sw.setAttribute("aria-checked", String(r.status === "Enabled")); card.dataset.status = r.status; swLabel.textContent = r.status === "Enabled" ? "Enabled" : "Paused"; update(); });

    // filter
    const prefix = h("input", { class: "input mono", type: "text", id: `${rid}-p`, autocomplete: "off", autocapitalize: "off", spellcheck: false, placeholder: "logs/ (leave empty for every file)", value: r.filter.prefix || "" });
    prefix.addEventListener("input", () => { r.filter.prefix = prefix.value; update(); });
    const tagsEd = tagEditor(r, update);
    flushers.push(tagsEd.flush);
    const min = sizeField({ label: "Larger than", bytes: r.filter.sizeGreaterThan, onChange: (n) => { if (n === undefined) delete r.filter.sizeGreaterThan; else r.filter.sizeGreaterThan = n; update(); } });
    const max = sizeField({ label: "Smaller than", bytes: r.filter.sizeLessThan, onChange: (n) => { if (n === undefined) delete r.filter.sizeLessThan; else r.filter.sizeLessThan = n; update(); } });

    // current-version expiration
    const e0 = r.expiration || {};
    const mode0 = Object.hasOwn(e0, "days") ? "days" : Object.hasOwn(e0, "date") ? "date" : e0.expiredObjectDeleteMarker ? "marker" : "";
    const modeSel = h("select", { class: "input", id: `${rid}-em`, "aria-label": "Deleting current files" },
      [["", "Keep files"], ["days", "Delete after N days"], ["date", "Delete on a date"], ["marker", "Remove leftover delete markers"]]
        .map(([val, text]) => h("option", { value: val, text, selected: val === mode0 })));
    const daysIn = numberField({ label: "Days until files are deleted", value: e0.days, onChange: (n) => { r.expiration = { days: n }; update(); }, id: `${rid}-ed` });
    const dateIn = h("input", { class: "input lcr-date", type: "date", id: `${rid}-edt`, "aria-label": "Date files are deleted", value: (e0.date || "").slice(0, 10) });
    dateIn.addEventListener("input", () => { r.expiration = { date: dateIn.value ? `${dateIn.value}T00:00:00Z` : "" }; update(); });
    const modeNote = h("small", { class: "cors-hint" });
    const expRow = h("div", { class: "lcr-inline" }, daysIn, h("span", { class: "lcd-unit", text: "days" }), dateIn);
    const syncMode = () => {
      const m = modeSel.value;
      daysIn.hidden = m !== "days";
      expRow.children[1].hidden = m !== "days";
      dateIn.hidden = m !== "date";
      expRow.hidden = m === "" || m === "marker";
      modeNote.textContent = m === "days" ? "Each file is deleted this many days after it was uploaded. With versioning on, it becomes an old version instead."
        : m === "date" ? "Everything matching is deleted on this date (midnight UTC), whenever it was uploaded."
        : m === "marker" ? "Cleans up delete markers that no longer hide any older version. Only a prefix filter works with this."
        : "Current files stay until you delete them.";
    };
    modeSel.addEventListener("change", () => {
      const m = modeSel.value;
      r.expiration = m === "days" ? { days: Number(daysIn.value) || undefined } : m === "date" ? { date: dateIn.value ? `${dateIn.value}T00:00:00Z` : "" } : m === "marker" ? { expiredObjectDeleteMarker: true } : null;
      syncMode();
      update();
    });

    // noncurrent versions
    const ncOn = h("input", { type: "checkbox", id: `${rid}-nc`, checked: !!r.noncurrentExpiration });
    const ncDays = numberField({ label: "Days until old versions are deleted", value: r.noncurrentExpiration?.days, onChange: (n) => { (r.noncurrentExpiration ||= {}).days = n; update(); } });
    const ncKeep = numberField({ label: "Old versions to keep", value: r.noncurrentExpiration?.newerNoncurrentVersions, placeholder: "none", onChange: (n) => { const c = (r.noncurrentExpiration ||= {}); if (n === undefined) delete c.newerNoncurrentVersions; else c.newerNoncurrentVersions = n; update(); } });
    const ncRow = h("div", { class: "lcr-inline" }, ncDays, h("span", { class: "lcd-unit", text: "days after replaced; always keep the newest" }), ncKeep, h("span", { class: "lcd-unit", text: "old versions" }));
    ncOn.addEventListener("change", () => { r.noncurrentExpiration = ncOn.checked ? { days: Number(ncDays.value) || 30 } : null; if (ncOn.checked && !ncDays.value) ncDays.value = "30"; ncRow.hidden = !ncOn.checked; update(); });
    ncRow.hidden = !ncOn.checked;
    syncMode();

    // unfinished uploads
    const abOn = h("input", { type: "checkbox", id: `${rid}-ab`, checked: r.abortMultipartDays != null });
    const abDays = numberField({ label: "Days until unfinished uploads are discarded", value: r.abortMultipartDays, onChange: (n) => { r.abortMultipartDays = n; update(); } });
    const abRow = h("div", { class: "lcr-inline" }, abDays, h("span", { class: "lcd-unit", text: "days after the upload started" }));
    abOn.addEventListener("change", () => { r.abortMultipartDays = abOn.checked ? (Number(abDays.value) || 7) : null; if (abOn.checked && !abDays.value) abDays.value = "7"; abRow.hidden = !abOn.checked; update(); });
    abRow.hidden = !abOn.checked;

    // Bucketeer's own rules can only change their number of days (or be removed).
    const dval = kind === "cur" ? r.expiration?.days : kind === "old" ? r.noncurrentExpiration?.days : r.abortMultipartDays;
    const dIn = numberField({ label: "Number of days", value: dval, id: `${rid}-md`, onChange: (n) => {
      if (kind === "cur") r.expiration = { days: n }; else if (kind === "old") r.noncurrentExpiration = { days: n }; else r.abortMultipartDays = n;
      update();
    } });
    const daysBody = h("fieldset", { class: "lcr-body", disabled: frozen(), "aria-label": `Number of days of rule ${r.id}` },
      h("div", { class: "field" }, h("label", { for: `${rid}-md`, text: kind === "cur" ? "Delete files after" : kind === "old" ? "Delete old versions after" : "Discard unfinished uploads after" }),
        h("div", { class: "lcr-inline lcr-solo" }, dIn, h("span", { class: "lcd-unit", text: kind === "abort" ? "days after the upload started" : "days" }))));
    const body = kind ? daysBody : h("fieldset", { class: "lcr-body", disabled: frozen(), "aria-label": `Settings of rule ${r.id}` },
      h("div", { class: "lcr-top" },
        h("div", { class: "field lcr-name" }, h("label", { for: `${rid}-n`, text: "Rule name" }), name),
        sw),
      h("section", { class: "lcr-sec" }, h("h3", { text: "Which files" }),
        h("div", { class: "field" }, h("label", { for: `${rid}-p`, text: "Name starts with (prefix)" }), prefix),
        h("div", { class: "field" }, h("label", { text: "Tagged with" }), tagsEd.el),
        h("div", { class: "lcr-sizes" },
          h("div", { class: "field" }, h("label", { text: "Larger than" }), min),
          h("div", { class: "field" }, h("label", { text: "Smaller than" }), max))),
      h("section", { class: "lcr-sec" }, h("h3", { text: "What happens" }),
        h("div", { class: "lcr-act" }, h("label", { for: `${rid}-em`, class: "lcr-act-h", text: "Current files" }), modeSel, expRow, modeNote),
        h("div", { class: "lcr-act" }, h("label", { class: "qd-check", for: `${rid}-nc` }, ncOn, h("span", { text: "Delete old versions" })), ncRow),
        h("div", { class: "lcr-act" }, h("label", { class: "qd-check", for: `${rid}-ab` }, abOn, h("span", { text: "Clean up unfinished uploads" })), abRow)));

    const lockBtn = r.managed ? h("button", { class: "btn btn-sm", type: "button", "aria-pressed": "false" }, icon("edit"), "Edit Bucketeer rule") : null;
    const del = h("button", { class: "btn btn-ghost btn-sm lcr-ibtn", type: "button", "aria-label": `Remove rule ${r.id}`, title: "Remove rule" }, icon("trash"));
    const dup = h("button", { class: "btn btn-ghost btn-sm lcr-ibtn", type: "button", "aria-label": `Duplicate rule ${r.id}`, title: "Duplicate rule" }, icon("copy"));
    const sync = () => {
      body.disabled = frozen();
      const locked = r.managed && !r._unlocked;
      if (lockBtn) { lockBtn.setAttribute("aria-pressed", String(!!r._unlocked)); lockBtn.replaceChildren(icon(r._unlocked ? "check" : "edit"), r._unlocked ? "Editing Bucketeer rule" : "Edit Bucketeer rule"); }
      del.disabled = busy || (r.managed && locked);
      dup.disabled = busy || rules.length >= MAX_RULES;
      summary.textContent = describe(r);
      const p = problems(r);
      problem.replaceChildren(...p.map((m) => h("span", { class: "cors-err-line", text: m })));
      problem.hidden = !p.length;
      const w = advice(r);
      warn.hidden = !w.length;
      warn.querySelector("span").textContent = w.join(" ");
    };
    syncers.push(sync);
    if (lockBtn) lockBtn.addEventListener("click", () => { r._unlocked = !r._unlocked; update(); if (r._unlocked) name.focus(); });
    del.addEventListener("click", async () => {
      if (transitions) {
        const ok = await confirmDialog({ title: ["Remove ", nameSpan(r.id), "?"], danger: true, confirmLabel: "Remove rule", message: "This rule moves files to another storage class. Removing it stops that for good, and Bucketeer cannot add it back." });
        if (!ok) return;
      }
      rules.splice(rules.indexOf(r), 1);
      render();
    });
    dup.addEventListener("click", () => {
      const c = { ...copy(clean(r)), transitions: null, managed: false };
      const base = `${r.id.replace(/^bucketeer-/, "")}-copy`;
      let n = 1;
      c.id = base;
      while (rules.some((x) => x.id === c.id)) c.id = `${base}${++n}`;
      rules.splice(rules.indexOf(r) + 1, 0, c);
      render();
      cards.children[rules.indexOf(c)]?.querySelector("input")?.focus();
    });

    const card = h("li", { class: "lcr-rule", "data-status": r.status, "data-managed": String(!!r.managed), "data-ro": String(transitions) },
      h("div", { class: "lcr-head" },
        h("span", { class: "lcr-ic", "aria-hidden": "true" }, icon(r.managed ? "lock" : "refresh")),
        title,
        r.managed ? h("span", { class: "chip chip-accent", title: "The simple Lifecycle dialog manages this rule." }, icon("lock", "icon"), "Bucketeer rule") : null,
        transitions ? h("span", { class: "chip", text: "Has transitions" }) : null,
        h("span", { class: "pol-spacer" }), lockBtn, dup, del),
      r.managed ? h("p", { class: "lcd-tip", role: "note" }, icon("shield", "icon"), h("span", { text: "The simple Lifecycle dialog keeps this rule up to date. Press “Edit Bucketeer rule” to change its number of days or remove it; saving asks again. For anything else, create your own rule." })) : null,
      transitions ? h("p", { class: "lcd-tip", role: "note" }, icon("alert", "icon"), h("span", { text: "This rule moves files to another storage class. Bucketeer cannot create or change those transitions yet, so this rule is read only and is kept exactly as it is." })) : null,
      summary, body, warn, problem, perr);
    return card;
  }

  function render() {
    flushers = [];
    syncers.length = 0;
    cards.replaceChildren(...rules.map(ruleCard));
    showError("");
    update();
  }

  function showFieldErrors(errors) {
    const general = [];
    const per = new Map();
    for (const e of errors) {
      if (e.rule && cards.children[e.rule - 1]) per.set(e.rule, [...(per.get(e.rule) || []), e.message]);
      else general.push(e.message);
    }
    for (const [n, msgs] of per) {
      const slot = cards.children[n - 1].querySelector(".cors-rule-err");
      slot.replaceChildren(...msgs.map((m) => h("span", { class: "cors-err-line", text: m })));
      slot.hidden = false;
    }
    if (general.length) showError(general.join(" "));
  }

  jsonBtn.addEventListener("click", () => {
    const on = jsonView.hidden;
    jsonView.hidden = !on;
    cards.hidden = on;
    jsonBtn.setAttribute("aria-pressed", String(on));
    jsonBtn.textContent = on ? "Back to editor" : "View JSON";
  });
  addBtn.addEventListener("click", () => {
    rules.push(blank(rules));
    render();
    const last = cards.lastElementChild;
    last.scrollIntoView({ block: "nearest" });
    last.querySelector("input")?.focus();
  });
  revert.addEventListener("click", () => { rules = copy(state.rules); conflict.hidden = true; render(); });

  // Deleting data later is irreversible, so say so once before saving rules that start doing it.
  function startsDeleting() {
    const o = origins();
    return rules.filter((r) => r.status === "Enabled" && o.get(r.id) !== key(r) && (r.expiration?.days != null || r.expiration?.date || r.noncurrentExpiration)).map((r) => r.id);
  }

  function idle() {
    busy = false;
    save.replaceChildren(icon("check"), "Save rules");
    update();
  }

  async function send(confirmManaged) {
    busy = true;
    showError("");
    cards.querySelectorAll(".cors-rule-err").forEach((n) => { n.hidden = true; n.replaceChildren(); });
    save.replaceChildren(h("span", { class: "spinner", "aria-hidden": "true" }), "Saving…");
    update();
    try {
      const res = await putLifecycleRules(bucket, { rules: rules.map(clean), etag: state.etag, ...(confirmManaged ? { confirmManaged: true } : {}) });
      state = res;
      versioning = res.versioning;
      rules = copy(res.rules);
      changed = changed || res.changed;
      render();
      toast.show(res.changed ? (res.rules.length ? `Lifecycle rules saved for ${bucket}.` : `Lifecycle rules of ${bucket} removed.`) : "Nothing to change.");
      for (const w of res.warnings) toast.show(w.message, { timeout: 9000 });
    } catch (err) {
      if (err.status === 401) { dlg.close(); return; }
      if (err.status === 409 && err.body?.requiresConfirmation) {
        idle();
        const ok = await confirmDialog({
          title: "Change Bucketeer rules?", confirmLabel: "Save changes", danger: true,
          message: `These rules are managed by the simple Lifecycle dialog: ${err.body.managedChanges.join(", ")}. Changing or removing them here can make that dialog show different values.`,
        });
        if (ok) return send(true);
      } else if (err.status === 409 && err.body?.etagMismatch) {
        draft = rules.map(clean);
        mine.value = JSON.stringify(draft, null, 2);
        state = err.body.current;
        versioning = state.versioning;
        rules = copy(state.rules);
        conflict.hidden = false;
        render();
      } else if (err.status === 422 && err.body?.errors) {
        showFieldErrors(err.body.errors);
        if (err.body.errors.some((x) => x.rule)) showError("Fix the marked rules and save again.");
      } else showError(err.message || "Could not save the rules.");
    } finally {
      idle();
    }
  }

  const form = h("form", { class: "pol-form lcr-form", novalidate: true },
    conflict,
    h("p", { class: "lcd-tip", role: "note" }, icon("shield", "icon"),
      h("span", { text: "Rules delete files for good once their time is up. Check the plain-language sentence on each rule, and use a filter unless you really mean the whole bucket." })),
    h("div", { class: "pol-bar" }, h("div", { class: "pol-tools" }, addBtn), h("div", { class: "pol-tools" }, jsonBtn)),
    empty, cards, jsonView, error,
    h("div", { class: "cd-actions pol-actions" }, revert, h("span", { class: "pol-spacer" }), close, save));

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (busy) return;
    for (const f of flushers) if (!f()) return;
    if (save.disabled) return;
    const doomed = startsDeleting();
    if (doomed.length) {
      const ok = await confirmDialog({
        title: "Delete files automatically?", confirmLabel: "Save rules", danger: true,
        message: `${doomed.length === 1 ? "The rule" : "The rules"} ${doomed.join(", ")} will delete files or versions for good once they are old enough. Deleted data cannot be brought back.`,
      });
      if (!ok) return;
    }
    send(false);
  });

  const dlg = h("dialog", { class: "cd qd pol lcr", "aria-labelledby": `${id}-t`, "aria-describedby": `${id}-m` },
    h("div", { class: "pol-body" },
      h("div", { class: "qd-head" },
        h("h2", { id: `${id}-t` }, "Lifecycle rules of ", nameSpan(bucket)),
        h("p", { class: "cd-msg", id: `${id}-m`, text: "Full control over what is deleted and when. This is an Advanced tool; the simple Lifecycle dialog covers the common cases." })),
      form));
  render();
  close.addEventListener("click", () => dlg.close());
  dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });

  return new Promise((resolve) => {
    dlg.addEventListener("close", () => { dlg.remove(); resolve({ changed }); });
    document.body.append(dlg);
    dlg.showModal();
    close.focus({ preventScroll: true });
    dlg.querySelector(".pol-body").scrollTop = 0;
  });
}
