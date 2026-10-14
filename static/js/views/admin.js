import { h } from "../dom.js";
import { icon } from "../icons.js";
import { adminBucketKeys, adminBuckets, adminRevokeBucketKey, adminUsers, setBucketQuota, setUserQuota, suspendUser, unsuspendUser } from "../api.js";
import { formatBytes, formatCount, quotaText } from "../format.js";
import { toast } from "../components/toast.js";
import { confirmDialog } from "../components/confirm-dialog.js";
import { quotaDialog } from "../components/quota-dialog.js";
import { renderSettings } from "./admin-settings.js";

// Administration: all users and buckets, quota editing, suspend/unsuspend.
// The server enforces the role on every call; this view is only shown to admins.

const collator = new Intl.Collator(undefined, { sensitivity: "base", numeric: true });
const quotaSize = (q) => (q.enabled && q.maxSize != null ? q.maxSize : Infinity);

function badge(text, kind = "") {
  return h("span", { class: `chip ${kind}`.trim() }, kind === "chip-warn" || kind === "chip-ok" ? h("span", { class: "dot" }) : null, text);
}

function meter(used, q) {
  if (!q.enabled || q.maxSize == null || q.maxSize === 0) return null;
  const pct = Math.min(100, (used / q.maxSize) * 100);
  const fill = h("span", { class: "meter-fill" });
  fill.style.width = `${pct}%`;
  return h("span", { class: "meter", "data-level": pct >= 90 ? "high" : pct >= 70 ? "mid" : "ok", role: "img", "aria-label": `${Math.round(pct)}% of quota used` }, fill);
}

// --- column definitions ------------------------------------------------------

function userColumns(act) {
  return [
    { key: "user", label: "User", sort: (r) => r.name || r.uid, cell: (r) => h("div", { class: "who" },
        h("b", { text: r.name || r.uid }),
        h("code", { text: r.uid }),
        r.email && h("span", { class: "sub", text: r.email })) },
    { key: "status", label: "Status", sort: (r) => (r.suspended ? 0 : 1), cell: (r) => h("div", { class: "chips" },
        r.suspended ? badge("Suspended", "chip-warn") : badge("Active", "chip-ok"),
        r.bucketKey && badge("Bucket key", "")) },
    { key: "buckets", label: "Buckets", num: true, sort: (r) => r.bucketCount, cell: (r) => formatCount(r.bucketCount) },
    { key: "quota", label: "User quota", sort: (r) => quotaSize(r.quota), cell: (r) => h("span", { class: r.quota.enabled ? "" : "muted", text: quotaText(r.quota) }) },
    { key: "actions", label: "Actions", noSort: true, cell: (r) => h("div", { class: "acts" },
        act.button("edit", "Quota", () => act.quota("user", r), `Edit quota of ${r.uid}`),
        r.suspended
          ? act.button("play", "Unsuspend", () => act.suspend(r, false), `Unsuspend ${r.uid}`)
          : act.button("pause", "Suspend", () => act.suspend(r, true), `Suspend ${r.uid}`, "danger")) },
  ];
}

function bucketColumns(act) {
  return [
    { key: "bucket", label: "Bucket", sort: (r) => r.name, cell: (r) => h("div", { class: "who" },
        h("b", { class: "mono", text: r.name }), r.tenant && h("span", { class: "sub", text: `tenant ${r.tenant}` })) },
    { key: "owner", label: "Owner", sort: (r) => r.owner, cell: (r) => h("code", { text: r.owner }) },
    { key: "budget", label: "Budget number", sort: (r) => r.budgetNumber ?? "", cell: (r) =>
        r.budgetNumber ? h("div", { class: "who" }, h("code", { text: r.budgetNumber }),
          r.budgetMatches === false && badge("Format mismatch", "chip-warn")) : badge("Missing", "chip-warn") },
    { key: "usage", label: "Usage", num: true, sort: (r) => r.bytes, cell: (r) => h("div", { class: "usage" },
        h("b", { text: formatBytes(r.bytes) }), h("span", { class: "sub", text: `${formatCount(r.objects)} objects` })) },
    { key: "quota", label: "Quota", sort: (r) => quotaSize(r.quota), cell: (r) => h("div", { class: "usage" },
        h("span", { class: r.quota.enabled ? "" : "muted", text: quotaText(r.quota) }), meter(r.bytes, r.quota)) },
    { key: "actions", label: "Actions", noSort: true, cell: (r) => h("div", { class: "acts" },
        act.button("edit", "Quota", () => act.quota("bucket", r), `Edit quota of ${r.name}`)) },
  ];
}

