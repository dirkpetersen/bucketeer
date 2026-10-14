import { h } from "../dom.js";
import { icon } from "../icons.js";
import { credentialsCard } from "./credentials.js";
import { save } from "../storage.js";
import { isAdvanced, setAdvanced, subscribe } from "../advanced.js";
import { toast } from "../components/toast.js";
import { renderAdmin } from "./admin.js";
import { renderProfile } from "./profile.js";
import { bucketsCard } from "./buckets.js";
import { sharedWithMeCard } from "./shared-with-me.js";

const THEMES = [["system", "monitor", "Theme: system"], ["light", "sun", "Theme: light"], ["dark", "moon", "Theme: dark"]];

function initials(name) {
  const parts = name.trim().split(/[\s._@-]+/).filter(Boolean);
  return ((parts[0]?.[0] ?? "?") + (parts.length > 1 ? parts.at(-1)[0] : "")).toUpperCase();
}
function greeting() {
  const hr = new Date().getHours();
  return hr < 5 ? "Working late" : hr < 12 ? "Good morning" : hr < 18 ? "Good afternoon" : "Good evening";
}

export function renderDashboard({ me, appName, onLogout, page = "home" }) {
  const root = document.documentElement;

  // Advanced mode: global store (advanced.js), server-persisted per user.
  const sw = h("button", { class: "switch", type: "button", role: "switch", "aria-checked": String(isAdvanced()) },
    h("span", { class: "switch-label", text: "Advanced" }), h("span", { class: "switch-track", "aria-hidden": "true" }));
  const shell = h("div", { class: "shell", "data-advanced": String(isAdvanced()) });
  const unsub = subscribe((on) => {
    if (!shell.isConnected) { unsub(); return; } // this shell was replaced
    sw.setAttribute("aria-checked", String(on));
    shell.dataset.advanced = String(on);
  });
  sw.addEventListener("click", () => {
    setAdvanced(!isAdvanced()).catch((err) => toast.error(err.message || "Could not save the Advanced setting."));
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
          page === "profile"
            ? h("a", { class: "btn btn-ghost", href: "#/" }, icon("home"), "My workspace")
            : h("a", { class: "btn btn-ghost", href: "#/profile" }, icon("user"), "Profile"),
          me.isAdmin && (page === "admin"
            ? h("a", { class: "btn btn-ghost", href: "#/" }, icon("home"), "My workspace")
            : h("a", { class: "btn btn-ghost", href: "#/admin" }, icon("shield"), "Administration")),
          h("button", { class: "btn btn-ghost", type: "button", on: { click: onLogout } }, icon("logout"), "Sign out")))));

  const home = () => h("main", { class: "main", id: "main" },
    h("div", { class: "hero" },
      h("span", { class: "eyebrow", text: "Your workspace" }),
      h("h1", { text: `${greeting()}, ${first}.` }),
      h("p", { text: "Your S3 credentials and your buckets, in one place." }),
      h("span", { class: "chip adv-note" }, h("span", { class: "dot" }), "Advanced mode on")),
    h("div", { class: "grid" },
      credentialsCard(),
      bucketsCard(),
      sharedWithMeCard()));

  shell.append(
    h("a", { class: "skip-link", href: "#main", text: "Skip to content" }),
    header, page === "admin" && me.isAdmin ? renderAdmin() : page === "profile" ? renderProfile({ me }) : home(),
    h("footer", { class: "foot", text: `${appName} · signed in as ${me.username}` }));
  return shell;
}
