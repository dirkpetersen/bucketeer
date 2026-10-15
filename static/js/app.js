import { getDemo, getMe, logout } from "./api.js";
import { renderLogin } from "./views/login.js";
import { renderDashboard } from "./views/dashboard.js";
import { h } from "./dom.js";
import { initAdvanced } from "./advanced.js";
import { toast } from "./components/toast.js";
import "./components/secret-field.js";
import "./components/code-panel.js";
import "./components/confirm-dialog.js";

const appName = document.title || "Bucketeer";
const root = document.getElementById("app");
let current = null; // "login" | "dashboard"

// Swap views with a cross-fade where View Transitions exist; instantly otherwise.
function swap(node, name) {
  const apply = () => {
    root.replaceChildren(node);
    current = name;
  };
  if (document.startViewTransition && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
    document.startViewTransition(apply);
  } else {
    apply();
  }
}

// Load the per-user Advanced setting from /api/me before the first paint.
function showMe(user) {
  initAdvanced(user.advancedMode);
  showDashboard(user);
}
const showLogin = () => swap(renderLogin({ appName, onSuccess: async () => {
    try { showMe(await getMe()); } catch (err) { toast.error(err.message || "Could not load your account."); }
  } }), "login");
let me = null;
// Hash routes: "#/admin" is the administration page (admins only; the server
// enforces the role as well), "#/profile" the user profile, anything else is the workspace.
function showDashboard(user = me) {
  me = user;
  const page = location.hash === "#/admin" && me.isAdmin ? "admin" : location.hash === "#/profile" ? "profile" : "home";
  swap(renderDashboard({ me, appName, onLogout: doLogout, page }), page);
  const titles = { admin: "Administration", profile: "Profile" };
  document.title = titles[page] ? `${titles[page]} · ${appName}` : appName;
}
window.addEventListener("hashchange", () => {
  if (current && current !== "login") showDashboard();
});

async function doLogout() {
  me = null;
  if (location.hash) history.replaceState(null, "", location.pathname + location.search);
  try {
    await logout();
  } catch {
    /* session is gone either way; fall through to the login view */
  }
  showLogin();
}

// Any API call that comes back 401 (expired session) returns to login.
window.addEventListener("bucketeer:unauthorized", () => {
  if (current !== "login") {
    showLogin();
    toast.show("Your session has ended. Please sign in again.");
  }
});

// "Demo mode" pill, only when the server says so (mock/demo.py).
getDemo()
  .then(({ demo }) => {
    if (!demo) return;
    document.body.classList.add("has-demo");
    document.body.append(
      h(
        "div",
        { class: "demo-banner", role: "note" },
        h("strong", {}, "Demo mode"),
        " fake directory, mock cluster, temporary data",
      ),
    );
  })
  .catch(() => {});

(async function start() {
  try {
    showMe(await getMe());
  } catch (err) {
    showLogin();
    if (err.status !== 401) toast.error(err.message);
  }
})();
