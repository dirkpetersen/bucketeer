import { h } from "../dom.js";
import { icon } from "../icons.js";
import { getProfile, saveProfile } from "../api.js";
import { toast } from "../components/toast.js";
import { checkBudget as check } from "../budget.js";

// The user's profile: name and email come from AD (read-only); the default
// budget number prefills the budget field when creating a bucket. The server
// validates; the same base rules run here only to give instant feedback.

function identity(me) {
  const row = (label, value) => h("div", { class: "pf-row" }, h("dt", { text: label }), h("dd", { text: value || "-" }));
  return h("section", { class: "card pf-card", "aria-labelledby": "pf-id" },
    h("div", { class: "set-head" },
      h("h2", { id: "pf-id", text: "Account" }),
      h("span", { class: "chip" }, icon("shield", "icon"), "From Active Directory")),
    h("p", { class: "set-lead", text: "These details come from your directory account and cannot be changed here." }),
    h("dl", { class: "pf-list" }, row("Name", me.displayName), row("Username", me.username), row("Email", me.email)));
}

function budgetCard() {
  let saved = null;
  let serverErr = ""; // last 422 message, shown until the user edits
  const input = h("input", {
    class: "input mono-input", type: "text", id: "pf-budget", autocomplete: "off", spellcheck: "false",
    placeholder: "e.g. 123456 or WT-ABC", maxlength: "200", disabled: true,
  });
  const hint = h("p", { class: "set-hint", id: "pf-budget-hint" });
  const error = h("p", { class: "set-err", id: "pf-budget-err", role: "alert", hidden: true });
  const cancel = h("button", { class: "btn", type: "button", text: "Reset", disabled: true });
  const clear = h("button", { class: "btn", type: "button", text: "Clear", disabled: true });
  const save = h("button", { class: "btn btn-primary", type: "submit", text: "Save", disabled: true });
  const form = h("form", { class: "set-form", novalidate: true },
    h("div", { class: "field" },
      h("label", { for: "pf-budget", text: "Default budget number" }), input,
      hint,
      error),
    h("div", { class: "set-actions" }, clear, cancel, save));

  const showError = (msg) => {
    error.textContent = msg;
    error.hidden = !msg;
    input.setAttribute("aria-invalid", String(!!msg));
    if (msg) input.setAttribute("aria-describedby", "pf-budget-err"); else input.removeAttribute("aria-describedby");
  };
  const refresh = () => {
    const dirty = input.value.trim() !== (saved ?? "");
    showError(check(input.value) || serverErr);
    save.disabled = cancel.disabled = !dirty || !!error.textContent;
    cancel.disabled = !dirty;
    clear.disabled = saved == null && !input.value;
  };
  function apply(profile) {
    serverErr = "";
    saved = profile.defaultBudgetNumber;
    hint.textContent = "Your index or worktag. Letters (A-Z), digits, spaces and + - = . _ : / @ are allowed, up to 64 characters."
      + (profile.budgetExample ? ` Example: ${profile.budgetExample}` : "");
    if (profile.budgetExample) input.placeholder = `e.g. ${profile.budgetExample}`;
    input.value = saved ?? "";
    refresh();
  }
  input.addEventListener("input", () => { serverErr = ""; refresh(); });
  cancel.addEventListener("click", () => { serverErr = ""; input.value = saved ?? ""; refresh(); input.focus(); });
  clear.addEventListener("click", () => { serverErr = ""; input.value = ""; refresh(); input.focus(); });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (save.disabled) return;
    save.disabled = cancel.disabled = clear.disabled = input.disabled = true;
    save.replaceChildren(h("span", { class: "spinner", "aria-hidden": "true" }), "Saving…");
    try {
      const res = await saveProfile(input.value.trim() || null);
      input.disabled = false;
      apply(res);
      toast.show(res.defaultBudgetNumber ? "Default budget number saved." : "Default budget number cleared.");
    } catch (err) {
      input.disabled = false;
      if (err.status === 422) serverErr = err.message;
      else if (err.status !== 401) toast.error(err.message);
      input.focus();
      save.disabled = false;
      cancel.disabled = false;
    } finally {
      save.textContent = "Save";
      input.disabled = false;
      refresh();
    }
  });

  const card = h("section", { class: "card pf-card", "aria-labelledby": "pf-bud" },
    h("div", { class: "set-head" }, h("h2", { id: "pf-bud", text: "Budget number" })),
    h("p", { class: "set-lead", text: "Every new bucket needs a budget number for chargeback. The default you save here is filled in for you whenever you create a bucket; you can still change it per bucket." }),
    h("div", { class: "set-loading", "aria-busy": "true" }, h("div", { class: "skel-line w60" }), h("div", { class: "skel-line w40" })));
  const loading = card.lastElementChild;

  getProfile().then((p) => {
    loading.replaceWith(form);
    input.disabled = false;
    apply(p);
  }).catch((err) => {
    if (err.status === 401) return;
    loading.replaceWith(h("div", { class: "set-unavail" }, h("p", { text: err.message }),
      h("button", { class: "btn", type: "button", on: { click: () => location.reload() } }, icon("refresh"), "Try again")));
  });
  return card;
}

export function renderProfile({ me }) {
  return h("main", { class: "main", id: "main" },
    h("div", { class: "hero" },
      h("span", { class: "eyebrow", text: "Your profile" }),
      h("h1", { text: me.displayName || me.username }),
      h("p", { text: "Defaults that save you typing. They only prefill forms; nothing here changes your access." })),
    h("div", { class: "pf-grid" }, budgetCard(), identity(me)));
}
