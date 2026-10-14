import { h } from "../dom.js";
import { icon } from "../icons.js";
import { getBucketLifecycle, setBucketLifecycle } from "../api.js";
import { toast } from "./toast.js";
import { confirmDialog, nameSpan } from "./confirm-dialog.js";

// lifecycleDialog({ bucket }) -> Promise<{ error? } | { changed: boolean }>
// The three simple lifecycle rules of one bucket, loaded on demand. Deleting rules is
// destructive, so enabling or shortening "delete after N days" asks first and sends
// confirm:true; the server enforces that too. Rules set outside Bucketeer are only counted.
let seq = 0;
const MAX_DAYS = 36500;
const DEFS = [
  { key: "expireCurrentDays", title: "Delete files after a number of days", def: 365, danger: true,
    hint: "Files are deleted automatically this many days after they were uploaded." },
  { key: "expireNoncurrentDays", title: "Delete old versions after a number of days", def: 30,
    hint: "Only for versioned buckets: replaced or deleted versions are removed this many days after they stopped being the current one." },
  { key: "abortMultipartDays", title: "Clean up unfinished uploads", def: 7,
    hint: "Large uploads that never finished are discarded after this many days, so they stop using space." },
];

export async function lifecycleDialog({ bucket }) {
  const id = `lc${++seq}`;
  let state;
  try {
    state = await getBucketLifecycle(bucket);
  } catch (err) {
    return { error: err };
  }
  let changed = false;
  let busy = false;
  const rows = {};

  const error = h("p", { class: "qd-error", role: "alert", hidden: true });
  const showError = (m) => { error.textContent = m; error.hidden = !m; };

  function daysOf(key) {
    const r = rows[key];
    if (!r.check.checked) return null;
    const v = r.input.value.trim();
    return /^\d+$/.test(v) ? Number(v) : NaN;
  }
  const problem = (key) => {
    const d = daysOf(key);
    if (d === null) return "";
    return Number.isNaN(d) || d < 1 || d > MAX_DAYS ? `Enter a whole number of days from 1 to ${MAX_DAYS}.` : "";
  };

  for (const def of DEFS) {
    const on = state[def.key] != null;
    const check = h("input", { type: "checkbox", id: `${id}-${def.key}`, checked: on, "aria-describedby": `${id}-${def.key}-h` });
    const input = h("input", {
      class: "input lcd-days", type: "text", inputmode: "numeric", autocomplete: "off", value: String(state[def.key] ?? def.def),
      "aria-label": `${def.title}: days`, "aria-describedby": `${id}-${def.key}-e`, disabled: !on,
    });
    const err = h("p", { class: "set-err", id: `${id}-${def.key}-e`, role: "alert", hidden: true });
    const sync = () => {
      input.disabled = !check.checked;
      const m = problem(def.key);
      err.textContent = m;
      err.hidden = !m;
      input.setAttribute("aria-invalid", String(!!m));
      showError("");
      update();
    };
    check.addEventListener("change", sync);
    input.addEventListener("input", sync);
    rows[def.key] = { def, check, input, err, el: h("div", { class: "lcd-row", "data-danger": String(!!def.danger) },
      h("label", { class: "qd-check lcd-check", for: check.id }, check,
        h("span", {}, h("b", { text: def.title }), h("small", { id: `${id}-${def.key}-h`, text: def.hint }))),
      h("div", { class: "lcd-days-wrap" }, input, h("span", { class: "lcd-unit", text: "days" })),
      err) };
  }

  const save = h("button", { class: "btn btn-primary", type: "submit" }, icon("check"), "Save");
  const cancel = h("button", { class: "btn", type: "button", text: "Cancel" });
  function current() {
    return Object.fromEntries(DEFS.map((d) => [d.key, daysOf(d.key)]));
  }
  function dirty() {
    return DEFS.some((d) => !Object.is(daysOf(d.key), state[d.key] ?? null));
  }
  function update() {
    save.disabled = busy || !dirty() || DEFS.some((d) => problem(d.key));
    suggestion.hidden = !(state.versioning === "enabled" && !rows.expireNoncurrentDays.check.checked && state.expireNoncurrentDays == null);
  }

  const suggestion = h("p", { class: "lcd-tip", role: "note", hidden: true }, icon("alert", "icon"),
    "Versioning is on, so old versions pile up and count toward your quota. Consider deleting old versions after 30 days or so.");
  const foreign = state.foreignRules
    ? h("p", { class: "lcd-foreign", text: `${state.foreignRules} other ${state.foreignRules === 1 ? "rule is" : "rules are"} managed outside Bucketeer. They are kept as they are.` })
    : null;

  const form = h("form", { class: "lcd-form", novalidate: true },
    DEFS.map((d) => rows[d.key].el), suggestion, foreign, error,
    h("div", { class: "cd-actions" }, cancel, save));

  async function send(rules, confirm) {
    return setBucketLifecycle(bucket, { ...rules, ...(confirm ? { confirm: true } : {}) });
  }

  // Ask before files are deleted. Resolves null (declined) or { alsoNoncurrent } (the
  // optional "also delete old versions" box, offered on versioned buckets with no such rule).
  async function askDelete(now) {
    const offer = state.versioning === "enabled" && state.expireNoncurrentDays == null && !rows.expireNoncurrentDays.check.checked;
    const box = h("input", { type: "checkbox", checked: true, id: `${id}-also` });
    const message = h("span", {},
      `Every file older than ${now} days will be deleted permanently, including files already there. `,
      state.versioning === "enabled" ? "Because versioning is on, they become old versions first and are removed for good only when old versions are deleted too. " : "",
      "It cannot be undone.",
      offer && h("label", { class: "qd-check lcd-also", for: box.id }, box,
        h("span", {}, `Also delete old versions after ${Math.min(now, 30)} days`)));
    const ok = await confirmDialog({
      title: ["Delete files in ", nameSpan(bucket), " after ", `${now} days?`], danger: true,
      confirmLabel: "Delete old files automatically", message,
    });
    return ok ? { alsoNoncurrent: offer && box.checked ? Math.min(now, 30) : null } : null;
  }

  // Show what the server has now (rules changed elsewhere), discarding unsaved edits.
  async function reloadState() {
    state = await getBucketLifecycle(bucket);
    for (const def of DEFS) {
      const r = rows[def.key];
      r.check.checked = state[def.key] != null;
      r.input.value = String(state[def.key] ?? def.def);
      r.input.disabled = !r.check.checked;
      r.err.hidden = true;
    }
    update();
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (busy || save.disabled) return;
    const rules = current();
    const was = state.expireCurrentDays;
    const now = rules.expireCurrentDays;
    const destructive = now != null && (was == null || now < was);
    if (destructive) {
      const choice = await askDelete(now);
      if (!choice) return;
      if (choice.alsoNoncurrent) {
        rules.expireNoncurrentDays = choice.alsoNoncurrent;
        rows.expireNoncurrentDays.check.checked = true;
        rows.expireNoncurrentDays.input.value = String(choice.alsoNoncurrent);
        rows.expireNoncurrentDays.input.disabled = false;
      }
    }
    busy = true;
    save.disabled = true;
    save.replaceChildren(h("span", { class: "spinner", "aria-hidden": "true" }), "Saving…");
    try {
      let res;
      try {
        res = await send(rules, destructive);
      } catch (err) {
        if (!(err.status === 409 && err.body?.requiresConfirmation) || destructive) throw err;
        // The rules changed elsewhere: show the current ones and write nothing; Save asks again.
        await reloadState();
        // Only the delete-after rule the user was editing keeps their value, so Save can ask for confirmation.
        const r = rows.expireCurrentDays;
        r.check.checked = now != null;
        r.input.value = String(now ?? r.def.def);
        r.input.disabled = now == null;
        update();
        toast.show("Rules were changed elsewhere. Review them and press Save again.", { timeout: 9000 });
        return;
      }
      state = { ...state, ...res };
      changed = changed || res.changed;
      toast.show(res.changed ? `Lifecycle rules saved for ${bucket}.` : "Nothing to change.");
      if (res.hint) toast.show(res.hint, { timeout: 9000 });
      dlg.close();
    } catch (err) {
      if (err.status === 401) { dlg.close(); return; }
      showError(err.message || "Could not save the rules.");
    } finally {
      busy = false;
      save.replaceChildren(icon("check"), "Save");
      update();
    }
  });

  const dlg = h("dialog", { class: "cd qd lcd", "aria-labelledby": `${id}-t`, "aria-describedby": `${id}-m` },
    h("div", { class: "lcd-body" },
      h("div", { class: "qd-head" },
        h("h2", { id: `${id}-t` }, "Lifecycle of ", nameSpan(bucket)),
        h("p", { class: "cd-msg", id: `${id}-m`, text: "Let Bucketeer tidy this bucket for you. Each rule applies to the whole bucket and runs daily." })),
      form));
  update();
  cancel.addEventListener("click", () => dlg.close());
  dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });

  return new Promise((resolve) => {
    dlg.addEventListener("close", () => { dlg.remove(); resolve({ changed }); });
    document.body.append(dlg);
    dlg.showModal();
    cancel.focus();
  });
}
