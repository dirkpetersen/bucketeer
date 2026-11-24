import { h } from "../dom.js";
import { icon } from "../icons.js";
import { searchPeople } from "../api.js";

// personPicker({ id, input, onPick, onUnauthorized }) -> { nodes, close, dispose }
// Type-ahead on an existing text field: from 2 characters on it asks /api/people/search
// (username, first name, last name, display name or email; every word a prefix) and shows a
// listbox of eligible people. Choosing one calls onPick({ username, displayName, email }).
// Suggestions only: the caller still shares through the exact lookup. Enter without an
// active option is left to the form (the exact "Find"); `searchNow()` shows the list at once.
const DEBOUNCE_MS = 250;
const MIN_CHARS = 2;

export function personPicker({ id, input, onPick, onUnauthorized = () => {} }) {
  let options = []; // [{ id, person, el }]
  let active = -1;
  let ticket = 0; // drops answers of superseded requests
  let timer = 0;

  input.setAttribute("role", "combobox");
  input.setAttribute("aria-autocomplete", "list");
  input.setAttribute("aria-expanded", "false");
  input.setAttribute("aria-controls", `${id}-pl`);
  const list = h("ul", { class: "slk-list pp-list", id: `${id}-pl`, role: "listbox", "aria-label": "Matching people", hidden: true });
  const status = h("p", { class: "slk-status", role: "status", "aria-live": "polite" });

  function setOpen(open) {
    list.hidden = !open || options.length === 0;
    input.setAttribute("aria-expanded", String(!list.hidden));
    if (list.hidden) { active = -1; input.removeAttribute("aria-activedescendant"); }
  }
  // Move the active option by `dir` (+1 / -1), skipping yourself (not selectable).
  function move(dir) {
    const len = options.length;
    if (!options.some((o) => !o.person.isYou)) return;
    let n = active < 0 ? (dir > 0 ? -1 : len) : active;
    do n = (n + dir + len) % len; while (options[n].person.isYou);
    active = n;
    options.forEach((o, m) => o.el.setAttribute("aria-selected", String(m === active)));
    input.setAttribute("aria-activedescendant", options[active].id);
    options[active].el.scrollIntoView({ block: "nearest" });
  }

  function render(res) {
    options = res.people.map((p, n) => {
      const oid = `${id}-p${n}`;
      const el = h("li", { class: "slk-opt pp-opt", id: oid, role: "option", "aria-selected": "false", "aria-disabled": p.isYou ? "true" : null },
        icon("user", "icon slk-ic"),
        h("span", { class: "pp-who" },
          h("b", { class: "pp-name", text: p.displayName || p.username }),
          h("span", { class: "pp-meta" },
            h("code", { class: "shd-user", text: p.username }),
            p.email && h("span", { text: p.email }),
            p.isYou && h("span", { class: "chip", text: "You" }))));
      el.addEventListener("mousedown", (e) => e.preventDefault()); // keep focus in the field
      el.addEventListener("click", () => choose(n));
      return { id: oid, person: p, el };
    });
    list.replaceChildren(...options.map((o) => o.el));
    const count = options.length;
    status.textContent = count
      ? `${count} ${count === 1 ? "person" : "people"}${res.truncated ? " shown, keep typing to narrow them down" : ""}.`
      : res.truncated
        ? "Too many matches. Keep typing."
        : "No one found. Check the spelling, or type the full username or email and press Find.";
    setOpen(count > 0);
    if (count) move(1);
  }

  async function suggest() {
    const q = input.value.trim();
    const mine = ++ticket;
    if (q.replace(/\s+/g, "").length < MIN_CHARS) {
      options = [];
      setOpen(false);
      status.textContent = "";
      return;
    }
    try {
      const res = await searchPeople(q);
      if (mine === ticket) render(res);
      return options.length;
    } catch (err) {
      if (mine !== ticket) return;
      if (err.status === 401) { onUnauthorized(); return; }
      options = [];
      setOpen(false);
      status.textContent = err.message || "Could not search the directory.";
    }
  }
  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(suggest, DEBOUNCE_MS);
  }

  function choose(n) {
    const opt = options[n];
    if (!opt || opt.person.isYou) return;
    ticket++;
    clearTimeout(timer);
    options = [];
    setOpen(false);
    status.textContent = "";
    onPick(opt.person);
  }

  input.addEventListener("input", schedule);
  input.addEventListener("focus", () => { if (options.length) setOpen(true); });
  input.addEventListener("blur", () => setOpen(false));
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (list.hidden) { if (options.length) setOpen(true); else { clearTimeout(timer); suggest(); } }
      if (options.length) move(e.key === "ArrowDown" ? 1 : -1);
    } else if (e.key === "Enter" && !list.hidden && active >= 0) {
      e.preventDefault();
      choose(active);
    } else if (e.key === "Escape" && !list.hidden) {
      e.preventDefault();
      e.stopPropagation(); // closes the list, not the dialog
      setOpen(false);
    }
  });

  return {
    nodes: [list, status],
    // Search now (no debounce), e.g. after an exact lookup found nobody. -> number of people.
    async searchNow() { clearTimeout(timer); return (await suggest()) ?? 0; },
    close() { ticket++; clearTimeout(timer); options = []; setOpen(false); status.textContent = ""; },
    dispose() { clearTimeout(timer); ticket++; },
  };
}