const ACCESS = { read: "Read only", readwrite: "Read and write" };
const KEY_STATE = { active: ["chip-ok", "Active"], suspended: ["chip-warn", "Suspended"], pending: ["chip-warn", "Needs clean-up"], failed: ["chip-warn", "Needs clean-up"], revoked: ["", "Revoked"] };
const shortDate = (iso) => (iso ? iso.slice(0, 10) : "");

function keyColumns(act) {
  const stateOf = (r) => (r.state !== "revoked" && r.suspended ? "suspended" : r.state);
  return [
    { key: "bucket", label: "Bucket", sort: (r) => r.bucket, cell: (r) => h("div", { class: "who" },
        h("b", { class: "mono", text: r.bucket }), h("span", { class: "sub", text: `key ${r.label}` })) },
    { key: "owner", label: "Owner", sort: (r) => r.ownerAd, cell: (r) => h("code", { text: r.ownerAd }) },
    { key: "access", label: "Access", sort: (r) => r.access, cell: (r) => badge(ACCESS[r.access] ?? r.access) },
    { key: "state", label: "State", sort: (r) => stateOf(r), cell: (r) => {
        const [cls, label] = KEY_STATE[stateOf(r)] ?? ["", r.state];
        return h("div", { class: "chips" }, badge(label, cls),
          r.statementPendingCleanup && badge("Policy entry left", "chip-warn"));
      } },
    { key: "created", label: "Created", sort: (r) => r.createdAt, cell: (r) => h("span", { text: shortDate(r.createdAt) }) },
    { key: "actions", label: "Actions", noSort: true, cell: (r) => h("div", { class: "acts" },
        r.state === "revoked"
          ? (r.statementPendingCleanup ? act.button("refresh", "Retry clean-up", () => act.revokeKey(r), `Retry removing the policy entry of key ${r.label}`) : null)
          : act.button("trash", "Revoke", () => act.revokeKey(r), `Revoke key ${r.label} of ${r.ownerAd}`, "danger")) },
  ];
}

// --- the view -----------------------------------------------------------------

