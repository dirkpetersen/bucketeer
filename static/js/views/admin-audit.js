import { h } from "../dom.js";
import { icon } from "../icons.js";
import { adminAudit, adminAuditFacets, auditExportUrl } from "../api.js";
import { formatCount } from "../format.js";

// The "Audit" tab of the administration page: a read-only, filterable view of the
// append-only audit log, newest first, paged by an id cursor ("Load more").
// `details` is attacker-influenced (bucket names, labels, budget numbers): it is
// only ever shown through h()/textContent, never parsed as markup.

const PAGE = 50;

// Flatten nested details to [["a.b", "value"], ...]; non-strings as JSON.
export function flattenDetails(value, prefix = "", out = []) {
  if (value && typeof value === "object") {
    const entries = Array.isArray(value) ? value.map((v, i) => [String(i), v]) : Object.entries(value);
    if (!entries.length && prefix) out.push([prefix, Array.isArray(value) ? "[]" : "{}"]);
    for (const [k, v] of entries) flattenDetails(v, prefix ? `${prefix}.${k}` : k, out);
  } else {
    out.push([prefix || "value", typeof value === "string" ? value : JSON.stringify(value)]);
  }
  return out;
}

const when = (iso) => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "medium" });
};

// A local calendar day as an ISO instant: start of that day, or of the next day.
function dayBound(value, plusDays = 0) {
  if (!value) return "";
  const [y, m, d] = value.split("-").map(Number);
  return new Date(y, m - 1, d + plusDays).toISOString();
}

