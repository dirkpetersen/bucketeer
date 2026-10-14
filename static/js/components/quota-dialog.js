import { h } from "../dom.js";
import { UNITS } from "../format.js";

// quotaDialog({ title, subject, quota, onSave }) -> Promise<boolean>
// A native <dialog> form. `onSave(body)` is async and is given
// { enabled, maxSize (bytes|null), maxObjects (int|null) }; if it throws, the
// message is shown inline and the dialog stays open. Resolves true once saved.
export const SIZE_UNITS = UNITS.filter(([u]) => ["KiB", "MiB", "GiB", "TiB"].includes(u));
let seq = 0;

export function splitSize(bytes) {
  if (bytes == null) return ["", "GiB"];
  for (const [u, f] of [...SIZE_UNITS].reverse()) if (bytes >= f && bytes % f === 0) return [String(bytes / f), u];
  const [u, f] = bytes >= 1024 ** 2 ? SIZE_UNITS[1] : SIZE_UNITS[0];
  return [String(+(bytes / f).toFixed(2)), u];
}

export function quotaDialog({ title, subject, hint = "", quota, onSave, min = 0 }) {
  const id = `qd${++seq}`;
  const [sizeText, sizeUnit] = splitSize(quota?.maxSize);
  const enforce = h("input", { type: "checkbox", id: `${id}-e`, checked: !!quota?.enabled });
  const size = h("input", { class: "input", type: "number", id: `${id}-s`, min: String(min), step: "any", inputMode: "decimal", value: sizeText, placeholder: "e.g. 500" });
  const unit = h("select", { class: "input", id: `${id}-u`, "aria-label": "Size unit" },
    SIZE_UNITS.map(([u]) => h("option", { value: u, selected: u === sizeUnit, text: u })));
  const noSize = h("input", { type: "checkbox", id: `${id}-ns`, checked: quota?.maxSize == null });
  const objs = h("input", { class: "input", type: "number", id: `${id}-o`, min: String(min), step: "1", inputMode: "numeric", value: quota?.maxObjects ?? "", placeholder: "e.g. 1000000" });
  const noObjs = h("input", { type: "checkbox", id: `${id}-no`, checked: quota?.maxObjects == null });
  const error = h("p", { class: "qd-error", role: "alert", hidden: true });
  const cancel = h("button", { class: "btn", type: "button", text: "Cancel" });
  const save = h("button", { class: "btn btn-primary", type: "submit", text: "Save quota" });

  const sync = () => {
    size.disabled = unit.disabled = !enforce.checked || noSize.checked;
    noSize.disabled = noObjs.disabled = !enforce.checked;
    objs.disabled = !enforce.checked || noObjs.checked;
  };
  for (const el of [enforce, noSize, noObjs]) el.addEventListener("change", sync);
  sync();

  const showError = (msg) => { error.textContent = msg; error.hidden = !msg; };
  function read() {
    if (!enforce.checked) return { enabled: false };
    let maxSize = null;
    let maxObjects = null;
    if (!noSize.checked) {
      const v = Number(size.value);
      if (size.value.trim() === "" || !Number.isFinite(v) || v < min) throw new Error(`Enter a size of ${min} or more, or choose No size limit.`);
      maxSize = Math.round(v * UNITS.find(([u]) => u === unit.value)[1]);
      if (maxSize < min) throw new Error(`The size must be at least ${min} byte${min === 1 ? "" : "s"}.`);
      if (!Number.isSafeInteger(maxSize)) throw new Error("That size is too large.");
    }
    if (!noObjs.checked) {
      const v = Number(objs.value);
      if (objs.value.trim() === "" || !Number.isInteger(v) || v < min) throw new Error(`Enter a whole number of objects (${min} or more), or choose No object limit.`);
      maxObjects = v;
    }
    if (maxSize == null && maxObjects == null) throw new Error("Set at least one limit, or switch the quota off.");
    return { enabled: true, maxSize, maxObjects };
  }

  const check = (input, label, cls = "") => h("label", { class: `qd-check ${cls}`, for: input.id }, input, label);
  const form = h("form", { novalidate: true },
    h("div", { class: "qd-head" },
      h("h2", { id: `${id}-t`, text: title }),
      h("p", { class: "cd-msg", id: `${id}-m` }, subject, hint && h("span", { class: "qd-hint", text: hint }))),
    check(enforce, h("span", {}, h("b", { text: "Enforce quota" }), h("small", { text: "Writes are refused once a limit is reached." })), "qd-main"),
    h("div", { class: "qd-grid" },
      h("div", { class: "field" }, h("label", { for: size.id, text: "Size limit" }),
        h("div", { class: "qd-pair" }, size, unit), check(noSize, "No size limit")),
      h("div", { class: "field" }, h("label", { for: objs.id, text: "Object limit" }), objs, check(noObjs, "No object limit"))),
    error,
    h("div", { class: "cd-actions" }, cancel, save));
  const dlg = h("dialog", { class: "cd qd", "aria-labelledby": `${id}-t`, "aria-describedby": `${id}-m` }, form);
  let busy = false;
  let saved = false;

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (busy) return;
    let body;
    try { body = read(); } catch (err) { showError(err.message); return; }
    showError("");
    busy = true;
    save.disabled = true;
    save.replaceChildren(h("span", { class: "spinner", "aria-hidden": "true" }), "Saving…");
    try {
      await onSave(body);
      saved = true;
      dlg.close();
    } catch (err) {
      showError(err.message || "Could not save the quota.");
      save.disabled = false;
      save.textContent = "Save quota";
    } finally {
      busy = false;
    }
  });
  cancel.addEventListener("click", () => dlg.close());
  dlg.addEventListener("cancel", (e) => { if (busy) e.preventDefault(); });
  dlg.addEventListener("click", (e) => { if (e.target === dlg && !busy) dlg.close(); });

  return new Promise((resolve) => {
    dlg.addEventListener("close", () => { dlg.remove(); resolve(saved); });
    document.body.append(dlg);
    dlg.showModal();
    (enforce.checked && !size.disabled ? size : enforce).focus();
  });
}
