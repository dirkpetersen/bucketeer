import { h } from "../dom.js";
import { icon } from "../icons.js";

// <menu-button> is a trigger button plus an action menu (WAI-ARIA menu button pattern).
//
//   const mb = h("menu-button", { label: "More", ariaLabel: "More actions for x" });
//   mb.items = [{ label, icon?, hint?, badge?, danger?, disabled?, reason?, separatorBefore?, onSelect }];
//
// The menu is a manual popover (top layer, so no overflow clipping) when the browser
// has the Popover API, else an ordinary fixed-position element. It opens under the
// trigger, flips above it near the viewport bottom, stays inside the viewport, and on
// narrow screens becomes a bottom sheet. Closing returns focus to the trigger.
// A disabled item stays focusable (aria-disabled) and shows its `reason` as the hint.
const HAS_POPOVER = typeof HTMLElement !== "undefined" && "popover" in HTMLElement.prototype;
const NARROW = "(max-width: 34em)";
let uid = 0;

class MenuButton extends HTMLElement {
  #items = [];
  #open = false;
  #trigger = null;
  #menu = null;
  #onDoc = (e) => {
    if (this.#trigger.contains(e.target)) return;
    if (e.target === this.#menu) { // a tap on the popover's ::backdrop reports the menu itself
      const r = this.#menu.getBoundingClientRect();
      if (e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom) return;
    } else if (this.#menu.contains(e.target)) return;
    this.close(false);
  };
  #onAway = () => this.close(false);
  #scrollAt = 0;
  // Scroll events are delivered a frame late, so compare positions instead of trusting the event.
  #onScroll = (e) => {
    if (this.#menu.contains(e.target)) return;
    if (e.target === document && Math.abs(window.scrollY - this.#scrollAt) < 2) return;
    this.close(false);
  };

  get items() { return this.#items; }
  set items(list) {
    this.#items = Array.isArray(list) ? list : [];
    if (this.#menu) this.#render();
  }
  set label(v) { this._label = v; if (this.#trigger) this.#paintTrigger(); }
  set ariaLabel(v) { this._aria = v; if (this.#trigger) this.#trigger.setAttribute("aria-label", v); }
  set disabled(v) { this._disabled = !!v; if (this.#trigger) this.#trigger.disabled = this._disabled; }

  connectedCallback() {
    if (this.#trigger) return;
    const id = `mb-${++uid}`;
    this.#trigger = h("button", {
      class: "btn btn-sm mb-trigger", type: "button", "aria-haspopup": "menu", "aria-expanded": "false", "aria-controls": `${id}-m`,
      on: { click: (e) => (this.#open ? this.close(true) : this.show({ focus: e.detail === 0 ? "first" : "none" })), keydown: (e) => this.#triggerKey(e) },
    });
    this.#trigger.disabled = !!this._disabled;
    if (this._aria) this.#trigger.setAttribute("aria-label", this._aria);
    this.#menu = h("div", {
      class: "mb-menu", id: `${id}-m`, role: "menu", hidden: true, on: { keydown: (e) => this.#menuKey(e) },
    });
    if (HAS_POPOVER) this.#menu.setAttribute("popover", "manual");
    this.#paintTrigger();
    this.append(this.#trigger, this.#menu);
    this.#render();
  }

  disconnectedCallback() { if (this.#open) this.close(false); }

  #paintTrigger() {
    this.#trigger.replaceChildren(icon("more", "icon"), h("span", { class: "mb-label", text: this._label ?? "More" }), icon("down", "icon mb-caret"));
  }

  #render() {
    const kids = [];
    this.#items.forEach((it, i) => {
      if (it.separatorBefore && i) kids.push(h("div", { class: "mb-sep", role: "separator" }));
      const hint = it.disabled && it.reason ? it.reason : it.hint;
      const b = h("button", {
        class: "mb-item", type: "button", role: "menuitem", tabindex: "-1", "data-danger": it.danger ? "true" : null,
        "aria-disabled": it.disabled ? "true" : null, title: it.disabled && it.reason ? it.reason : null,
        on: { click: () => this.#choose(it) },
      },
      it.icon ? icon(it.icon, "icon mb-ic") : h("span", { class: "mb-ic" }),
      h("span", { class: "mb-text" }, h("span", { class: "mb-name", text: it.label }), hint ? h("span", { class: "mb-hint", text: hint }) : null),
      it.badge ? h("span", { class: "mb-badge", text: String(it.badge) }) : null);
      kids.push(b);
    });
    this.#menu.replaceChildren(...kids);
  }

  #buttons() { return [...this.#menu.querySelectorAll(".mb-item")]; }

  #choose(it) {
    if (it.disabled) return;
    this.close(true);
    it.onSelect?.();
  }

  show({ focus = "first" } = {}) {
    if (this.#open || !this.#items.length) return;
    this.#open = true;
    this.#trigger.setAttribute("aria-expanded", "true");
    const sheet = matchMedia(NARROW).matches;
    this.#menu.dataset.sheet = String(sheet);
    this.#menu.hidden = false;
    if (HAS_POPOVER) this.#menu.showPopover();
    this.#place(sheet);
    this.dataset.open = "true";
    this.#scrollAt = window.scrollY;
    document.addEventListener("pointerdown", this.#onDoc, true);
    window.addEventListener("resize", this.#onAway);
    window.addEventListener("scroll", this.#onScroll, true);
    const list = this.#buttons();
    const target = focus === "last" ? list.at(-1) : focus === "first" ? list[0] : null;
    if (target) target.focus({ preventScroll: true });
    else { this.#menu.tabIndex = -1; this.#menu.focus({ preventScroll: true }); }
  }

  #place(sheet) {
    const m = this.#menu;
    for (const p of ["left", "top", "bottom", "right", "maxHeight"]) m.style[p] = "";
    if (sheet) return; // CSS pins the sheet to the bottom edge
    const gap = 6;
    const vw = document.documentElement.clientWidth;
    const vh = window.innerHeight;
    const t = this.#trigger.getBoundingClientRect();
    const w = m.offsetWidth;
    const hgt = m.offsetHeight;
    const below = vh - t.bottom - gap - 8;
    const above = t.top - gap - 8;
    const up = hgt > below && above > below;
    const left = Math.max(8, Math.min(t.right - w, vw - w - 8));
    m.style.left = `${left}px`;
    m.style.maxHeight = `${Math.max(120, up ? above : below)}px`;
    if (up) m.style.bottom = `${vh - t.top + gap}px`;
    else m.style.top = `${t.bottom + gap}px`;
    m.dataset.up = String(up);
  }

  close(returnFocus = true) {
    if (!this.#open) return;
    this.#open = false;
    document.removeEventListener("pointerdown", this.#onDoc, true);
    window.removeEventListener("resize", this.#onAway);
    window.removeEventListener("scroll", this.#onScroll, true);
    if (HAS_POPOVER && this.#menu.matches(":popover-open")) this.#menu.hidePopover();
    this.#menu.hidden = true;
    this.#trigger.setAttribute("aria-expanded", "false");
    delete this.dataset.open;
    if (returnFocus) this.#trigger.focus();
  }

  #triggerKey(e) {
    if (e.key === "Escape" && this.#open) { e.preventDefault(); e.stopPropagation(); this.close(true); return; }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!this.#open) this.show({ focus: e.key === "ArrowUp" ? "last" : "first" });
      else (e.key === "ArrowUp" ? this.#buttons().at(-1) : this.#buttons()[0])?.focus();
    }
  }

  #menuKey(e) {
    const list = this.#buttons();
    const i = list.indexOf(document.activeElement);
    const go = (n) => { e.preventDefault(); list[(n + list.length) % list.length]?.focus({ preventScroll: true }); };
    if (e.key === "ArrowDown") go(i + 1);
    else if (e.key === "ArrowUp") go(i < 0 ? list.length - 1 : i - 1);
    else if (e.key === "Home") go(0);
    else if (e.key === "End") go(list.length - 1);
    else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); this.close(true); }
    else if (e.key === "Tab") this.close(true); // focus goes back to the trigger, then Tab moves on from there
    else if (e.key.length === 1 && /\S/.test(e.key)) { // type-ahead
      const k = e.key.toLowerCase();
      const order = [...list.slice(i + 1), ...list.slice(0, i + 1)];
      order.find((b) => b.textContent.trim().toLowerCase().startsWith(k))?.focus();
    }
  }
}
customElements.define("menu-button", MenuButton);

export function menuButton({ label = "More", ariaLabel, items, disabled = false, className = "" } = {}) {
  const el = document.createElement("menu-button");
  el.label = label;
  if (ariaLabel) el.ariaLabel = ariaLabel;
  el.disabled = disabled;
  if (className) el.className = className;
  el.items = items || [];
  return el;
}