export function renderAudit() {
  const st = { status: "idle", entries: [], next: null, more: false, error: "", seq: 0, facets: false };

  const input = (attrs) => h("input", { class: "input", autocomplete: "off", spellcheck: "false", ...attrs });
  const field = (label, control, cls = "") => h("label", { class: `af ${cls}`.trim() }, h("span", { class: "af-l", text: label }), control);
  const actor = input({ type: "text", placeholder: "e.g. alice", maxlength: "200" });
  const action = h("select", { class: "input" }, h("option", { value: "", text: "Any action" }));
  const prefix = input({ type: "text", placeholder: "e.g. bucket.", maxlength: "199" });
  const ttype = h("select", { class: "input" }, h("option", { value: "", text: "Any type" }));
  const target = input({ type: "text", placeholder: "Name contains", maxlength: "200" });
  const from = input({ type: "date" });
  const to = input({ type: "date" });
  const clear = h("button", { class: "btn", type: "button", text: "Clear filters" });
  const exportEl = h("a", { class: "btn", href: "#", download: "", title: "Download the filtered log (up to 50,000 rows) as CSV. The export is recorded in the audit log." }, icon("download"), "Export CSV");
  const more = h("button", { class: "btn", type: "button", text: "Load more" });
  const countEl = h("span", { class: "count", "aria-live": "polite" });

  const bar = h("form", { class: "audit-filters", role: "search", "aria-label": "Filter the audit log", on: { submit: (e) => e.preventDefault() } },
    field("Actor", actor), field("Action", action), field("Action starts with", prefix),
    field("Target type", ttype), field("Target", target), field("From", from), field("To", to),
    h("div", { class: "af-acts" }, clear, exportEl));
  const body = h("div", { class: "audit-body", "aria-live": "polite" });
  const host = h("div", { class: "audit" }, bar, h("div", { class: "audit-count" }, countEl), body);

  function params(extra = {}) {
    const p = prefix.value.trim();
    return {
      actor: actor.value.trim(),
      action: p ? (p.endsWith(".") ? p : `${p}.`) : action.value,
      target_type: ttype.value,
      target_contains: target.value.trim(),
      since: dayBound(from.value),
      until: dayBound(to.value, 1),
      ...extra,
    };
  }
  const isFiltered = () => Object.values(params()).some(Boolean);
  const range = () => from.value && to.value && from.value > to.value;

  async function fetchPage(append) {
    const my = ++st.seq;
    st.status = "loading";
    if (!append) { st.entries = []; st.next = null; }
    paint();
    try {
      const res = await adminAudit(params({ limit: PAGE, before_id: append ? st.next : "" }));
      if (my !== st.seq) return;
      st.entries = append ? st.entries.concat(res.entries) : res.entries;
      st.next = res.nextCursor;
      st.status = "ready";
    } catch (err) {
      if (my !== st.seq || err.status === 401) return;
      st.status = "error";
      st.error = err.message;
    }
    paint();
  }

  async function loadFacets() {
    try {
      const f = await adminAuditFacets();
      const keep = [action.value, ttype.value];
      action.replaceChildren(h("option", { value: "", text: "Any action" }), ...f.actions.map((a) => h("option", { value: a, text: a })));
      ttype.replaceChildren(h("option", { value: "", text: "Any type" }), ...f.targetTypes.map((t) => h("option", { value: t, text: t })));
      action.value = keep[0];
      ttype.value = keep[1];
      st.facets = true;
    } catch { /* the filters still work typed in */ }
  }

  function load() {
    loadFacets();
    return fetchPage(false);
  }

  function detailList(details) {
    const rows = flattenDetails(details);
    if (!details || !Object.keys(details).length) return h("p", { class: "muted", text: "No details." });
    return h("dl", { class: "kv" }, rows.map(([k, v]) => h("div", {}, h("dt", { text: k }), h("dd", { text: v === "" ? "(empty)" : v }))));
  }

  function entryRow(e) {
    const panel = h("div", { class: "audit-detail", hidden: true }, detailList(e.details));
    const id = `ad-${e.id}`;
    panel.id = id;
    const toggle = h("button", { class: "btn btn-sm", type: "button", "aria-expanded": "false", "aria-controls": id,
      "aria-label": `Details of ${e.action} on ${e.target}`, on: { click: () => {
        const open = panel.hidden;
        panel.hidden = !open;
        toggle.setAttribute("aria-expanded", String(open));
        toggle.replaceChildren(icon(open ? "up" : "down"), h("span", { text: open ? "Hide" : "Details" }));
      } } }, icon("down"), h("span", { text: "Details" }));
    return h("tbody", { class: "audit-entry" },
      h("tr", {},
        h("td", { "data-label": "Time" }, h("time", { datetime: e.ts, title: e.ts, text: when(e.ts) })),
        h("td", { "data-label": "Actor" }, h("code", { text: e.actor })),
        h("td", { "data-label": "Action" }, h("span", { class: "chip audit-action", text: e.action })),
        h("td", { "data-label": "Target" }, h("div", { class: "who" }, h("b", { class: "mono", text: e.target }), h("span", { class: "sub", text: e.targetType }))),
        h("td", { class: "td-acts" }, toggle)),
      h("tr", { class: "audit-detail-row" }, h("td", { colspan: "5" }, panel)));
  }

  function paint() {
    exportEl.href = auditExportUrl(params());
    const bad = range();
    to.setAttribute("aria-invalid", String(!!bad));
    clear.disabled = !isFiltered();
    more.disabled = st.status === "loading";
    more.textContent = st.status === "loading" ? "Loading…" : "Load more";
    countEl.textContent = st.status === "ready" ? `${formatCount(st.entries.length)} entr${st.entries.length === 1 ? "y" : "ies"}${st.next ? " shown" : ""}` : "";
    if (bad) {
      body.replaceChildren(h("div", { class: "empty" }, icon("alert", "icon empty-icon"), h("h3", { text: "Check the dates" }), h("p", { text: "“From” is after “To”." })));
      return;
    }
    if (st.status === "loading" && !st.entries.length) {
      body.replaceChildren(h("div", { class: "tbl-skel", "aria-hidden": "true" }, Array.from({ length: 6 }, () =>
        h("div", { class: "skel-row" }, h("div", { class: "skel-dot" }),
          h("div", { class: "skel-stack" }, h("div", { class: "skel-line w60" }), h("div", { class: "skel-line w40" })),
          h("div", { class: "skel-pill" })))));
      return;
    }
    if (st.status === "error" && !st.entries.length) {
      body.replaceChildren(h("div", { class: "empty" }, icon("alert", "icon empty-icon"), h("h3", { text: "Could not load the audit log" }), h("p", { text: st.error }),
        h("div", { class: "empty-acts" }, h("button", { class: "btn btn-primary", type: "button", on: { click: () => fetchPage(false) } }, icon("refresh"), "Try again"))));
      return;
    }
    if (!st.entries.length) {
      body.replaceChildren(h("div", { class: "empty" }, icon("search", "icon empty-icon"),
        h("h3", { text: isFiltered() ? "Nothing matches" : "The audit log is empty" }),
        h("p", { text: isFiltered() ? "Try different filters or a wider date range." : "Actions such as logins, bucket changes and settings edits appear here." }),
        isFiltered() ? h("div", { class: "empty-acts" }, h("button", { class: "btn", type: "button", on: { click: clearAll } }, "Clear filters")) : null));
      return;
    }
    const head = h("tr", {}, ["Time", "Actor", "Action", "Target", ""].map((t) => h("th", { scope: "col" }, t || h("span", { class: "visually-hidden", text: "Details" }))));
    body.replaceChildren(...[
      h("div", { class: "tbl-wrap" }, h("table", { class: "tbl audit-tbl" },
        h("caption", { class: "visually-hidden", text: "Audit log, newest first" }),
        h("thead", {}, head), st.entries.map(entryRow))),
      st.status === "error" ? h("p", { class: "qd-error", role: "alert", text: st.error }) : null,
      st.next ? h("div", { class: "audit-more" }, more) : h("p", { class: "audit-end", text: "End of the log." }),
    ].filter(Boolean));
  }

  let timer;
  const refilter = (delay = 0) => {
    clearTimeout(timer);
    paint();
    if (range()) return;
    timer = setTimeout(() => fetchPage(false), delay);
  };
  for (const el of [actor, prefix, target]) el.addEventListener("input", () => refilter(300));
  for (const el of [action, ttype, from, to]) el.addEventListener("change", () => refilter());
  prefix.addEventListener("input", () => { action.disabled = !!prefix.value.trim(); });
  function clearAll() {
    for (const el of [actor, prefix, target, from, to]) el.value = "";
    action.value = ""; ttype.value = ""; action.disabled = false;
    refilter();
  }
  clear.addEventListener("click", clearAll);
  more.addEventListener("click", () => st.next && fetchPage(true));

  paint();
  return { el: host, load, busy: () => st.status === "loading" };
}
