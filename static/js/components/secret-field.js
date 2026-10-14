import { h } from "../dom.js";
import { icon } from "../icons.js";
import { copyText } from "../clipboard.js";
import { toast } from "./toast.js";

// <secret-field label="Secret access key" ttl="15">
//   field.value = "...";   // set via property so the secret never sits in an attribute
//   field.fetcher = async () => "...";   // OR: fetch on demand (reveal/copy call it);
//     the value is dropped again whenever the field re-masks, so a secret never
//     lingers in memory. Fires bubbling "sf-reveal" / "sf-mask" events (no payload).
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
  #busy = false;
  #gen = 0; // bumped on every mask/clear/set: a fetch that finishes later is discarded
  fetcher = null;

  get value() { return this.#value; }
  set value(v) {
    this.#gen++;
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

  // Run the fetcher once, guarding against double clicks; null on failure (toasted).
  async #fetch() {
    if (this.#value) return this.#value;
    if (!this.fetcher || this.#busy) return null;
    this.#busy = true;
    const gen = this.#gen;
    this.revealBtn.disabled = this.copyBtn.disabled = true;
    try {
      const got = String((await this.fetcher()) ?? "");
      if (gen !== this.#gen) return null; // cleared or changed meanwhile: drop it
      this.#value = got;
      return got || null;
    } catch (err) {
      if (err?.name !== "ApiError" || err.status !== 401) toast.error(err?.message ?? "Could not load the secret.");
      return null;
    } finally {
      this.#busy = false;
      this.revealBtn.disabled = this.copyBtn.disabled = false;
    }
  }

  async #reveal(ttl) {
    if (!(await this.#fetch())) return;
    if (!this.isConnected) return this.#drop();
    this.#revealed = true;
    this.#paint();
    clearTimeout(this.#timer);
    this.#timer = setTimeout(() => this.#mask(), ttl * 1000);
    this.dispatchEvent(new CustomEvent("sf-reveal", { bubbles: true }));
  }

  #drop() {
    if (this.fetcher) this.#value = "";
  }

  #mask() {
    this.#gen++;
    clearTimeout(this.#timer);
    const was = this.#revealed || (this.fetcher && this.#value);
    this.#revealed = false;
    this.#drop();
    this.#paint();
    if (was) this.dispatchEvent(new CustomEvent("sf-mask", { bubbles: true }));
  }

  /** Mask now and forget a fetched value (e.g. when the key selection changes). */
  clear() { this.#mask(); }

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
    const wasRevealed = this.#revealed;
    const value = await this.#fetch();
    if (!value) return this.fetcher ? undefined : toast.error("Nothing to copy.");
    const ok = await copyText(value);
    if (!wasRevealed) this.#drop(); // copied while masked: do not keep it around
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
