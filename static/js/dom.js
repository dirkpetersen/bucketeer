// Tiny element builder. Text goes through textContent / text nodes, never
// the HTML parser, so server- or user-supplied strings cannot inject markup.
export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k === "class") el.className = v;
    else if (k === "on") for (const [ev, fn] of Object.entries(v)) el.addEventListener(ev, fn);
    else if (k === "text") el.textContent = v;
    // Custom elements take props as properties: a value like a secret must never
    // become an attribute (visible in the DOM inspector).
    else if (k in el && (el.localName.includes("-") || (typeof v !== "string" && k !== "dataset"))) el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  append(el, children);
  return el;
}
export function append(el, children) {
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    el.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return el;
}
