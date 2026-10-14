import { isAdvanced, subscribe } from "../advanced.js";
import { h } from "../dom.js";
import { icon } from "../icons.js";
import { bucketOptions, createBucket } from "../api.js";
import { checkBudget } from "../budget.js";

// createBucketDialog() -> Promise<{ created: object } | null>
// A native <dialog> form. The server is authoritative for every rule (naming, budget
// format, placement); the checks here give instant feedback only. Object lock and
// placement live under "Advanced" and are shown only when the global toggle is on
// (placement only when the administrators allow more than one).
let seq = 0;

const DNS = /^[a-z0-9][a-z0-9.-]*[a-z0-9]$/;

export function checkName(name, prefix) {
  if (!name) return "Enter a bucket name.";
  if (name.length < 3 || name.length > 63) return "Use 3 to 63 characters.";
  if (!DNS.test(name)) return "Use lowercase letters, digits, dots and hyphens; start and end with a letter or digit.";
  if (name.includes("..") || name.includes(".-") || name.includes("-.")) return "Dots and hyphens cannot sit next to each other.";
  if (/^\d+\.\d+\.\d+\.\d+$/.test(name)) return "A name cannot look like an IP address.";
  if (prefix && !name.startsWith(prefix)) return `The name must start with "${prefix}".`;
  return "";
}

