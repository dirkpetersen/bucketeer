import { h } from "../dom.js";
import { icon } from "../icons.js";
import { bucketOptions, setBucketBudget } from "../api.js";
import { checkBudget } from "../budget.js";
import { nameSpan } from "./confirm-dialog.js";

// budgetDialog({ bucket, current }) -> Promise<{ error? } | { saved }>
// Change the budget number a bucket is charged to. The server is authoritative (character set,
// the administrators' format, "required unless you are an administrator"); the checks here give
// instant feedback only. The number is mirrored into the bucketeer-budget tag by the server.
let seq = 0;

export async function budgetDialog({ bucket, current }) {
  const id = `bud${++seq}`;
  let opts;
  try { opts = await bucketOptions(); } catch (err) { return { error: err }; }
  const required = opts.budgetRequired;
  let saved = null;
  let busy = false;

  const input = h("input", {
    class: "input mono-input", id: `${id}-b`, value: current ?? "", autocomplete: "off", spellcheck: "false",
    maxlength: "64", placeholder: opts.budgetExample ? `e.g. ${opts.budgetExample}` : "e.g. 123456 or WT-ABC",
    "aria-describedby": `${id}-h ${id}-e`,
  });
  const fieldErr = h("p", { class: "set-err", id: `${id}-e`, role: "alert", hidden: true });
  const error = h("p", { class: "qd-error", role: "alert", hidden: true });
  const show = (el, msg) => { el.textContent = msg; el.hidden = !msg; };
  const cancel = h("button", { class: "btn", type: "button", text: "Cancel" });
  const submit = h("button", { class: "btn btn-primary", type: "submit" }, icon("check"), "Save");

  const validate = () => {
    const v = input.value.trim();
    const m = !v && required ? "Enter a budget number." : checkBudget(input.value);
    show(fieldErr, !v && !required ? "" : m);
    input.toggleAttribute("aria-invalid", !!(m && (v || required)));
    return m;
  };
  input.addEventListener("input", () => { validate(); show(error, ""); });

  const form = h("form", { novalidate: true },
    h("div", { class: "qd-head" },
      h("h2", { id: `${id}-t` }, "Budget number of ", nameSpan(bucket)),
      h("p", { class: "cd-msg", id: `${id}-m`, text: "The index or worktag this bucket is charged to. The change is recorded in the audit log." })),
    h("div", { class: "field" },
      h("label", { for: input.id }, "Budget number ", h("span", { class: "cb-opt", text: required ? "(required)" : "(optional for administrators)" })),
      input,
      h("p", { class: "set-hint", id: `${id}-h`, text: opts.budgetExample ? `Example: ${opts.budgetExample}` : "Your index or worktag, for chargeback." }),
      fieldErr),
    error,
    h("div", { class: "cd-actions" }, cancel, submit));
  const dlg = h("dialog", { class: "cd qd cb", "aria-labelledby": `${id}-t`, "aria-describedby": `${id}-m` }, form);

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (busy) return;
    if (validate()) { input.focus(); return; }
    const value = input.value.trim() || null;
    if (value === (current ?? null)) { dlg.close(); return; }
    show(error, "");
    busy = true;
    submit.disabled = true;
    submit.replaceChildren(h("span", { class: "spinner", "aria-hidden": "true" }), "Saving…");
    try {
      saved = await setBucketBudget(bucket, value);
      dlg.close();
    } catch (err) {
      if (err.status === 401) { dlg.close(); return; }
      show(error, err.message || "Could not save the budget number.");
      submit.disabled = false;
      submit.replaceChildren(icon("check"), "Save");
    } finally {
      busy = false;
    }
  });
  cancel.addEventListener("click", () => dlg.close());
  dlg.addEventListener("cancel", (e) => { if (busy) e.preventDefault(); });
  dlg.addEventListener("click", (e) => { if (e.target === dlg && !busy) dlg.close(); });

  return new Promise((resolve) => {
    dlg.addEventListener("close", () => { dlg.remove(); resolve({ saved }); });
    document.body.append(dlg);
    dlg.showModal();
    input.focus();
    input.select();
  });
}
