// Administration > Settings: the "User notices" card. It loads on its own
// (GET /api/admin/settings/notices). Once a day Bucketeer emails the owner of an on-premises
// bucket that is nearly full, and people whose access keys are old; the server validates
// every value (422 with per-field errors), this card only gives quick feedback.
import { append, h } from "../dom.js";
import { icon } from "../icons.js";
import { getNoticeSettings, saveNoticeSettings } from "../api.js";
import { toast } from "../components/toast.js";

let seq = 0;
const nid = (p) => `ntc-${p}${++seq}`;

const fmtWhen = (value) => {
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
};

function numberField(label, hint, [min, max]) {
  const input = h("input", {
    class: "input mono-input", type: "number", id: nid("n"), min: String(min), max: String(max), step: "1",
    inputMode: "numeric",
  });
  const error = h("p", { class: "set-err", role: "alert", id: nid("e"), hidden: true });
  const el = h("div", { class: "field" }, h("label", { for: input.id, text: label }), input,
    h("p", { class: "set-hint", text: hint }), error);
  return {
    el, input,
    error(msg) {
      error.textContent = msg || "";
      error.hidden = !msg;
      input.setAttribute("aria-invalid", String(!!msg));
      if (msg) input.setAttribute("aria-describedby", error.id); else input.removeAttribute("aria-describedby");
    },
    read() {
      const v = input.value.trim();
      const n = /^\d+$/.test(v) ? Number(v) : NaN;
      if (!Number.isInteger(n) || n < min || n > max) throw new Error(`Enter a whole number from ${min} to ${max}.`);
      return n;
    },
  };
}

function lastRunText(run) {
  if (!run) return "No check has run yet.";
  const c = run.counts || {};
  const parts = [
    `${c.nearFull ?? 0} nearly full ${c.nearFull === 1 ? "bucket" : "buckets"}`,
    `${c.oldKeys ?? 0} old ${c.oldKeys === 1 ? "key" : "keys"}`,
    `${(c.bucketNotices ?? 0) + (c.keyNotices ?? 0)} new ${(c.bucketNotices ?? 0) + (c.keyNotices ?? 0) === 1 ? "notice" : "notices"}`,
  ];
  return `Last check ${fmtWhen(run.at)}: ${parts.join(", ")}.`;
}

export function noticesCard() {
  const hid = nid("h");
  const status = h("span", { class: "chip" });
  const box = h("div", { class: "set-test-out", "aria-live": "polite" });
  const save = h("button", { class: "btn btn-primary", type: "submit", text: "Save", disabled: true });
  const cancel = h("button", { class: "btn", type: "button", text: "Cancel", disabled: true });
  let data = null;
  let pct = null;
  let age = null;
  const grid = h("div", { class: "set-grid" });
  const form = h("form", { class: "set-form", novalidate: true }, grid, box, h("div", { class: "set-actions" }, cancel, save));
  const el = h("section", { class: "card set-card", "aria-labelledby": hid, "data-section": "notices" },
    h("div", { class: "set-head" }, h("h2", { id: hid, text: "User notices" }), status),
    h("p", { class: "set-lead", text: "Once a day Bucketeer emails the owner of a bucket on premises that is nearly full, before uploads stop at its quota, and reminds people of access keys that are old enough to replace." }),
    form);

  const dirty = () => !!data && (pct.input.value.trim() !== String(data.nearFullPercent) || age.input.value.trim() !== String(data.maxKeyAgeDays));
  const update = () => { save.disabled = cancel.disabled = !dirty(); };

  const paint = () => {
    status.replaceChildren(`${data.nearFullPercent}% full · ${data.maxKeyAgeDays ? `keys after ${data.maxKeyAgeDays} days` : "key reminders off"}`);
    pct.input.value = String(data.nearFullPercent);
    age.input.value = String(data.maxKeyAgeDays);
    pct.error(""); age.error("");
    const s = data.schedule || {};
    // append() skips the parts left out (null); replaceChildren would print them.
    box.replaceChildren();
    append(box, [
      !data.email?.enabled
        ? h("div", { class: "set-warn", role: "status" }, icon("alert"),
          h("span", { text: "Email is off (SMTP_FROM is empty). The daily check still runs and writes the audit log, but nobody is emailed." }))
        : null,
      h("p", { class: "set-hint", text: lastRunText(s.lastRun) }),
      s.lastRun?.errors?.length ? h("p", { class: "set-hint", text: "The last check could not finish everything (the storage service or AWS did not answer); it is tried again an hour later." }) : null,
      h("p", { class: "set-hint", text: `Runs every day at ${s.time || "07:00"} server time. Next check: ${fmtWhen(s.nextRun) || s.nextRun || "soon"}.` }),
    ]);
    update();
  };

  const build = () => {
    pct = numberField("Near-full warning at (% of the bucket quota)",
      `The owner gets an email when a bucket on premises holds this much of its quota, at most once a week per bucket; the bucket list shows "Nearly full" from the same point. Uploads stop at 100%. Default ${data.defaults.nearFullPercent}%.`,
      data.limits.nearFullPercent);
    age = numberField("Key age reminder after (days)",
      `People get one email naming each own key, subuser key or cloud key older than this, at most once a month per key, and key lists show "Rotate soon". 0 switches the reminders off. Keys made before Bucketeer kept track count from the day it first saw them. Default ${data.defaults.maxKeyAgeDays} days.`,
      data.limits.maxKeyAgeDays);
    for (const f of [pct, age]) f.input.addEventListener("input", () => { f.error(""); update(); });
    grid.replaceChildren(pct.el, age.el);
    paint();
  };

  async function load() {
    box.replaceChildren(h("p", { class: "set-hint", role: "status" }, h("span", { class: "spinner", "aria-hidden": "true" }), "Loading…"));
    try {
      data = await getNoticeSettings();
      build();
    } catch (err) {
      if (err.status === 401) return;
      box.replaceChildren(h("p", { class: "set-err", role: "alert", text: err.message || "Could not load the notice settings." }),
        h("div", { class: "set-actions" }, h("button", { class: "btn", type: "button", on: { click: load } }, icon("refresh"), "Try again")));
    }
  }

  cancel.addEventListener("click", () => { if (data) paint(); });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (save.disabled || !data) return;
    const body = {};
    for (const [key, f] of [["nearFullPercent", pct], ["maxKeyAgeDays", age]]) {
      try { body[key] = f.read(); } catch (err) { f.error(err.message); return; }
    }
    save.disabled = cancel.disabled = true;
    save.replaceChildren(h("span", { class: "spinner", "aria-hidden": "true" }), "Saving…");
    try {
      data = await saveNoticeSettings(body);
      toast.show("Notice settings saved.");
      paint();
    } catch (err) {
      if (err.status === 401) return;
      const errors = err.body?.errors || [];
      for (const x of errors) (x.field === "maxKeyAgeDays" ? age : pct).error(x.message);
      if (!errors.length) toast.error(err.message || "Could not save the notice settings.");
    } finally {
      save.textContent = "Save";
      update();
    }
  });

  load();
  return el;
}