export async function createBucketDialog() {
  const id = `cb${++seq}`;
  let opts;
  try {
    opts = await bucketOptions();
  } catch (err) {
    return { error: err };
  }
  const prefix = opts.naming.prefix || "";

  const name = h("input", {
    class: "input mono-input", id: `${id}-n`, value: prefix, autocomplete: "off", autocapitalize: "off",
    spellcheck: "false", maxlength: "63", placeholder: prefix ? `${prefix}my-data` : "my-data", "aria-describedby": `${id}-nh ${id}-ne`,
  });
  const nameErr = h("p", { class: "set-err", id: `${id}-ne`, role: "alert", hidden: true });
  const budget = h("input", {
    class: "input mono-input", id: `${id}-b`, value: opts.defaultBudgetNumber ?? "", autocomplete: "off",
    spellcheck: "false", maxlength: "64", placeholder: opts.budgetExample ? `e.g. ${opts.budgetExample}` : "e.g. 123456 or WT-ABC",
    "aria-describedby": `${id}-bh ${id}-be`, required: opts.budgetRequired,
  });
  const budgetErr = h("p", { class: "set-err", id: `${id}-be`, role: "alert", hidden: true });
  const versioning = h("input", { type: "checkbox", id: `${id}-v`, checked: true });
  const ncDays = h("input", {
    class: "input mono-input", type: "number", id: `${id}-nd`, value: "30", min: "1", max: "36500",
    inputmode: "numeric", "aria-label": "Days before old versions are deleted",
  });
  const ncRow = h("div", { class: "field cb-nc" },
    h("label", { for: ncDays.id }, "Delete old versions after ", ncDays, " days"),
    h("p", { class: "set-hint", text: "Old versions count toward your quota. This removes replaced or deleted versions after the chosen time; you can change it later under Lifecycle." }));
  const lock = h("input", { type: "checkbox", id: `${id}-l` });
  const placement = opts.placements.length > 1
    ? h("select", { class: "input", id: `${id}-p`, "aria-label": "Placement" },
      opts.placements.map((p) => h("option", { value: p, selected: p === opts.defaultPlacement, text: p === opts.defaultPlacement ? `${p} (default)` : p })))
    : null;
  const error = h("p", { class: "qd-error", role: "alert", hidden: true });
  const cancel = h("button", { class: "btn", type: "button", text: "Cancel" });
  const submit = h("button", { class: "btn btn-primary", type: "submit", text: "Create bucket" });

  const check = (input, label, hint, cls = "") =>
    h("label", { class: `qd-check ${cls}`, for: input.id }, input, h("span", {}, h("b", { text: label }), h("small", { text: hint })));

  const advancedBlock = h("fieldset", { class: "cb-adv" },
      h("legend", {}, h("span", { class: "chip chip-accent", text: "Advanced" })),
      check(lock, "Enable object lock", "Lets you protect objects from deletion with retention periods and legal holds.", "qd-main"),
      h("div", { class: "set-warn cb-lock-warn", id: `${id}-lw`, hidden: true },
        icon("alert", "icon"),
        h("span", { text: "Object lock can only be switched on now, while the bucket is created, and it can never be switched off. It also turns versioning on for good. Locked objects cannot be deleted until their retention ends, and a bucket holding them cannot be deleted." })),
      placement && h("div", { class: "field" },
        h("label", { for: placement.id, text: "Placement" }), placement,
        h("p", { class: "set-hint", text: "Where the data is stored. The default suits most buckets." })));
  advancedBlock.hidden = !isAdvanced();

  const form = h("form", { novalidate: true },
    h("div", { class: "qd-head" },
      h("h2", { id: `${id}-t`, text: "Create a bucket" }),
      h("p", { class: "cd-msg", id: `${id}-m`, text: "Bucket names are unique across the whole storage system and cannot be changed later." })),
    h("div", { class: "field" },
      h("label", { for: name.id, text: "Bucket name" }), name,
      h("p", { class: "set-hint", id: `${id}-nh`, text: opts.naming.hint }), nameErr),
    h("div", { class: "field" },
      h("label", { for: budget.id }, "Budget number ", h("span", { class: "cb-opt", text: opts.budgetRequired ? "(required)" : "(optional for administrators)" })),
      budget,
      h("p", { class: "set-hint", id: `${id}-bh`, text: opts.budgetExample ? `Your index or worktag, for chargeback. Example: ${opts.budgetExample}` : "Your index or worktag, for chargeback." }),
      budgetErr),
    check(versioning, "Keep every version of each object", "Protects against accidental overwrites and deletes. Old versions count toward your quota."),
    ncRow,
    advancedBlock,
    error,
    h("div", { class: "cd-actions" }, cancel, submit));

  const dlg = h("dialog", { class: "cd qd cb", "aria-labelledby": `${id}-t`, "aria-describedby": `${id}-m` }, form);

  const show = (el, msg) => {
    el.textContent = msg;
    el.hidden = !msg;
    const input = el === nameErr ? name : budget;
    input.setAttribute("aria-invalid", String(!!msg));
  };
  const validateName = () => { const m = checkName(name.value, prefix); show(nameErr, name.value === prefix ? "" : m); return m; };
  const validateBudget = () => {
    const v = budget.value.trim();
    const m = !v && opts.budgetRequired ? "Enter a budget number." : checkBudget(budget.value);
    show(budgetErr, !v ? "" : m);
    return m;
  };
  name.addEventListener("input", () => { validateName(); showError(""); });
  budget.addEventListener("input", () => { validateBudget(); showError(""); });
  function showError(msg) { error.textContent = msg; error.hidden = !msg; }
  const syncLock = () => {
    const locked = lock.checked;
    const warn = dlg.querySelector(".cb-lock-warn");
    if (warn) warn.hidden = !locked;
    if (locked) versioning.checked = true;
    versioning.disabled = locked;
    ncRow.hidden = !versioning.checked;
  };
  lock.addEventListener("change", syncLock);
  versioning.addEventListener("change", syncLock);

  let busy = false;
  let result = null;
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (busy) return;
    const nm = validateName();
    const bm = validateBudget();
    if (nm || bm) { show(nameErr, nm); show(budgetErr, bm); (nm ? name : budget).focus(); return; }
    const withRule = versioning.checked || (isAdvanced() && lock.checked);
    const days = Number(ncDays.value);
    if (withRule && (!Number.isInteger(days) || days < 1 || days > 36500)) {
      showError("Enter a whole number of days from 1 to 36500.");
      ncDays.focus();
      return;
    }
    showError("");
    busy = true;
    submit.disabled = true;
    submit.replaceChildren(h("span", { class: "spinner", "aria-hidden": "true" }), "Creating…");
    try {
      result = await createBucket({
        name: name.value,
        budgetNumber: budget.value.trim() || null,
        versioning: versioning.checked || (isAdvanced() && lock.checked),
        objectLock: isAdvanced() && lock.checked,
        noncurrentDays: withRule ? days : null,
        placement: isAdvanced() && placement && placement.value !== opts.defaultPlacement ? placement.value : null,
      });
      dlg.close();
    } catch (err) {
      if (err.status === 401) { dlg.close(); return; }
      showError(err.message || "Could not create the bucket.");
      submit.disabled = false;
      submit.textContent = "Create bucket";
    } finally {
      busy = false;
    }
  });
  cancel.addEventListener("click", () => dlg.close());
  dlg.addEventListener("cancel", (e) => { if (busy) e.preventDefault(); });
  dlg.addEventListener("click", (e) => { if (e.target === dlg && !busy) dlg.close(); });

  const unsub = subscribe((on) => { advancedBlock.hidden = !on; });
  return new Promise((resolve) => {
    dlg.addEventListener("close", () => { unsub(); dlg.remove(); resolve({ created: result }); });
    document.body.append(dlg);
    dlg.showModal();
    name.focus();
    name.setSelectionRange(name.value.length, name.value.length);
  });
}
