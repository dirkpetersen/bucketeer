import { h } from "../dom.js";
import { icon } from "../icons.js";

// <toast-region> is a singleton live region; call toast.show() / toast.error().
class ToastRegion extends HTMLElement {
  connectedCallback() {
    this.setAttribute("role", "region");
    this.setAttribute("aria-label", "Notifications");
  }

  show(message, { kind = "info", timeout = 4000 } = {}) {
    const isError = kind === "error";
    const close = h("button", { class: "btn btn-icon btn-ghost", type: "button", "aria-label": "Dismiss" }, icon("x"));
    const el = h(
      "div",
      { class: "toast", "data-kind": kind, role: isError ? "alert" : "status" },
      icon(isError ? "alert" : "check", "toast-icon"),
      h("span", { class: "toast-msg", text: message }),
      close,
    );
    const dismiss = () => {
      if (el.dataset.leaving) return;
      el.dataset.leaving = "true";
      const done = () => el.remove();
      el.addEventListener("animationend", done, { once: true });
      setTimeout(done, 400); // safety net when animations are disabled
    };
    close.addEventListener("click", dismiss);
    this.append(el);
    while (this.children.length > 4) this.firstElementChild.remove();
    if (timeout > 0) setTimeout(dismiss, timeout);
    return dismiss;
  }
}
customElements.define("toast-region", ToastRegion);

function region() {
  let r = document.querySelector("toast-region");
  if (!r) {
    r = document.createElement("toast-region");
    document.body.append(r);
  }
  return r;
}

export const toast = {
  show: (msg, opts) => region().show(msg, opts),
  error: (msg, opts) => region().show(msg, { kind: "error", timeout: 7000, ...opts }),
};
