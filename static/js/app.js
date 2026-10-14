import { getMe, logout } from "./api.js";
import { renderLogin } from "./views/login.js";
import { renderDashboard } from "./views/dashboard.js";
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

const showLogin = () => swap(renderLogin({ appName, onSuccess: showDashboard }), "login");
const showDashboard = (me) => swap(renderDashboard({ me, appName, onLogout: doLogout }), "dashboard");

async function doLogout() {
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

(async function start() {
  try {
    showDashboard(await getMe());
  } catch (err) {
    showLogin();
    if (err.status !== 401) toast.error(err.message);
  }
})();
