import { h } from "../dom.js";
import { icon } from "../icons.js";
import { api } from "../api.js";
import { toast } from "../components/toast.js";

// Admin "Billing" tab: price per TiB-month, fiscal year start, and the monthly
// charges per user or per bucket for the last 12 months plus fiscal year to date.
// The server computes every figure; this view only shows them.

const FY = [[1, "January 1"], [4, "April 1"], [7, "July 1"], [10, "October 1"]];

export function renderBilling() {
  let data = null;
  let view = "users";
  const host = h("div", { class: "settings", "aria-label": "Billing" });

  async function load() {
    if (!data) host.replaceChildren(h("div", { class: "set-loading", "aria-hidden": "true" }, [1, 2].map(() => h("div", { class: "skel-field set-skel" }))));
    try {
      data = await api("/admin/billing");
      paint();
    } catch (err) {
      if (err.status === 401) return;
      host.replaceChildren(h("div", { class: "set-unavail" }, h("p", { text: err.message }),
        h("button", { class: "btn", type: "button", on: { click: load } }, icon("refresh"), "Try again")));
    }
  }

  const money = (v) => {
    try {
      return new Intl.NumberFormat(undefined, { style: "currency", currency: data.settings.currency }).format(v);
    } catch {
      return `${v.toFixed(2)} ${data.settings.currency}`;
    }
  };
  const monthLabel = (m) => new Date(`${m}-15T00:00:00`).toLocaleDateString(undefined, { month: "short", year: "2-digit" });

  function settingsCard() {
    const s = data.settings;
    const price = h("input", { class: "input", id: "bl-price", type: "number", min: "0", step: "0.01", value: String(s.pricePerTiBMonth) });
    const cur = h("input", { class: "input mono-input bl-cur", id: "bl-cur", type: "text", maxlength: "3", value: s.currency, autocomplete: "off" });
    const fy = h("select", { class: "input", id: "bl-fy" }, FY.map(([v, t]) => h("option", { value: String(v), selected: v === s.fiscalYearStart, text: t })));
    const save = h("button", { class: "btn btn-primary", type: "submit", text: "Save" });
    const form = h("form", { class: "bl-form", novalidate: true },
      h("div", { class: "field" }, h("label", { for: "bl-price", text: "Price per TiB per month" }), price),
      h("div", { class: "field" }, h("label", { for: "bl-cur", text: "Currency" }), cur),
      h("div", { class: "field" }, h("label", { for: "bl-fy", text: "Fiscal year starts" }), fy),
      h("div", { class: "set-actions" }, save));
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      save.disabled = true;
      try {
        await api("/admin/billing/settings", { method: "PUT", json: {
          pricePerTiBMonth: Number(price.value), currency: cur.value.trim(), fiscalYearStart: Number(fy.value) } });
        toast.show("Billing settings saved.");
        await load();
      } catch (err) {
        if (err.status !== 401) toast.error(err.message);
      } finally {
        save.disabled = false;
      }
    });
    return h("section", { class: "card set-card", "aria-labelledby": "bl-h" },
      h("div", { class: "set-head" }, h("h2", { id: "bl-h", text: "Billing" }),
        h("span", { class: "chip", text: data.fiscalYear.label })),
      h("p", { class: "set-lead", text: "Bucketeer records the size of every bucket each hour (the last reading of a day counts). A month's charge is the average stored TiB over the days of that month times the price. Days before recording started, or when the cluster could not be read, are not charged. The current price applies to all months shown." }),
      form);
  }

  function tableCard() {
    const months = data.months;
    const rows = view === "users" ? data.users : data.buckets;
    const lead = view === "users"
      ? [["User", (r) => h("code", { text: r.owner })], ["Buckets", (r) => String(r.buckets), true]]
      : [["Bucket", (r) => h("div", { class: "who" }, h("b", { text: r.bucket }), h("code", { text: r.owner }))],
         ["Budget number", (r) => r.budgetNumber ? h("code", { text: r.budgetNumber }) : h("span", { class: "muted", text: "Missing" })]];
    const head = h("tr", {}, lead.map(([t, , num]) => h("th", { class: num ? "num" : "", scope: "col", text: t })),
      months.map((m) => h("th", { class: "num", scope: "col", text: monthLabel(m) })),
      h("th", { class: "num bl-fytd", scope: "col", text: `${data.fiscalYear.label} to date` }));
    const cell = (label, v, extra = "") => h("td", { class: `num ${extra}`.trim(), "data-label": label, text: money(v) });
    const body = rows.map((r) => h("tr", {},
      lead.map(([t, f, num]) => h("td", { class: num ? "num" : "", "data-label": t }, f(r))),
      months.map((m) => cell(monthLabel(m), r.months[m])),
      cell(`${data.fiscalYear.label} to date`, r.fytd, "bl-fytd")));
    const foot = h("tfoot", {},
      h("tr", { class: "bl-total" }, h("th", { scope: "row", colspan: String(lead.length), text: "Total" }),
        months.map((m) => cell(monthLabel(m), data.totals.months[m])), cell(`${data.fiscalYear.label} to date`, data.totals.fytd, "bl-fytd")),
      h("tr", { class: "bl-cov" }, h("th", { scope: "row", colspan: String(lead.length), text: "Days measured" }),
        months.map((m) => h("td", { class: "num", "data-label": monthLabel(m), text: `${data.coverage[m]}/${data.daysInMonth[m]}` })), h("td", {})));
    const seg = (key, text) => h("button", { class: `btn btn-filter ${view === key ? "on" : ""}`, type: "button", "aria-pressed": String(view === key),
      on: { click: () => { view = key; paint(); } } }, text);
    return h("section", { class: "card set-card", "aria-labelledby": "bl-t" },
      h("div", { class: "set-head" }, h("h2", { id: "bl-t", text: "Monthly charges" }),
        h("div", { class: "bl-tools" }, seg("users", "Per user"), seg("buckets", "Per bucket"),
          h("a", { class: "btn", href: `/api/admin/billing/export.csv?view=${view}`, download: "" }, icon("download"), "Export CSV"))),
      data.firstDay ? null : h("p", { class: "set-hint", text: "No readings yet. The first one is taken within an hour of startup." }),
      rows.length
        ? h("div", { class: "tbl-wrap" }, h("table", { class: "tbl bl-tbl" },
          h("caption", { class: "visually-hidden", text: view === "users" ? "Charges per user" : "Charges per bucket" }),
          h("thead", {}, head), h("tbody", {}, body), foot))
        : h("p", { class: "muted", text: "Nothing to charge in the last 12 months." }));
  }

  function paint() {
    host.replaceChildren(settingsCard(), tableCard());
  }

  return { el: host, load };
}
