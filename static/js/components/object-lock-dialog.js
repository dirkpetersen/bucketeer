import { h } from "../dom.js";
import { icon } from "../icons.js";
import {
  getObjectLockDefault, putObjectLockDefault, getObjectLock, putObjectRetention, putObjectLegalHold,
} from "../api.js";
import { toast } from "./toast.js";
import { confirmDialog, nameSpan } from "./confirm-dialog.js";
import { keyPicker } from "./key-picker.js";

// objectLockDialog({ bucket }) -> Promise<{ error? } | { changed }>
// Advanced only; the bucket must have been created with object lock (the list says so).
// Tab 1: default retention for new files. Tab 2: retention and legal hold of ONE file chosen by
// key (autocomplete, not a file browser; version optional). The server decides every rule; the
// dialog only asks for the explicit OKs it answers with 409 (compliance, bypass governance).
let seq = 0;
const MODES = {
  GOVERNANCE: { label: "Governance", hint: "Protects files from being overwritten or deleted. You, as the bucket owner, can still shorten or lift it, after an extra confirmation." },
  COMPLIANCE: { label: "Compliance", hint: "Nobody can delete the file or shorten the lock until it expires. Not you, not an administrator." },
};
const when = (iso) => new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
const pad = (n) => String(n).padStart(2, "0");
const dateValue = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const period = (d) => (d ? `${d.days ?? d.years} ${d.days ? (d.days === 1 ? "day" : "days") : (d.years === 1 ? "year" : "years")}` : "");

const COMPLIANCE_DEFAULT_WARN = "Compliance cannot be undone for the files it covers. Every file written while this default is set can't be deleted by anyone, administrators included, until its retention has expired. Turning the default off later does not release those files.";
const COMPLIANCE_OBJECT_WARN = "Compliance cannot be undone. Nobody, administrators included, can delete this file or move the date earlier until the date has passed. It can only be extended.";

