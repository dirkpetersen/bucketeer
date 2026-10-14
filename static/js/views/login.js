import { h } from "../dom.js";
import { icon } from "../icons.js";
import { ApiError, login } from "../api.js";

// Distinct, human messages per status. 401 deliberately does not say whether
// the account exists or is merely not authorized.
function messageFor(err) {
  if (!(err instanceof ApiError)) return "Something went wrong. Try again.";
  switch (err.status) {
    case 400: return "The sign-in request was not understood. Reload the page and try again.";
    case 401: return "Invalid credentials or not authorized.";
    case 429: {
      const m = err.retryAfter ? Math.max(1, Math.ceil(err.retryAfter / 60)) : null;
      return `Too many failed attempts. ${m ? `Try again in about ${m} minute${m > 1 ? "s" : ""}.` : "Try again later."}`;
    }
    case 503: return "Login service unavailable. Try again later.";
    case 0: return err.message;
    default: return err.message;
  }
}

export function renderLogin({ appName, onSuccess }) {
  const user = h("input", {
    class: "input", id: "login-user", name: "username", type: "text", autocomplete: "username",
    autocapitalize: "none", spellcheck: "false", required: true, placeholder: "name@example.org",
  });
  const pass = h("input", {
    class: "input", id: "login-pass", name: "password", type: "password",
    autocomplete: "current-password", required: true,
  });
  const errBox = h("div", { id: "login-error", "aria-live": "polite" });
  const label = h("span", { text: "Sign in" });
  const btn = h("button", { class: "btn btn-primary btn-lg", type: "submit" }, label);
  const card = h("div", { class: "login-card" },
    h("div", {}, h("h1", { text: "Welcome back" }), h("p", { text: "Sign in with your institutional account." })),
    h("form", { class: "login-form", novalidate: true,
      on: { submit: async (e) => {
        e.preventDefault();
        if (btn.disabled) return;
        for (const i of [user, pass]) i.removeAttribute("aria-invalid");
        errBox.replaceChildren();
        if (!user.value.trim() || !pass.value) {
          const bad = !user.value.trim() ? user : pass;
          bad.setAttribute("aria-invalid", "true");
          show("Enter your username or email and your password.");
          bad.focus();
          return;
        }
        btn.disabled = true;
        btn.setAttribute("aria-busy", "true");
        label.textContent = "Signing in";
        btn.prepend(h("span", { class: "spinner", "aria-hidden": "true" }));
        try {
          const me = await login(user.value, pass.value);
          pass.value = "";
          onSuccess(me);
        } catch (err) {
          show(messageFor(err));
          if (err instanceof ApiError && err.status === 401) { pass.value = ""; pass.setAttribute("aria-invalid", "true"); pass.focus(); }
        } finally {
          btn.disabled = false;
          btn.removeAttribute("aria-busy");
          label.textContent = "Sign in";
          btn.querySelector(".spinner")?.remove();
        }
      } } },
      h("div", { class: "field" }, h("label", { for: "login-user", text: "Username or email" }), user),
      h("div", { class: "field" }, h("label", { for: "login-pass", text: "Password" }), pass),
      errBox,
      btn));

  function show(text) {
    errBox.replaceChildren(h("div", { class: "form-error", role: "alert" }, icon("alert"), h("span", { text })));
    card.classList.remove("shake");
    void card.offsetWidth;
    card.classList.add("shake");
  }

  const view = h("main", { class: "login", id: "main" },
    h("div", { class: "login-wrap" },
      h("div", { class: "brand" }, h("img", { src: "img/mark.svg", alt: "", width: 36, height: 36 }), h("span", { class: "brand-name", text: appName })),
      card,
      h("p", { class: "login-foot", text: "Your password is checked against the directory and never stored." })));
  queueMicrotask(() => user.focus());
  return view;
}
