import { h } from "../dom.js";
import { icon } from "../icons.js";
import { credentialsCard } from "./credentials.js";
import { load, save } from "../storage.js";
import { renderAdmin } from "./admin.js";

const THEMES = [["system", "monitor", "Theme: system"], ["light", "sun", "Theme: light"], ["dark", "moon", "Theme: dark"]];

function initials(name) {
  const parts = name.trim().split(/[\s._@-]+/).filter(Boolean);
  return ((parts[0]?.[0] ?? "?") + (parts.length > 1 ? parts.at(-1)[0] : "")).toUpperCase();
}
function greeting() {
  const hr = new Date().getHours();
  return hr < 5 ? "Working late" : hr < 12 ? "Good morning" : hr < 18 ? "Good afternoon" : "Good evening";
}

function skeletonRows(n) {
  return h("div", { class: "skel", "aria-hidden": "true" },
    Array.from({ length: n }, () =>
      h("div", { class: "skel-row" }, h("div", { class: "skel-dot" }),
        h("div", { class: "skel-stack" }, h("div", { class: "skel-line w60" }), h("div", { class: "skel-line w40" })),
        h("div", { class: "skel-pill" }))));
}

function section(iconName, title, note, body, adv) {
  return h("section", { class: "card panel", "aria-labelledby": `h-${title}` },
    h("div", { class: "panel-head" },
      h("h2", { id: `h-${title}`, text: title }),
      h("span", { class: "chip chip-accent" }, icon(iconName, "icon"), "Coming soon")),
    h("p", { class: "panel-note", text: note }),
    body,
    h("p", { class: "adv-only", text: adv }));
}

export function renderDashboard({ me, appName, onLogout, page = "home" }) {
  const root = document.documentElement;

  // Advanced mode: visual placeholder only; server-side persistence comes later.
  let advanced = load("advanced") === "true";
  const sw = h("button", { class: "switch", type: "button", role: "switch", "aria-checked": String(advanced) },
    h("span", { class: "switch-label", text: "Advanced" }), h("span", { class: "switch-track", "aria-hidden": "true" }));
  const shell = h("div", { class: "shell", "data-advanced": String(advanced) });
  sw.addEventListener("click", () => {
    advanced = !advanced;
    sw.setAttribute("aria-checked", String(advanced));
    shell.dataset.advanced = String(advanced);
    save("advanced", advanced);
  });

  // Theme: system -> light -> dark
  let ti = Math.max(0, THEMES.findIndex(([k]) => k === (root.dataset.theme ?? "system")));
  const themeBtn = h("button", { class: "btn btn-ghost btn-icon", type: "button" });
  const paintTheme = () => {
    const [, ic, label] = THEMES[ti];
    themeBtn.replaceChildren(icon(ic));
    themeBtn.setAttribute("aria-label", `${label}. Click to change.`);
    themeBtn.title = label;
  };
  themeBtn.addEventListener("click", () => {
    ti = (ti + 1) % THEMES.length;
    const k = THEMES[ti][0];
    if (k === "system") { delete root.dataset.theme; save("theme", null); } else { root.dataset.theme = k; save("theme", k); }
    paintTheme();
  });
  paintTheme();

  const name = me.displayName || me.username;
  const first = name.split(/\s+/)[0];
  const menuId = "user-menu";
  const header = h("header", { class: "hdr" },
    h("div", { class: "hdr-in" },
      h("a", { class: "brand", href: "#/", "aria-label": `${appName} home` },
        h("img", { src: "img/mark.svg", alt: "", width: 30, height: 30 }), h("span", { class: "brand-name", text: appName })),
      sw, themeBtn,
      h("div", { class: "user" },
        h("button", { class: "user-btn", type: "button", popovertarget: menuId, "aria-label": `Account menu for ${name}` },
          h("span", { class: "avatar", "aria-hidden": "true", text: initials(name) }), h("span", { class: "user-name", text: name })),
        h("div", { class: "menu", id: menuId, popover: "auto" },
          h("div", { class: "menu-who" }, h("b", { text: name }), h("span", { text: me.email || me.username }),
            me.isAdmin && h("span", {}, h("span", { class: "chip chip-accent", text: "Administrator" }))),
          me.isAdmin && (page === "admin"
            ? h("a", { class: "btn btn-ghost", href: "#/" }, icon("home"), "My workspace")
            : h("a", { class: "btn btn-ghost", href: "#/admin" }, icon("shield"), "Administration")),
          h("button", { class: "btn btn-ghost", type: "button", on: { click: onLogout } }, icon("logout"), "Sign out")))));

  const home = () => h("main", { class: "main", id: "main" },
    h("div", { class: "hero" },
      h("span", { class: "eyebrow", text: "Your workspace" }),
      h("h1", { text: `${greeting()}, ${first}.` }),
      h("p", { text: "Your S3 credentials are below. Bucket management is coming next." }),
      h("span", { class: "chip adv-note" }, h("span", { class: "dot" }), "Advanced mode on")),
    h("div", { class: "grid" },
      credentialsCard(),
      section("bucket", "Buckets", "Create, share and manage your buckets, with usage at a glance.", skeletonRows(4),
        "Advanced: policies, CORS, lifecycle and tagging will appear here.")));

  shell.append(
    h("a", { class: "skip-link", href: "#main", text: "Skip to content" }),
    header, page === "admin" && me.isAdmin ? renderAdmin() : home(),
    h("footer", { class: "foot", text: `${appName} · signed in as ${me.username}` }));
  return shell;
}