export function renderAdmin() {
  const state = {
    tab: "users",
    q: "",
    sort: { users: { key: "user", dir: 1 }, buckets: { key: "bucket", dir: 1 }, keys: { key: "bucket", dir: 1 } },
    only: { users: false, buckets: false, keys: false }, // suspended only / missing budget only / revoked too
    data: { users: { status: "loading", rows: [] }, buckets: { status: "loading", rows: [] }, keys: { status: "loading", rows: [] } },
  };
  const busy = new Set();

  const act = {
    button(ic, label, onClick, aria, kind = "") {
      const b = h("button", { class: `btn btn-sm ${kind === "danger" ? "btn-quiet-danger" : ""}`.trim(), type: "button", "aria-label": aria, on: { click: onClick } }, icon(ic), h("span", { text: label }));
      return b;
    },
    async quota(kind, row) {
      const isUser = kind === "user";
      const id = isUser ? row.uid : row.name;
      const saved = await quotaDialog({
        title: isUser ? "User quota" : "Bucket quota",
        subject: h("span", {}, isUser ? "Applies to all buckets of " : "Applies to ", h("strong", { text: id }), "."),
        hint: isUser ? " Combined size and object count." : " Use 1 or more; to block writes, suspend the owner.",
        quota: row.quota,
        min: isUser ? 0 : 1, // croit reads a bucket bound of 0 as no bound
        onSave: async (body) => {
          const res = await (isUser ? setUserQuota(id, body) : setBucketQuota(id, body));
          row.quota = res.quota;
        },
      });
      if (saved) {
        toast.show(`Quota saved for ${id}.`);
        render();
      }
    },
    async revokeKey(row) {
      const retry = row.state === "revoked";
      const ok = await confirmDialog(retry
        ? { title: "Retry the clean-up?", message: `The key ${row.label} is already revoked. This removes its leftover entry from the policy of ${row.bucket}.`, confirmLabel: "Retry" }
        : { title: `Revoke key ${row.label}?`, message: `${row.ownerAd}'s tools that use this key for ${row.bucket} stop working right away, and the key cannot be restored. The owner's own S3 key is not affected.`, confirmLabel: "Revoke key", danger: true });
      if (!ok || busy.has(`key${row.id}`)) return;
      busy.add(`key${row.id}`);
      try {
        const res = await adminRevokeBucketKey(row.id);
        toast.show(res.statementRemoved ? `Key ${row.label} revoked.` : `Key ${row.label} revoked. Its policy entry is harmless and stays until the owner's account works again.`);
      } catch (err) {
        toast.error(err.message);
      } finally {
        busy.delete(`key${row.id}`);
        load("keys");
      }
    },
    async suspend(row, suspend) {
      const ok = await confirmDialog(suspend
        ? { title: `Suspend ${row.uid}?`, message: `${row.name || row.uid} loses S3 access right away, and so do all of their bucket keys. Nothing is deleted; you can unsuspend at any time.`, confirmLabel: "Suspend", danger: true }
        : { title: `Unsuspend ${row.uid}?`, message: `${row.name || row.uid} gets S3 access back, together with the bucket keys that were suspended with the account.`, confirmLabel: "Unsuspend" });
      if (!ok || busy.has(row.uid)) return;
      busy.add(row.uid);
      try {
        const res = await (suspend ? suspendUser(row.uid) : unsuspendUser(row.uid));
        row.suspended = res.suspended;
        const n = res.keys.length;
        toast.show(`${row.uid} ${suspend ? "suspended" : "unsuspended"}${n ? `, with ${n} bucket key${n > 1 ? "s" : ""}` : ""}.`);
      } catch (err) {
        toast.error(err.message);
        load("users"); // partial failures change state on the server
      } finally {
        busy.delete(row.uid);
        render();
      }
    },
  };
  const columns = { users: userColumns(act), buckets: bucketColumns(act), keys: keyColumns(act) };

  // --- data ---
  async function load(tab) {
    state.data[tab] = { status: "loading", rows: state.data[tab].rows };
    render();
    try {
      const res = await (tab === "users" ? adminUsers() : tab === "keys" ? adminBucketKeys() : adminBuckets());
      state.data[tab] = { status: "ready", rows: res[tab] };
    } catch (err) {
      if (err.status !== 401) state.data[tab] = { status: "error", rows: [], error: err.message };
    }
    render();
  }

  // --- static frame ---
  const statsEl = h("div", { class: "stats" });
  const search = h("input", { class: "input search-input", type: "search", placeholder: "Search users", "aria-label": "Search", autocomplete: "off", spellcheck: "false" });
  const sortSel = h("select", { class: "input sort-select", "aria-label": "Sort by" });
  const dirBtn = h("button", { class: "btn btn-icon sort-dir", type: "button" });
  const onlyBtn = h("button", { class: "btn btn-filter", type: "button", "aria-pressed": "false" });
  const refreshBtn = h("button", { class: "btn btn-icon", type: "button", "aria-label": "Refresh", title: "Refresh" }, icon("refresh"));
  const exportEl = h("a", { class: "btn", href: "/api/admin/export/chargeback.csv", download: "", title: "One row per bucket: bucket, tenant, owner, budget number and status, objects, bytes, GiB, quota, created. Opens in Excel." }, "Export CSV");
  const exportNote = h("p", { class: "export-note", text: "Export CSV lists every bucket (not just the filtered ones): bucket, tenant, owner, budget number and status, objects, bytes, GiB, quota and creation date, for chargeback. UTF-8, opens in Excel." });
  const filtersEl = h("div", { class: "admin-filters" },
    h("div", { class: "search" }, icon("search", "icon search-ic"), search), sortSel, dirBtn, onlyBtn, exportEl);
  const countEl = h("span", { class: "count", "aria-live": "polite" });
  const tableHost = h("div", { class: "table-host" });
  const settings = renderSettings();
  const TAB_KEYS = ["users", "buckets", "keys", "settings"];
  const tabs = [["users", "Users", "users"], ["buckets", "Buckets", "bucket"], ["keys", "Bucket keys", "key"], ["settings", "Settings", "sliders"]].map(([key, label, ic]) =>
    h("button", { class: "tab", type: "button", role: "tab", id: `tab-${key}`, "aria-controls": "admin-panel", on: { click: () => switchTab(key) } },
      icon(ic), label, h("span", { class: "tab-count", id: `tc-${key}` })));
  const tablist = h("div", { class: "tabs", role: "tablist", "aria-label": "Administration" }, tabs);
  tablist.addEventListener("keydown", (e) => {
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      const step = e.key === "ArrowRight" ? 1 : -1;
      const next = TAB_KEYS[(TAB_KEYS.indexOf(state.tab) + step + TAB_KEYS.length) % TAB_KEYS.length];
      switchTab(next);
      document.getElementById(`tab-${next}`)?.focus();
    }
  });

  function switchTab(key) {
    if (state.tab === key) return;
    state.tab = key;
    state.q = "";
    search.value = "";
    paintControls();
    render();
  }
  search.addEventListener("input", () => { state.q = search.value.trim().toLowerCase(); render(); });
  sortSel.addEventListener("change", () => { state.sort[state.tab].key = sortSel.value; render(); });
  dirBtn.addEventListener("click", () => { state.sort[state.tab].dir *= -1; render(); });
  onlyBtn.addEventListener("click", () => { state.only[state.tab] = !state.only[state.tab]; render(); });
  refreshBtn.addEventListener("click", () => (state.tab === "settings" ? settings.load() : load(state.tab)));

  function paintControls() {
    const tab = state.tab;
    if (tab === "settings") return;
    search.placeholder = tab === "users" ? "Search users, names, e-mail" : tab === "keys" ? "Search owners, buckets, labels" : "Search buckets, owners, budget numbers";
    sortSel.replaceChildren(...columns[tab].filter((c) => !c.noSort).map((c) =>
      h("option", { value: c.key, selected: c.key === state.sort[tab].key, text: `Sort: ${c.label}` })));
  }

  const text = (r) => Object.values(r).filter((v) => typeof v === "string").join(" ").toLowerCase();

  function visibleRows() {
    const tab = state.tab;
    const cols = columns[tab];
    let rows = state.data[tab].rows;
    if (tab === "keys") { if (!state.only.keys) rows = rows.filter((r) => r.state !== "revoked"); }
    else if (state.only[tab]) rows = rows.filter((r) => (tab === "users" ? r.suspended : !r.budgetNumber));
    if (state.q) rows = rows.filter((r) => text(r).includes(state.q));
    const { key, dir } = state.sort[tab];
    const col = cols.find((c) => c.key === key) ?? cols[0];
    return [...rows].sort((a, b) => {
      const x = col.sort(a), y = col.sort(b);
      const c = typeof x === "number" && typeof y === "number" ? (x === y ? 0 : x < y ? -1 : 1) : collator.compare(String(x), String(y));
      return c * dir || collator.compare(String(cols[0].sort(a)), String(cols[0].sort(b)));
    });
  }

  function paintStats() {
    const { users, buckets, keys } = state.data;
    const stat = (label, value, warn) => h("div", { class: `stat ${warn ? "stat-warn" : ""}`.trim() },
      h("span", { class: "stat-v", text: value ?? "–" }), h("span", { class: "stat-l", text: label }));
    const u = users.status === "ready", b = buckets.status === "ready";
    const susp = u ? users.rows.filter((r) => r.suspended).length : null;
    const missing = b ? buckets.rows.filter((r) => !r.budgetNumber).length : null;
    statsEl.replaceChildren(
      stat("Users", u ? formatCount(users.rows.length) : null),
      stat("Suspended", u ? formatCount(susp) : null, susp > 0),
      stat("Buckets", b ? formatCount(buckets.rows.length) : null),
      stat("Without budget number", b ? formatCount(missing) : null, missing > 0),
      stat("Stored", b ? formatBytes(buckets.rows.reduce((s, r) => s + r.bytes, 0)) : null));
    document.getElementById("tc-users")?.replaceChildren(u ? formatCount(users.rows.length) : "");
    document.getElementById("tc-buckets")?.replaceChildren(b ? formatCount(buckets.rows.length) : "");
    const liveKeys = keys.status === "ready" ? keys.rows.filter((r) => r.state !== "revoked").length : null;
    document.getElementById("tc-keys")?.replaceChildren(liveKeys == null ? "" : formatCount(liveKeys));
  }

  function skeleton() {
    return h("div", { class: "tbl-skel", "aria-hidden": "true" }, Array.from({ length: 6 }, () =>
      h("div", { class: "skel-row" }, h("div", { class: "skel-dot" }),
        h("div", { class: "skel-stack" }, h("div", { class: "skel-line w60" }), h("div", { class: "skel-line w40" })),
        h("div", { class: "skel-pill" }))));
  }

  function emptyState(title, msg, actions = []) {
    return h("div", { class: "empty" }, icon("search", "icon empty-icon"), h("h3", { text: title }), h("p", { text: msg }), actions.length ? h("div", { class: "empty-acts" }, actions) : null);
  }

  function table(rows) {
    const tab = state.tab;
    const { key, dir } = state.sort[tab];
    const head = h("tr", {}, columns[tab].map((c) => {
      const sorted = c.key === key;
      const th = h("th", { scope: "col", class: c.num ? "num" : "", "aria-sort": c.noSort ? null : sorted ? (dir > 0 ? "ascending" : "descending") : "none" });
      if (c.noSort) th.append(h("span", { class: "th-plain", text: c.label }));
      else th.append(h("button", { class: "th-btn", type: "button", on: { click: () => {
        const s = state.sort[tab];
        if (s.key === c.key) s.dir *= -1; else { s.key = c.key; s.dir = 1; }
        sortSel.value = s.key;
        render();
      } } }, c.label, sorted ? icon(dir > 0 ? "up" : "down", "icon th-ic") : null));
      return th;
    }));
    const body = rows.map((r) => h("tr", { "data-suspended": String(!!r.suspended) },
      columns[tab].map((c) => h("td", { class: `${c.num ? "num" : ""} ${c.key === "actions" ? "td-acts" : ""}`.trim(), "data-label": c.key === "actions" ? null : c.label }, c.cell(r)))));
    return h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" },
      h("caption", { class: "visually-hidden", text: tab === "users" ? "RGW users" : tab === "keys" ? "Bucket keys" : "Buckets" }),
      h("thead", {}, head), h("tbody", {}, body)));
  }

  function render() {
    const tab = state.tab;
    const isSettings = tab === "settings";
    filtersEl.hidden = isSettings;
    exportNote.hidden = tab !== "buckets";
    for (const t of tabs) t.setAttribute("aria-selected", String(t.id === `tab-${tab}`)), t.tabIndex = t.id === `tab-${tab}` ? 0 : -1;
    paintStats();
    if (isSettings) {
      countEl.textContent = "";
      refreshBtn.disabled = false;
      refreshBtn.classList.remove("spin");
      if (tableHost.firstElementChild !== settings.el) tableHost.replaceChildren(settings.el);
      return;
    }
    const d = state.data[tab];
    exportEl.hidden = tab !== "buckets";
    onlyBtn.textContent = tab === "users" ? "Suspended only" : tab === "keys" ? "Show revoked" : "Missing budget number";
    onlyBtn.setAttribute("aria-pressed", String(state.only[tab]));
    onlyBtn.classList.toggle("on", state.only[tab]);
    const s = state.sort[tab];
    sortSel.value = s.key;
    dirBtn.replaceChildren(icon(s.dir > 0 ? "up" : "down"));
    dirBtn.setAttribute("aria-label", s.dir > 0 ? "Ascending. Click for descending." : "Descending. Click for ascending.");
    refreshBtn.disabled = d.status === "loading";
    refreshBtn.classList.toggle("spin", d.status === "loading");

    if (d.status === "loading" && !d.rows.length) {
      countEl.textContent = "Loading…";
      tableHost.replaceChildren(skeleton());
      return;
    }
    if (d.status === "error") {
      countEl.textContent = "";
      tableHost.replaceChildren(emptyState("Could not load this list", d.error,
        [h("button", { class: "btn btn-primary", type: "button", on: { click: () => load(tab) } }, icon("refresh"), "Try again")]));
      return;
    }
    const rows = visibleRows();
    const noun = tab === "users" ? "user" : tab === "keys" ? "key" : "bucket";
    const total = tab === "keys" && !state.only.keys ? d.rows.filter((r) => r.state !== "revoked").length : d.rows.length;
    countEl.textContent = rows.length === total ? `${formatCount(rows.length)} ${noun}${rows.length === 1 ? "" : "s"}` : `${formatCount(rows.length)} of ${formatCount(total)}`;
    if (!rows.length) {
      const filtered = state.q || (state.only[tab] && tab !== "keys");
      tableHost.replaceChildren(filtered
        ? emptyState("Nothing matches", "Try a different search or clear the filter.",
          [h("button", { class: "btn", type: "button", on: { click: () => { state.q = ""; search.value = ""; if (tab !== "keys") state.only[tab] = false; render(); } } }, "Clear filters")])
        : emptyState(`No ${noun}s yet`, `There are no ${noun}s on the cluster.`));
      return;
    }
    tableHost.replaceChildren(table(rows));
  }

  paintControls();
  settings.load();
  load("users");
  load("buckets");
  load("keys");

  return h("main", { class: "main admin", id: "main" },
    h("div", { class: "hero" },
      h("span", { class: "eyebrow", text: "Administration" }),
      h("h1", { text: "Users, buckets and settings" }),
      h("p", { text: "Every S3 user and bucket on the cluster. Set quotas, check budget numbers, suspend or restore access, and edit the portal's policies." })),
    statsEl,
    h("section", { class: "card admin-card", "aria-label": "Administration" },
      h("div", { class: "admin-bar" }, tablist, h("div", { class: "admin-tools" }, countEl, refreshBtn)),
      filtersEl,
      exportNote,
      h("div", { id: "admin-panel", role: "tabpanel", "aria-live": "polite" }, tableHost)));
}
