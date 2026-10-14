import { h } from "../dom.js";

// <confirm-dialog> on a native <dialog>. Usage:
//   const ok = await confirmDialog({ title, message, confirmLabel, danger, requireText });
// With `requireText` (e.g. a bucket name) the confirm button stays disabled
// until the user types exactly that text. Esc / Cancel / backdrop resolve false.
let seq = 0;

class ConfirmDialog extends HTMLElement {
  ask({ title, message = "", confirmLabel = "Confirm", cancelLabel = "Cancel", danger = false, requireText = "" }) {
    const id = `cd${++seq}`;
    const confirmBtn = h("button", { class: `btn ${danger ? "btn-danger" : "btn-primary"}`, type: "submit", value: "yes", text: confirmLabel });
    const cancelBtn = h("button", { class: "btn", type: "submit", value: "no", formNoValidate: true, text: cancelLabel });
    const msg = h("p", { class: "cd-msg", id: `${id}-m` }, message);
    let input = null;
    const kids = [h("h2", { id: `${id}-t`, text: title }), msg];
    if (requireText) {
      input = h("input", { class: "input", id: `${id}-i`, autocomplete: "off", autocapitalize: "off", spellcheck: "false" });
      confirmBtn.disabled = true;
      input.addEventListener("input", () => (confirmBtn.disabled = input.value !== requireText));
      kids.push(h("div", { class: "field" },
        h("label", { for: `${id}-i` }, "Type ", h("strong", { text: requireText }), " to confirm"), input));
    }
    kids.push(h("div", { class: "cd-actions" }, cancelBtn, confirmBtn));
    const form = h("form", { method: "dialog" }, ...kids);
    const dlg = h("dialog", { class: "cd", "aria-labelledby": `${id}-t`, "aria-describedby": `${id}-m`, "data-danger": String(danger) }, form);
    this.replaceChildren(dlg);

    return new Promise((resolve) => {
      dlg.addEventListener("close", () => {
        resolve(dlg.returnValue === "yes" && (!requireText || input.value === requireText));
        dlg.remove();
      });
      dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close("no"); }); // backdrop
      dlg.showModal();
      (input ?? cancelBtn).focus(); // never default-focus a destructive action
    });
  }
}
customElements.define("confirm-dialog", ConfirmDialog);

export function confirmDialog(opts) {
  let el = document.querySelector("confirm-dialog");
  if (!el) {
    el = document.createElement("confirm-dialog");
    document.body.append(el);
  }
  return el.ask(opts);
}
