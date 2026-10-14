// Inline SVG icons (Lucide-style strokes, drawn here; no external assets).
const NS = "http://www.w3.org/2000/svg";
const PATHS = {
  copy: ["M9 9h10a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2v-9a2 2 0 0 1 2-2z", "M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"],
  check: ["M20 6 9 17l-5-5"],
  eye: ["M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z", "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z"],
  "eye-off": ["M10.7 5.1A10 10 0 0 1 12 5c6.4 0 10 7 10 7a17 17 0 0 1-2.2 3.2", "M6.6 6.6A17 17 0 0 0 2 12s3.6 7 10 7a9.7 9.7 0 0 0 5.4-1.6", "m2 2 20 20", "M9.9 9.9a3 3 0 0 0 4.2 4.2"],
  download: ["M12 3v12", "m7 10 5 5 5-5", "M5 21h14"],
  x: ["M18 6 6 18", "m6 6 12 12"],
  alert: ["M12 8v5", "M12 17h.01", "M10.3 3.9 2.4 17.5A2 2 0 0 0 4.1 20.5h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"],
  logout: ["M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4", "m16 17 5-5-5-5", "M21 12H9"],
  sun: ["M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8z", "M12 2v2", "M12 20v2", "m4.9 4.9 1.4 1.4", "m17.7 17.7 1.4 1.4", "M2 12h2", "M20 12h2", "m4.9 19.1 1.4-1.4", "m17.7 6.3 1.4-1.4"],
  moon: ["M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9z"],
  monitor: ["M3 4h18a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1z", "M8 21h8", "M12 17v4"],
  key: ["M21 2l-2 2m-7.6 7.6a5.5 5.5 0 1 1-7.8 7.8 5.5 5.5 0 0 1 7.8-7.8zm0 0L15.5 7.5m0 0 3 3L22 7l-3-3m-3.5 3.5L19 4"],
  bucket: ["M4 8h16l-1.7 11.2a2 2 0 0 1-2 1.8H7.7a2 2 0 0 1-2-1.8z", "M12 8c4.4 0 8-1 8-2.5S16.4 3 12 3 4 4 4 5.5 7.6 8 12 8z"],
};
export function icon(name, cls = "icon") {
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("class", cls);
  for (const d of PATHS[name] ?? []) {
    const p = document.createElementNS(NS, "path");
    p.setAttribute("d", d);
    svg.append(p);
  }
  return svg;
}
