// A tiny INI highlighter for ~/.aws/config and ~/.aws/credentials. Returns a
// DocumentFragment built from text nodes and spans (never parsed as markup).
function span(cls, text) {
  const s = document.createElement("span");
  s.className = `syn-${cls}`;
  s.textContent = text;
  return s;
}

export function highlightIni(text) {
  const frag = document.createDocumentFragment();
  const lines = text.split("\n");
  lines.forEach((line, i) => {
    const t = line.trimStart();
    const indent = line.slice(0, line.length - t.length);
    if (indent) frag.append(indent);
    if (t === "") {
      /* blank */
    } else if (t.startsWith("#") || t.startsWith(";")) {
      frag.append(span("comment", t));
    } else if (/^\[.*\]\s*$/.test(t)) {
      frag.append(span("section", t));
    } else {
      const m = /^([^=]+?)(\s*=\s*)(.*)$/.exec(t);
      if (m) {
        frag.append(span("key", m[1]), span("punct", m[2]));
        if (m[3]) frag.append(span("value", m[3]));
      } else {
        frag.append(t);
      }
    }
    if (i < lines.length - 1) frag.append("\n");
  });
  return frag;
}
