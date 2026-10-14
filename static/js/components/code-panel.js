import { h } from "../dom.js";
import { icon } from "../icons.js";
import { copyText } from "../clipboard.js";
import { highlightIni } from "../ini.js";
import { toast } from "./toast.js";

// <code-panel> with tabs, copy, download and INI highlighting.
//   panel.tabs = [{ label: "~/.aws/config", filename: "config", content: "..." }, ...]
//   A tab may set `locked: "reason"`: copy and download are disabled and the reason is shown.
//   panel.hint = "Optional note shown under the code."
// Tabs follow the WAI-ARIA pattern (arrow keys, Home/End, roving tabindex).
let seq = 0;

class CodePanel extends HTMLElement {
  #tabs = [];
  #active = 0;
  #hint = "";
  #id = `cp${++seq}`;
  #timer = 0;

  set tabs(v) { this.#tabs = v ?? []; this.#active = Math.min(this.#active, Math.max(0, this.#tabs.length - 1)); this.#render(); }
  get tabs() { return this.#tabs; }
  set hint(v) { this.#hint = v ?? ""; this.#render(); }

  connectedCallback() { this.#render(); }
  disconnectedCallback() { clearTimeout(this.#timer); }

  #select(i, focus = false) {
    this.#active = (i + this.#tabs.length) % this.#tabs.length;
    this.#render();
    if (focus) this.querySelector('[role="tab"][aria-selected="true"]').focus();
  }

  #render() {
    if (!this.isConnected) return;
    const tabs = this.#tabs;
    if (!tabs.length) return this.replaceChildren();
    const cur = tabs[this.#active];
    const tabEls = tabs.map((t, i) =>
      h("button", {
        class: "cp-tab", role: "tab", type: "button", id: `${this.#id}-t${i}`,
        "aria-selected": String(i === this.#active), "aria-controls": `${this.#id}-p`,
        tabindex: i === this.#active ? "0" : "-1", text: t.label,
        on: {
          click: () => this.#select(i),
          keydown: (e) => {
            const k = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 }[e.key];
            if (k !== undefined) { e.preventDefault(); this.#select(k, true); }
          },
        },
      }),
    );
    const copyBtn = h("button", { class: "btn btn-ghost", type: "button", "aria-label": `Copy ${cur.label}` },
      icon("copy"), h("span", { class: "cp-label", text: "Copy" }));
    copyBtn.addEventListener("click", async () => {
      const ok = await copyText(cur.content);
      if (!ok) return toast.error("Could not copy. Select the text and copy it manually.");
      copyBtn.dataset.done = "true";
      copyBtn.replaceChildren(icon("check"), h("span", { class: "cp-label", text: "Copied" }));
      toast.show(`${cur.label} copied`);
      clearTimeout(this.#timer);
      this.#timer = setTimeout(() => {
        delete copyBtn.dataset.done;
        copyBtn.replaceChildren(icon("copy"), h("span", { class: "cp-label", text: "Copy" }));
      }, 1800);
    });
    const dlBtn = h("button", { class: "btn btn-ghost", type: "button", "aria-label": `Download ${cur.filename}` },
      icon("download"), h("span", { class: "cp-label", text: "Download" }));
    dlBtn.addEventListener("click", () => this.#download(cur));
    if (cur.locked) {
      copyBtn.disabled = dlBtn.disabled = true;
      copyBtn.title = dlBtn.title = cur.locked;
    }

    const body = h("pre", {
      class: "cp-body", id: `${this.#id}-p`, role: "tabpanel", tabindex: "0",
      "aria-labelledby": `${this.#id}-t${this.#active}`,
    }, h("code"));
    body.firstChild.append(cur.language === "plain" ? cur.content : highlightIni(cur.content));

    this.replaceChildren(
      h("div", { class: "cp" },
        h("div", { class: "cp-bar" },
          h("div", { class: "cp-tabs", role: "tablist", "aria-label": "Files" }, tabEls),
          h("div", { class: "cp-actions" }, copyBtn, dlBtn)),
        body,
        (cur.locked || this.#hint) && h("p", { class: "cp-hint", text: [cur.locked, this.#hint].filter(Boolean).join(" ") })),
    );
  }

  #download(t) {
    const url = URL.createObjectURL(new Blob([t.content], { type: "text/plain;charset=utf-8" }));
    const a = h("a", { href: url, download: t.filename ?? "file.txt", class: "visually-hidden" });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
}
customElements.define("code-panel", CodePanel);
