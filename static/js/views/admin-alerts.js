// Administration > Settings: the "Alert emails" card. Who is emailed when something needs an
// administrator: a failed backup or restore test, no successful backup for 48 hours, an
// offboarding run that was aborted or failed (GET/PUT /api/admin/alerts). It loads on its
// own, like the location card. Addresses and subjects come from the server: text only.
import { h } from "../dom.js";
import { icon } from "../icons.js";
import { api } from "../api.js";
import { toast } from "../components/toast.js";

let seq = 0;
const nid = (p) => `al-${p}${++seq}`;

const when = (iso) => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? String(iso ?? "") : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
};

const NOT_SENT = { email_off: "not emailed: email is off", no_contacts: "not emailed: no contacts", send_failed: "no mail server took it" };

function lastLine(last) {
  if (!last) return h("p", { class: "set-hint", text: "No alert so far." });
  const ok = last.emailed;
  const to = ok ? `sent to ${last.sent.join(", ")}${last.failed?.length ? ` (failed: ${last.failed.join(", ")})` : ""}` : NOT_SENT[last.reason] ?? "not emailed";
  return h("p", { class: ok ? "set-hint" : "set-err" }, `Last alert ${when(last.at)}: `, h("b", { text: last.subject }), ` (${to}).`);
}

export function alertsCard() {
  const hid = nid("h");
  const status = h("span", { class: "chip", text: "…" });
  const area = h("textarea", { class: "input mono-input al-contacts", id: nid("t"), rows: "3", spellcheck: "false", placeholder: "ops-team@example.org", disabled: true });
  const hint = h("small", { class: "set-hint", id: nid("d"), text: "One email address per line, at most 10. Leave empty to only write alerts to the server log." });
  area.setAttribute("aria-describedby", hint.id);
  const kinds = h("ul", { class: "al-kinds" });
  const notes = h("div", { class: "al-notes" });
  const cancel = h("button", { class: "btn", type: "button", text: "Cancel", disabled: true });
  const save = h("button", { class: "btn btn-primary", type: "submit", text: "Save", disabled: true });
  const form = h("form", { class: "set-form", novalidate: true },
    h("div", { class: "field" }, h("label", { for: area.id, text: "Alert contacts" }), area, hint),
    notes,
    h("div", { class: "set-actions" }, cancel, save));
  const el = h("section", { class: "card set-card", "aria-labelledby": hid, "data-section": "alerts" },
    h("div", { class: "set-head" }, h("h2", { id: hid, text: "Alert emails" }), status),
    h("p", { class: "set-lead", text: "Who hears about trouble that needs an administrator. Bucketeer emails these addresses when:" }),
    kinds, form);

  let data = null;
  const typed = () => area.value.split(/[\s,;]+/).filter(Boolean);
  const dirty = () => data && typed().join("\n") !== data.contacts.join("\n");
  const sync = () => { save.disabled = cancel.disabled = !dirty(); };

  function paint() {
    const n = data.contacts.length;
    status.textContent = !data.email.enabled ? "Email off" : n ? `${n} contact${n === 1 ? "" : "s"}` : "No contacts";
    status.className = data.email.enabled && n ? "chip chip-ok" : "chip";
    area.value = data.contacts.join("\n");
    area.disabled = false;
    kinds.replaceChildren(...data.kinds.map((k) => h("li", { text: k.text })));
    notes.replaceChildren(
      data.email.enabled ? null : h("div", { class: "set-warn", role: "note" }, icon("alert"),
        h("span", { text: "Email is off (SMTP_FROM is empty in the server settings), so alerts only go to the server log." })),
      lastLine(data.last));
    sync();
  }

  async function load() {
    try {
      data = await api("/admin/alerts");
      paint();
    } catch (err) {
      if (err.status === 401) return;
      notes.replaceChildren(h("p", { class: "set-err", role: "alert", text: err.message }));
    }
  }

  area.addEventListener("input", sync);
  cancel.addEventListener("click", () => { if (data) paint(); });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (save.disabled) return;
    save.disabled = cancel.disabled = true;
    save.replaceChildren(h("span", { class: "spinner", "aria-hidden": "true" }), "Saving…");
    try {
      data = await api("/admin/alerts", { method: "PUT", json: { contacts: typed() } });
      save.textContent = "Save";
      paint();
      toast.show("Alert contacts saved.");
    } catch (err) {
      save.textContent = "Save";
      sync(); // keep what was typed so it can be corrected
      if (err.status !== 401) toast.error(err.message);
    }
  });

  load();
  return el;
}
