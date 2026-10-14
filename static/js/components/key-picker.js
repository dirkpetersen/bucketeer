import { h } from "../dom.js";
import { icon } from "../icons.js";
import { listBucketObjects } from "../api.js";
import { formatBytes } from "../format.js";

// keyPicker({ id, bucket, onChange, onError, onUnauthorized, onEnter }) -> { input, nodes, dispose }
// A text field for one object key with prefix autocomplete from a read-only, one-page listing
// (not a file browser). `nodes` are the input, hint, list and status elements to place in a
// field; `onEnter()` (optional) replaces the form submit on Enter; `onChange()` fires when the text changes by typing or choosing; folders expand in
// place; a chosen file closes the list. Used by the share-link and object-ACL dialogs.
const DEBOUNCE_MS = 220;

export function keyPicker({ id, bucket, onChange = () => {}, onError = () => {}, onUnauthorized = () => {}, onEnter = null }) {
  let options = []; // [{ id, kind: "folder" | "file", value, el }]
  let active = -1;
  let ticket = 0; // drops answers of superseded autocomplete requests
  let timer = 0;

  const input = h("input", {
    class: "input slk-key", id: `${id}-key`, type: "text", role: "combobox", autocomplete: "off", autocapitalize: "off",
    spellcheck: "false", placeholder: "reports/2026/summary.pdf", "aria-autocomplete": "list", "aria-expanded": "false",
    "aria-controls": `${id}-list`, "aria-describedby": `${id}-hint`, maxlength: 1024,
  });
  const hint = h("p", { class: "slk-hint", id: `${id}-hint`, text: "Start typing to see matching files. Choose a folder to look inside it." });
  const list = h("ul", { class: "slk-list", id: `${id}-list`, role: "listbox", "aria-label": "Matching files and folders", hidden: true });
  const status = h("p", { class: "slk-status", role: "status", "aria-live": "polite" });

  function setOpen(open) {
    list.hidden = !open || options.length === 0;
    input.setAttribute("aria-expanded", String(!list.hidden));
    if (list.hidden) { active = -1; input.removeAttribute("aria-activedescendant"); }
  }
  function setActive(i) {
    if (!options.length) return;
    active = (i + options.length) % options.length;
    options.forEach((o, n) => o.el.setAttribute("aria-selected", String(n === active)));
    const cur = options[active];
    input.setAttribute("aria-activedescendant", cur.id);
    cur.el.scrollIntoView({ block: "nearest" });
  }

  function render(res) {
    options = [];
    const items = [];
    const add = (kind, value, label, meta) => {
      const oid = `${id}-o${options.length}`;
      const el = h("li", { class: "slk-opt", id: oid, role: "option", "aria-selected": "false", "data-kind": kind },
        icon(kind === "folder" ? "right" : "download", "icon slk-ic"),
        h("span", { class: "slk-name", text: label }),
        meta && h("span", { class: "slk-meta", text: meta }));
      el.addEventListener("mousedown", (e) => e.preventDefault()); // keep focus in the field
      el.addEventListener("click", () => choose(options.find((o) => o.id === oid)));
      options.push({ id: oid, kind, value, el });
      items.push(el);
    };
    for (const p of res.prefixes) add("folder", p.prefix, p.prefix.slice(res.prefix.length) || p.prefix);
    for (const e of res.entries) add("file", e.key, e.key.slice(res.prefix.length) || e.key, formatBytes(e.size));
    list.replaceChildren(...items);
    status.textContent = items.length
      ? `${items.length} ${items.length === 1 ? "match" : "matches"}${res.truncated ? ", keep typing to narrow them down" : ""}.`
      : "No files or folders start with that.";
    setOpen(items.length > 0);
    if (items.length) setActive(0);
  }

  async function suggest() {
    const mine = ++ticket;
    try {
      const res = await listBucketObjects(bucket, input.value);
      if (mine !== ticket) return;
      render(res);
    } catch (err) {
      if (mine !== ticket) return;
      if (err.status === 401) { onUnauthorized(); return; }
      options = [];
      setOpen(false);
      status.textContent = "";
      onError(err.message || "Could not look up file names.");
    }
  }
  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(suggest, DEBOUNCE_MS);
  }

  function choose(opt) {
    if (!opt) return;
    input.value = opt.value;
    onChange();
    if (opt.kind === "folder") {
      suggest(); // folders expand in place
    } else {
      ticket++;
      options = [];
      setOpen(false);
      status.textContent = "";
    }
    input.focus();
  }

  input.addEventListener("input", () => { onChange(); schedule(); });
  input.addEventListener("focus", () => { if (!options.length && !input.value) suggest(); else if (options.length) setOpen(true); });
  input.addEventListener("blur", () => setOpen(false));
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (list.hidden) { if (options.length) setOpen(true); else suggest(); }
      if (options.length) setActive(e.key === "ArrowDown" ? active + 1 : active - 1);
    } else if (e.key === "Enter" && !list.hidden && active >= 0) {
      e.preventDefault();
      choose(options[active]);
    } else if (e.key === "Enter" && onEnter) {
      e.preventDefault();
      onEnter();
    } else if (e.key === "Escape" && !list.hidden) {
      e.preventDefault();
      e.stopPropagation(); // closes the list, not the dialog
      setOpen(false);
    }
  });

  return {
    input,
    nodes: [input, hint, list, status],
    close() { setOpen(false); },
    dispose() { clearTimeout(timer); ticket++; },
  };
}