export async function objectLockDialog({ bucket }) {
  const id = `olk${++seq}`;
  let state;
  try { state = await getObjectLockDefault(bucket); } catch (err) { return { error: err }; }
  let busy = false;
  let changed = false;

  // ---------- tab 1: default retention ----------
  let draft = { mode: state.defaultRetention?.mode ?? "", unit: state.defaultRetention?.years ? "years" : "days", n: String(state.defaultRetention?.days ?? state.defaultRetention?.years ?? 30) };
  const dErr = h("p", { class: "qd-error", role: "alert", hidden: true });
  const showD = (m) => { dErr.textContent = m; dErr.hidden = !m; };
  const dWarn = h("p", { class: "lcd-tip acl-warn", role: "note", hidden: true }, icon("alert", "icon"), h("span", { text: COMPLIANCE_DEFAULT_WARN }));
  const dSave = h("button", { class: "btn btn-primary", type: "submit" }, icon("check"), "Save default");
  const dNow = h("p", { class: "olk-now" });
  const num = h("input", { class: "input lcd-days", type: "text", inputmode: "numeric", autocomplete: "off", id: `${id}-n`, "aria-label": "Retention period", "aria-describedby": `${id}-ne` });
  const unit = h("select", { class: "input olk-unit", "aria-label": "Unit" }, h("option", { value: "days", text: "days" }), h("option", { value: "years", text: "years" }));
  const nErr = h("p", { class: "set-err", id: `${id}-ne`, role: "alert", hidden: true });
  const periodRow = h("div", { class: "olk-period" }, h("label", { for: num.id, text: "Keep each new file locked for" }), h("div", { class: "olk-period-in" }, num, unit), nErr);
  const radios = [["", "No default", "New files are not locked unless the upload asks for it."], ...Object.entries(MODES).map(([m, v]) => [m, v.label, v.hint])]
    .map(([value, title, hint]) => {
      const input = h("input", { type: "radio", name: `${id}-mode`, value, checked: value === draft.mode });
      input.addEventListener("change", () => { draft.mode = value; showD(""); syncD(); });
      return h("label", { class: "qd-check bkd-opt", "data-danger": String(value === "COMPLIANCE") }, input, h("span", {}, h("b", { text: title }), h("small", { text: hint })));
    });

  const parsed = () => {
    if (!draft.mode) return { ok: true, value: null };
    const n = Number(draft.n);
    const max = draft.unit === "years" ? 100 : 36500;
    if (!/^\d+$/.test(draft.n.trim()) || n < 1 || n > max) return { ok: false, message: `Enter a whole number of ${draft.unit} from 1 to ${max}.` };
    return { ok: true, value: { mode: draft.mode, [draft.unit]: n } };
  };
  const sameDefault = () => JSON.stringify(parsed().value) === JSON.stringify(state.defaultRetention);

  function syncD(showErr = false) {
    const p = parsed();
    periodRow.hidden = !draft.mode;
    dWarn.hidden = draft.mode !== "COMPLIANCE";
    nErr.hidden = p.ok || !showErr;
    nErr.textContent = p.ok ? "" : p.message;
    num.toggleAttribute("aria-invalid", !p.ok && showErr);
    dSave.disabled = busy || (p.ok && sameDefault());
    const cur = state.defaultRetention;
    dNow.textContent = cur ? `Currently: ${MODES[cur.mode].label}, ${period(cur)}.` : "Currently: no default retention.";
  }
  num.value = draft.n;
  unit.value = draft.unit;
  num.addEventListener("input", () => { draft.n = num.value; showD(""); syncD(true); });
  unit.addEventListener("change", () => { draft.unit = unit.value; showD(""); syncD(true); });

  const dForm = h("form", { class: "olk-form", novalidate: true },
    h("p", { class: "cd-msg", text: "Applies to files uploaded from now on. Files that are already in the bucket keep the lock they have." }),
    dNow,
    h("fieldset", { class: "bkd-access" }, h("legend", { class: "label", text: "Default mode" }), ...radios),
    periodRow, dWarn, dErr,
    h("div", { class: "cd-actions" }, dSave));

  dForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (busy || dSave.disabled) return;
    const p = parsed();
    if (!p.ok) { syncD(true); num.focus(); return; }
    let confirm = false;
    if (p.value?.mode === "COMPLIANCE") {
      confirm = await confirmDialog({
        title: ["Set a Compliance default on ", nameSpan(bucket), "?"], confirmLabel: "Set Compliance default", danger: true, requireText: bucket,
        message: COMPLIANCE_DEFAULT_WARN,
      });
      if (!confirm) return;
    }
    busy = true;
    showD("");
    dSave.replaceChildren(h("span", { class: "spinner", "aria-hidden": "true" }), "Saving…");
    syncD();
    try {
      const res = await putObjectLockDefault(bucket, { defaultRetention: p.value, etag: state.etag, confirm });
      state = res;
      changed = changed || res.changed;
      toast.show(res.changed ? `Default retention of ${bucket} saved.` : "Nothing to change.");
    } catch (err) {
      if (err.status === 401) { dlg.close(); return; }
      if (err.body?.etagMismatch) {
        state = err.body.current;
        draft = { mode: state.defaultRetention?.mode ?? "", unit: state.defaultRetention?.years ? "years" : "days", n: String(state.defaultRetention?.days ?? state.defaultRetention?.years ?? draft.n) };
        radios.forEach((l) => { const i = l.querySelector("input"); i.checked = i.value === draft.mode; });
        num.value = draft.n;
        unit.value = draft.unit;
      }
      showD(err.message || "Could not save the default retention.");
    } finally {
      busy = false;
      dSave.replaceChildren(icon("check"), "Save default");
      syncD();
    }
  });

  // ---------- tab 2: one object ----------
  let obj = null; // {key, versionId, retention, legalHold} as loaded
  const oErr = h("p", { class: "qd-error", role: "alert", hidden: true });
  const showO = (m) => { oErr.textContent = m; oErr.hidden = !m; };
  const picker = keyPicker({
    id, bucket,
    onChange: () => { showO(""); obj = null; panel.hidden = true; syncLoad(); },
    onError: showO,
    onUnauthorized: () => dlg.close(),
    onEnter: () => loadBtn.click(),
  });
  const version = h("input", { class: "input mono-input", id: `${id}-v`, type: "text", autocomplete: "off", autocapitalize: "off", spellcheck: "false", maxlength: 1024, placeholder: "latest version", "aria-describedby": `${id}-vh` });
  version.addEventListener("input", () => { showO(""); obj = null; panel.hidden = true; syncLoad(); });
  const loadBtn = h("button", { class: "btn", type: "button", disabled: true }, icon("search"), "Show lock");
  const syncLoad = () => { loadBtn.disabled = busy || !picker.input.value.trim() || picker.input.value.endsWith("/"); };

  const status = h("div", { class: "olk-status", "aria-live": "polite" });
  const hold = h("button", { class: "switch olk-hold", type: "button", role: "switch", "aria-checked": "false" }, h("span", { class: "switch-label", text: "Legal hold" }), h("span", { class: "switch-track", "aria-hidden": "true" }));
  const holdNote = h("p", { class: "set-hint", text: "A legal hold blocks deleting or overwriting this version until you switch it off. It has no end date and can be lifted at any time." });
  const modeSel = h("select", { class: "input olk-mode", id: `${id}-m`, "aria-label": "Lock mode" }, ...Object.entries(MODES).map(([v, m]) => h("option", { value: v, text: m.label })));
  const date = h("input", { class: "input olk-date", id: `${id}-d`, type: "date", "aria-label": "Keep locked until" });
  const rWarn = h("p", { class: "lcd-tip acl-warn", role: "note", hidden: true }, icon("alert", "icon"), h("span", { text: COMPLIANCE_OBJECT_WARN }));
  const rSet = h("button", { class: "btn btn-primary", type: "button" }, icon("check"), "Set retention");
  const rClear = h("button", { class: "btn btn-quiet-danger", type: "button" }, icon("trash"), "Remove retention");
  const rForm = h("div", { class: "olk-rform" },
    h("h3", { text: "Retention" }),
    h("p", { class: "olk-hint", id: `${id}-rh`, text: "The file can't be deleted or overwritten until this date. Extending is always possible." }),
    h("div", { class: "olk-rrow" }, h("div", { class: "field" }, h("label", { for: modeSel.id, text: "Mode" }), modeSel), h("div", { class: "field" }, h("label", { for: date.id, text: "Locked until" }), date)),
    rWarn, h("div", { class: "cd-actions olk-ra" }, rClear, rSet));
  const panel = h("div", { class: "olk-panel", hidden: true }, status, rForm,
    h("div", { class: "olk-holdrow" }, hold, holdNote));

  function paintObj() {
    const r = obj.retention;
    status.replaceChildren(
      h("p", { class: "olk-file" }, icon("lock", "icon"), h("code", { text: obj.key }), vid() ? h("span", { class: "chip", title: vid(), text: "Specific version" }) : null),
      h("p", { class: "olk-line" }, h("b", { text: "Retention: " }), r ? `${MODES[r.mode].label} until ${when(r.retainUntil)}` : "none"),
      h("p", { class: "olk-line" }, h("b", { text: "Legal hold: " }), obj.legalHold === "ON" ? "on" : "off"));
    hold.setAttribute("aria-checked", String(obj.legalHold === "ON"));
    const compliance = r?.mode === "COMPLIANCE";
    modeSel.value = r?.mode ?? "GOVERNANCE";
    modeSel.disabled = compliance;
    const tomorrow = new Date(Date.now() + 86400000);
    const floor = r ? new Date(r.retainUntil) : tomorrow;
    date.min = dateValue(compliance ? floor : tomorrow);
    date.value = r ? dateValue(new Date(r.retainUntil)) : dateValue(new Date(Date.now() + 30 * 86400000));
    rClear.hidden = !r || compliance;
    rSet.firstChild.nextSibling.textContent = r ? "Change retention" : "Set retention";
    syncRForm();
    panel.hidden = false;
  }
  function syncRForm() {
    rWarn.hidden = modeSel.value !== "COMPLIANCE";
    rSet.disabled = busy || !date.value;
    rClear.disabled = busy;
    hold.disabled = busy;
  }
  modeSel.addEventListener("change", syncRForm);
  date.addEventListener("input", () => { showO(""); syncRForm(); });

  const vid = () => version.value.trim() || undefined;

  async function loadObject() {
    if (busy || loadBtn.disabled) return;
    busy = true;
    showO("");
    picker.close();
    syncLoad();
    const key = picker.input.value;
    try {
      obj = await getObjectLock(bucket, key, vid());
      paintObj();
    } catch (err) {
      if (err.status === 401) { dlg.close(); return; }
      panel.hidden = true;
      showO(err.message || "Could not read the lock.");
    } finally {
      busy = false;
      syncLoad();
      if (obj) syncRForm();
    }
  }
  loadBtn.addEventListener("mousedown", (e) => e.preventDefault()); // keep the field focused: closing its list would move the button under the pointer
  loadBtn.addEventListener("click", loadObject);

  // Runs one retention write; answers the server's explicit OK requests (compliance, bypass).
  async function writeRetention(body) {
    busy = true;
    showO("");
    syncRForm();
    try {
      let extra = {};
      for (let attempt = 0; attempt < 3; attempt++) {
        try {
          const res = await putObjectRetention(bucket, obj.key, vid(), { ...body, ...extra });
          obj = res;
          obj.key = res.key;
          changed = changed || res.changed;
          paintObj();
          toast.show(res.changed ? "Retention saved." : "Nothing to change.");
          return;
        } catch (err) {
          if (err.status === 401) { dlg.close(); return; }
          const b = err.body || {};
          if (err.status === 409 && (b.requiresBypass || b.requiresConfirmation)) {
            const bypass = !!b.requiresBypass;
            const ok = await confirmDialog({
              title: bypass ? "Bypass governance retention?" : "Lock in Compliance mode?",
              confirmLabel: bypass ? "Bypass and continue" : "Lock in Compliance mode", danger: true,
              message: [bypass ? "This file is locked in Governance mode. Shortening or removing the lock overrides the protection that was put in place, and the change is recorded in the audit log. Only continue if you are sure." : "", b.requiresConfirmation ? COMPLIANCE_OBJECT_WARN : ""].filter(Boolean).join(" "),
            });
            if (!ok) return;
            extra = { ...extra, ...(b.requiresBypass ? { bypassGovernance: true } : {}), ...(b.requiresConfirmation ? { confirm: true } : {}) };
            continue;
          }
          showO(err.message || "Could not change the retention.");
          return;
        }
      }
    } finally {
      busy = false;
      syncRForm();
    }
  }

  rSet.addEventListener("click", () => {
    if (busy || !date.value) return;
    const [y, m, d] = date.value.split("-").map(Number);
    const until = new Date(y, m - 1, d, 23, 59, 0); // end of the chosen local day
    if (until.getTime() <= Date.now()) { showO("Pick a day in the future."); return; }
    writeRetention({ mode: modeSel.value, retainUntil: until.toISOString() });
  });
  rClear.addEventListener("click", () => { if (!busy) writeRetention({ mode: null }); });

  hold.addEventListener("click", async () => {
    if (busy || !obj) return;
    const next = obj.legalHold === "ON" ? "OFF" : "ON";
    busy = true;
    showO("");
    syncRForm();
    try {
      const res = await putObjectLegalHold(bucket, obj.key, vid(), next);
      obj = res;
      changed = changed || res.changed;
      paintObj();
      toast.show(`Legal hold ${next === "ON" ? "switched on" : "switched off"}.`);
    } catch (err) {
      if (err.status === 401) { dlg.close(); return; }
      showO(err.message || "Could not change the legal hold.");
    } finally {
      busy = false;
      syncRForm();
    }
  });

  const oForm = h("div", { class: "olk-object" },
    h("p", { class: "cd-msg", text: "Pick one file by name. Bucketeer shows its lock and lets you extend the retention or place a legal hold. It does not list or open your files." }),
    h("div", { class: "field" }, h("label", { for: picker.input.id, text: "File name (key)" }), ...picker.nodes),
    h("div", { class: "field" }, h("label", { for: version.id, text: "Version ID (optional)" }), version,
      h("p", { class: "set-hint", id: `${id}-vh`, text: "Leave empty for the newest version." })),
    h("div", { class: "acl-load" }, loadBtn), oErr, panel);

  // ---------- tabs ----------
  const TABS = [["default", "Default retention", dForm], ["object", "One file", oForm]];
  const tabBtns = TABS.map(([key, label]) => h("button", { class: "tab", type: "button", role: "tab", id: `${id}-tab-${key}`, "aria-controls": `${id}-panel` }, label));
  const body = h("div", { id: `${id}-panel`, role: "tabpanel", class: "olk-body" });
  let tab = "default";
  function show(key) {
    tab = key;
    TABS.forEach(([k, , el], i) => {
      tabBtns[i].setAttribute("aria-selected", String(k === key));
      tabBtns[i].tabIndex = k === key ? 0 : -1;
      el.hidden = k !== key;
    });
    body.setAttribute("aria-labelledby", `${id}-tab-${key}`);
  }
  tabBtns.forEach((b, i) => b.addEventListener("click", () => { show(TABS[i][0]); }));
  const tablist = h("div", { class: "tabs olk-tabs", role: "tablist", "aria-label": "Object lock" }, tabBtns);
  tablist.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    const next = (TABS.findIndex(([k]) => k === tab) + (e.key === "ArrowRight" ? 1 : -1) + TABS.length) % TABS.length;
    show(TABS[next][0]);
    tabBtns[next].focus();
  });
  body.append(...TABS.map(([, , el]) => el));

  const close = h("button", { class: "btn", type: "button", text: "Close" });
  const dlg = h("dialog", { class: "cd qd bkd olk", "aria-labelledby": `${id}-t` },
    h("div", { class: "bkd-body" },
      h("div", { class: "qd-head" },
        h("h2", { id: `${id}-t` }, "Object lock of ", nameSpan(bucket)),
        h("p", { class: "cd-msg", text: state.enabled ? "Object lock makes files tamper-proof for a set time. It was enabled when this bucket was created and cannot be turned off." : "This bucket was created without object lock. It can't be added later." })),
      state.enabled ? tablist : null,
      state.enabled ? body : h("p", { class: "lcd-tip", role: "note" }, icon("alert", "icon"), "Nothing to configure here."),
      h("div", { class: "cd-actions" }, close)));
  close.addEventListener("click", () => dlg.close());
  dlg.addEventListener("cancel", (e) => { if (busy) e.preventDefault(); });
  dlg.addEventListener("click", (e) => { if (e.target === dlg && !busy) dlg.close(); });
  show("default");
  syncD();

  return new Promise((resolve) => {
    dlg.addEventListener("close", () => { picker.dispose(); dlg.remove(); resolve({ changed }); });
    document.body.append(dlg);
    dlg.showModal();
    close.focus();
  });
}
