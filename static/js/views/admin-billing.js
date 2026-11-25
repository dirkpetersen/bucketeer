import { h } from "../dom.js";
import { icon } from "../icons.js";
import { api } from "../api.js";
import { toast } from "../components/toast.js";

// Admin "Billing" tab: product catalog (Tier-3-S3 price = the billing price), fiscal year
// start, the monthly chargeback .xlsx (download, send now, contacts and monthly email), and
// the monthly charges per user or per bucket for the last 12 months plus fiscal year to date.
// With cloud billing (CLOUD_BILLING_BUCKET), the sheet card also shows the AWS export's S3
// total for the picked month next to the sum of the sheet's cloud rows.
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

  // Rounded to cents first, so a tiny negative amount (or -0) never shows as "-$0.00".
  const cents = (v) => Math.round((Number(v) || 0) * 100) / 100 || 0;
  const money = (v) => {
    const n = cents(v);
    try {
      return new Intl.NumberFormat(undefined, { style: "currency", currency: data.settings.currency }).format(n);
    } catch {
      return `${n.toFixed(2)} ${data.settings.currency}`;
    }
  };
  const monthLabel = (m) => new Date(`${m}-15T00:00:00`).toLocaleDateString(undefined, { month: "short", year: "2-digit" });

  function productRows(s) {
    return s.products.map((p) => {
      const id = `bl-p-${p.id}`;
      const name = h("input", { class: "input mono-input", id: `${id}-name`, type: "text", maxlength: "40", value: p.name, autocomplete: "off", "aria-label": `Name of ${p.name}` });
      const price = h("input", { class: "input", id: `${id}-price`, type: "number", min: "0", step: "0.01", value: String(p.pricePerTiBMonth), "aria-label": `Price per TiB-month of ${p.name}` });
      const text = h("input", { class: "input", id: `${id}-text`, type: "text", maxlength: "200", value: p.advertised, "aria-label": `Advertised text of ${p.name}` });
      const row = h("div", { class: "bl-prod", role: "row" },
        h("div", { class: "bl-prod-cell", role: "cell", "data-label": "Product" }, name,
          h("small", { class: "set-hint", text: p.id === "tier3" ? "On premises, billed on the quota" : "Cloud, billed at the real AWS cost" })),
        h("div", { class: "bl-prod-cell", role: "cell", "data-label": `Price per TiB-month (${s.currency})` }, price),
        h("div", { class: "bl-prod-cell", role: "cell", "data-label": "Advertised text" }, text));
      return { el: row, value: () => ({ id: p.id, name: name.value.trim(), pricePerTiBMonth: Number(price.value), advertised: text.value.trim() }) };
    });
  }

  function settingsCard() {
    const s = data.settings;
    const cur = h("input", { class: "input mono-input bl-cur", id: "bl-cur", type: "text", maxlength: "3", value: s.currency, autocomplete: "off" });
    const fy = h("select", { class: "input", id: "bl-fy" }, FY.map(([v, t]) => h("option", { value: String(v), selected: v === s.fiscalYearStart, text: t })));
    const products = productRows(s);
    const save = h("button", { class: "btn btn-primary", type: "submit", text: "Save" });
    const form = h("form", { class: "bl-form", novalidate: true },
      h("div", { class: "field" }, h("label", { for: "bl-cur", text: "Currency" }), cur),
      h("div", { class: "field" }, h("label", { for: "bl-fy", text: "Fiscal year starts" }), fy),
      h("div", { class: "bl-prods", role: "table", "aria-label": "Products" },
        h("div", { class: "bl-prod bl-prod-head", role: "row" },
          ["Product", `Price per TiB-month (${s.currency})`, "Advertised text"].map((t) => h("span", { role: "columnheader", text: t }))),
        products.map((p) => p.el)),
      h("p", { class: "set-hint bl-wide", text: "The Tier-3-S3 price is the price used for the monthly charges below. Cloud products are billed at the real AWS cost; their price and text are what users see as an estimate." }),
      h("div", { class: "set-actions" }, save));
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      save.disabled = true;
      try {
        await api("/admin/billing/settings", { method: "PUT", json: {
          products: products.map((p) => p.value()), currency: cur.value.trim(), fiscalYearStart: Number(fy.value) } });
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

  const fmtWhen = (iso) => new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });

  const usd = (v) => {
    const n = cents(v);
    try {
      return new Intl.NumberFormat(undefined, { style: "currency", currency: "USD" }).format(n);
    } catch {
      return `${n.toFixed(2)} USD`;
    }
  };

  // The AWS side of the picked month: export total vs. the sheet's cloud rows.
  function cloudPanel() {
    const box = h("div", { class: "bl-cloud", "aria-live": "polite" });
    let seq = 0;
    async function show(m) {
      const mine = ++seq;
      box.replaceChildren(h("p", { class: "set-hint", text: `Reading the AWS billing export for ${m}…` }));
      let c;
      try {
        c = await api(`/admin/billing/cloud?month=${encodeURIComponent(m)}`);
      } catch (err) {
        if (err.status === 401 || mine !== seq) return;
        box.replaceChildren(h("p", { class: "set-err", text: err.message }));
        return;
      }
      if (mine !== seq) return;
      if (!c.available) {
        box.replaceChildren(h("div", { class: "set-warn", role: "note" }, icon("alert"),
          h("span", { text: `Cloud costs for ${m} are not available: ${c.reason || "the billing export could not be read"}.` })));
        return;
      }
      const stat = (label, value, extra) => h("div", { class: "bl-stat" },
        h("span", { class: "bl-stat-label", text: label }), h("b", { class: "bl-stat-value", text: value }),
        extra ? h("small", { class: "bl-stat-note", text: extra }) : null);
      const gap = cents(c.exportTotal - c.rowsTotal); // never "-$0.00"
      box.replaceChildren(
        h("div", { class: "bl-cloud-head" },
          h("b", { text: `Cloud (AWS), ${m}` }),
          h("span", { class: c.final ? "chip chip-ok" : "chip", text: c.final ? "Final" : "Not final yet" }),
          c.stale ? h("span", { class: "chip chip-warn", text: "Older figures" }) : null),
        h("div", { class: "bl-stats" },
          stat("Export total, our buckets", usd(c.exportTotal)),
          stat("Sheet cloud rows", usd(c.rowsTotal), `${c.rows} ${c.rows === 1 ? "row" : "rows"}`),
          stat("Not billed", usd(gap), gap ? "See the Problems sheet" : "Nothing left over"),
          stat("System and unassigned", usd((c.systemTotal || 0) + (c.unattributed || 0)), "Billing and backup buckets, S3 costs without a bucket")),
        h("p", { class: "set-hint", text: c.generatedAt
          ? `Export generated ${fmtWhen(c.generatedAt)}, read ${fmtWhen(c.fetchedAt)}. AWS finishes a month by the 5th of the next one.`
          : (c.reason || "No export for this month yet.") }));
    }
    return { box, show };
  }

  function sheetCard() {
    const s = data.settings;
    const rep = data.report;
    const month = h("input", { class: "input mono-input", id: "bl-month", type: "month", value: rep.lastMonth, max: data.months[data.months.length - 1], required: true, pattern: "[0-9]{4}-[0-9]{2}" });
    const download = h("a", { class: "btn", download: "" }, icon("download"), "Download .xlsx");
    const sync = () => { download.href = `/api/admin/billing/chargeback.xlsx?month=${encodeURIComponent(month.value)}`; };
    month.addEventListener("input", sync);
    sync();
    const cloud = rep.cloudBilling ? cloudPanel() : null;
    if (cloud) {
      let timer = 0;
      month.addEventListener("input", () => {
        clearTimeout(timer);
        timer = setTimeout(() => { if (month.checkValidity()) cloud.show(month.value); }, 300);
      });
      cloud.show(month.value);
    }
    const sendNow = h("button", { class: "btn", type: "button", disabled: !data.email.enabled || !s.contacts.length }, icon("bell"), "Send now");
    sendNow.addEventListener("click", async () => {
      sendNow.disabled = true;
      try {
        const r = await api("/admin/billing/report/send", { method: "POST", json: { month: month.value } });
        toast.show(r.failed.length ? `Sent to ${r.sent.join(", ")}; not to ${r.failed.join(", ")}.` : `Sheet for ${month.value} sent to ${r.sent.join(", ")}.`);
        await load();
      } catch (err) {
        if (err.status !== 401) toast.error(err.message);
        sendNow.disabled = false;
      }
    });

    const contacts = h("textarea", { class: "input mono-input bl-contacts", id: "bl-contacts", rows: "3", spellcheck: "false", "aria-describedby": "bl-contacts-hint" });
    contacts.value = s.contacts.join("\n");
    const day = h("select", { class: "input", id: "bl-day" }, Array.from({ length: 28 }, (_, i) => h("option", { value: String(i + 1), selected: i + 1 === s.sendDay, text: String(i + 1) })));
    const on = h("input", { type: "checkbox", id: "bl-on", checked: s.sendEnabled, disabled: !data.email.enabled && !s.sendEnabled });
    const save = h("button", { class: "btn btn-primary", type: "submit", text: "Save" });
    const form = h("form", { class: "bl-form", novalidate: true },
      h("div", { class: "field bl-wide" }, h("label", { for: "bl-contacts", text: "Billing contacts" }), contacts,
        h("small", { class: "set-hint", id: "bl-contacts-hint", text: "One email address per line, at most 10." })),
      h("div", { class: "field" }, h("label", { for: "bl-day", text: "Send on day of the month" }), day,
        rep.cloudBilling && s.sendDay < rep.sendDay ? h("small", { class: "set-hint", text: `Cloud billing is on, so the email waits until day ${rep.sendDay}, when AWS has finished the month.` }) : null),
      h("label", { class: "qd-check set-check bl-switch", for: "bl-on" }, on,
        h("span", {}, h("b", { text: "Email last month's sheet" }), h("small", { text: "On the send day at 06:00 server time" }))),
      h("div", { class: "set-actions" }, save));
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      save.disabled = true;
      try {
        await api("/admin/billing/settings", { method: "PUT", json: {
          contacts: contacts.value.split(/[\s,;]+/).filter(Boolean), sendDay: Number(day.value), sendEnabled: on.checked } });
        toast.show("Email settings saved.");
        await load();
      } catch (err) {
        if (err.status !== 401) toast.error(err.message);
      } finally {
        save.disabled = false;
      }
    });

    const last = rep.last;
    const status = h("div", { class: "bl-status" },
      rep.nextSend ? h("p", { class: "set-hint", text: `Next email: ${fmtWhen(rep.nextSend)}.` }) : null,
      rep.lastSentMonth ? h("p", { class: "set-hint", text: `Last scheduled sheet sent: ${rep.lastSentMonth}.` }) : null,
      last ? h("p", { class: last.ok ? "set-ok" : "set-err", text: last.ok
        ? `${last.trigger === "manual" ? "Sent by hand" : "Sent"} ${fmtWhen(last.at)}: ${last.month} to ${last.sent.join(", ")}${last.failed.length ? ` (failed: ${last.failed.join(", ")})` : ""}.`
        : `Sending ${last.month} failed ${fmtWhen(last.at)}. The server log has the mail server's answer.` }) : null);

    return h("section", { class: "card set-card", "aria-labelledby": "bl-x" },
      h("div", { class: "set-head" }, h("h2", { id: "bl-x", text: "Chargeback sheet" }),
        h("span", { class: "chip", text: s.sendEnabled ? `Emailed on day ${rep.sendDay || s.sendDay}` : "Email off" })),
      h("p", { class: "set-lead", text: "One row per bucket and month for finance. On-premises buckets are billed on their quota: the month's daily average in TiB, rounded up. A bucket without a quota is billed on its measured size. Buckets without a budget number, or owners without an email address, are listed on the Problems sheet." }),
      rep.cloudBilling ? h("p", { class: "set-hint", text: "Cloud buckets are billed at their real AWS cost from the billing export, in cents (Quantity 1234 = $12.34). Cloud buckets Bucketeer can't place, system costs and S3 costs without a bucket are listed on the Problems sheet with their amounts." }) : null,
      h("div", { class: "bl-month" },
        h("div", { class: "field" }, h("label", { for: "bl-month", text: "Month" }), month),
        h("div", { class: "bl-tools" }, download, sendNow)),
      cloud ? cloud.box : null,
      data.email.enabled ? null : h("div", { class: "set-warn", role: "note" }, icon("alert"),
        h("span", { text: "Email is off (SMTP_FROM is empty in the server settings), so the sheet can only be downloaded." })),
      form, status);
  }

  function tableCard() {
    const months = [...data.months].reverse(); // newest first: the current month needs no scrolling
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
    host.replaceChildren(settingsCard(), sheetCard(), tableCard());
  }

  return { el: host, load };
}
