import { h } from "../dom.js";
import { icon } from "../icons.js";
import { copyText } from "../clipboard.js";
import { toast } from "./toast.js";

// <secret-field label="Secret access key" ttl="15">
//   field.value = "...";   // set via property so the secret never sits in an attribute
// Masked by default (a fixed-length mask, so the real length is not leaked).
// The eye button toggles reveal (a toggle, not press-and-hold, so it works with
// keyboard and touch); it re-masks itself after `ttl` seconds, with a draining
// bar showing the time left. Copy works whether masked or not.
const MASK = "•".repeat(24);
let seq = 0;

class SecretField extends HTMLElement {
  #value = "";
  #timer = 0;
  #copyTimer = 0;
  #revealed = false;

  get value() { return this.#value; }
  set value(v) {
    this.#value = String(v ?? "");
    this.removeAttribute("value"); // a secret must never sit in an attribute
    this.#mask();
  }

  connectedCallback() {
    this.removeAttribute("value");
    if (this.firstElementChild) return this.#mask(); // reconnected: always masked
    const id = `sf${++seq}`;
    const ttl = Number(this.getAttribute("ttl")) || 15;
    this.style.setProperty("--sf-ttl", `${ttl}s`);
    this.valueEl = h("output", { class: "sf-value", id, "aria-live": "off" }, MASK);
    this.revealBtn = h("button", { class: "btn btn-ghost btn-icon sf-btn", type: "button", "aria-pressed": "false" });
    this.copyBtn = h("button", { class: "btn btn-ghost btn-icon sf-btn", type: "button" });
    this.revealBtn.addEventListener("click", () => (this.#revealed ? this.#mask() : this.#reveal(ttl)));
    this.copyBtn.addEventListener("click", () => this.#copy());
    this.root = h(
      "div",
      { class: "sf", "data-revealed": "false" },
      h("span", { class: "label", id: `${id}-l`, text: this.getAttribute("label") ?? "Secret" }),
      h("div", { class: "sf-row" }, this.valueEl, this.revealBtn, this.copyBtn, h("span", { class: "sf-timer", "aria-hidden": "true" })),
      h("span", { class: "sf-hint", text: `Hidden by default. Revealed values hide again after ${ttl} seconds.` }),
    );
    this.valueEl.setAttribute("aria-labelledby", `${id}-l`);
    this.append(this.root);
    this.#paint();
  }

  disconnectedCallback() {
    clearTimeout(this.#timer);
    clearTimeout(this.#copyTimer);
    this.#mask(); // never leave plaintext in the detached DOM
    this.#value = ""; // do not keep a secret around in a detached element
  }

  #reveal(ttl) {
    this.#revealed = true;
    this.#paint();
    clearTimeout(this.#timer);
    this.#timer = setTimeout(() => this.#mask(), ttl * 1000);
  }

  #mask() {
    clearTimeout(this.#timer);
    this.#revealed = false;
    this.#paint();
  }

  #paint() {
    if (!this.root) return;
    this.root.dataset.revealed = String(this.#revealed);
    this.valueEl.textContent = this.#revealed ? this.#value : MASK;
    this.revealBtn.setAttribute("aria-pressed", String(this.#revealed));
    this.revealBtn.setAttribute("aria-label", this.#revealed ? "Hide secret" : "Reveal secret");
    this.revealBtn.replaceChildren(icon(this.#revealed ? "eye-off" : "eye"));
    if (!this.copyBtn.dataset.done) {
      this.copyBtn.setAttribute("aria-label", "Copy secret");
      this.copyBtn.replaceChildren(icon("copy"));
    }
    // restart the draining bar
    const bar = this.root.querySelector(".sf-timer");
    bar.style.animation = "none";
    void bar.offsetWidth;
    bar.style.animation = "";
  }

  async #copy() {
    if (!this.#value) return toast.error("Nothing to copy.");
    const ok = await copyText(this.#value);
    if (!ok) return toast.error("Could not copy. Select the value and copy it manually.");
    this.copyBtn.dataset.done = "true";
    this.copyBtn.replaceChildren(icon("check"));
    this.copyBtn.setAttribute("aria-label", "Copied");
    toast.show(`${this.getAttribute("label") ?? "Secret"} copied`);
    clearTimeout(this.#copyTimer);
    this.#copyTimer = setTimeout(() => {
      delete this.copyBtn.dataset.done;
      this.#paint();
    }, 1800);
  }
}
customElements.define("secret-field", SecretField);
