import { h } from "../dom.js";
import { icon } from "../icons.js";
import { api } from "../api.js";
import { formatBytes } from "../format.js";
import { toast } from "../components/toast.js";

// Admin "Backup" tab: the nightly archive of the database and .env in an object-locked
// bucket (BACKUP_BUCKET in .env). Shows the bucket's state, sets the time and retention,
// starts a backup by hand and lists the last 30 runs. Names, keys and errors come from the
// server and the cluster: shown through h()/text only.

const OUTCOMES = { ok: ["chip-ok", "Completed"], failed: ["chip-warn", "Failed"], running: ["", "Running"] };

const when = (iso) => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? String(iso ?? "") : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
};

const chip = (text, kind = "") =>
  h("span", { class: `chip ${kind}`.trim() }, kind ? h("span", { class: "dot" }) : null, text);

const outcome = (run) => {
  const [kind, label] = OUTCOMES[run.outcome] ?? ["", run.outcome];
  return chip(label, kind);
};

function fact(label, ...value) {
  return h("div", { class: "bu-fact" }, h("dt", { text: label }), h("dd", {}, ...value));
}

export function renderBackup() {
  let data = null;
  const host = h("div", { class: "settings", "aria-label": "Backup" });

  async function load() {
    if (!data) host.replaceChildren(h("div", { class: "set-loading", "aria-hidden": "true" }, [1, 2].map(() => h("div", { class: "skel-field set-skel" }))));
    try {
      data = await api("/admin/backup");
      paint();
    } catch (err) {
      if (err.status === 401) return;
      host.replaceChildren(h("div", { class: "set-unavail" }, h("p", { text: err.message }),
        h("button", { class: "btn", type: "button", on: { click: load } }, icon("refresh"), "Try again")));
    }
  }

  function offCard() {
    return h("section", { class: "card set-card", "aria-labelledby": "bu-h" },
      h("div", { class: "set-head" }, h("h2", { id: "bu-h", text: "Backup" }), chip("Off", "chip-warn")),
      h("p", { class: "set-lead", text: "Backups are off because BACKUP_BUCKET is not set. Add this line to the server's .env (with appmotel: appmo env bucketeer), then restart Bucketeer. The name is new and random so nobody else has claimed it; Bucketeer creates the bucket on the first run." }),
      h("pre", { class: "bu-env" }, h("code", { text: `BACKUP_BUCKET=${data.suggestedBucket}` })),
      h("p", { class: "set-hint", text: "The suggestion changes at every restart. Any unused name with lowercase letters, digits and hyphens works." }));
  }

  function bucketFacts() {
    const st = data.bucketState ?? {};
    const lockText = st.exists
      ? (st.objectLock ? `${st.mode ?? "No default"} · ${st.retentionDays ?? "?"} days` : "Not enabled")
      : "Created on the first run";
    const last = data.lastRun;
    const cloud = data.destination === "cloud";
    return h("dl", { class: "bu-facts" },
      fact("Bucket", data.bucket ? h("code", { text: data.bucket }) : "Named and created on the first run"),
      fact("Location", cloud ? `Cloud (AWS, ${data.region ?? "?"})` : "On premises"),
      fact(cloud ? "Writer" : "Owner", h("code", { text: data.user }),
        cloud ? h("span", { class: "set-hint", text: " IAM user, a new access key for each run, deleted after it" }) : null),
      fact("Object lock", st.exists && st.objectLock && st.mode === "COMPLIANCE" ? chip(lockText, "chip-ok") : lockText),
      fact("Versioning", st.exists ? ({ enabled: "On", suspended: "Paused", off: "Off" }[st.versioning] ?? st.versioning ?? "Unknown") : "Created on the first run"),
      fact("Last backup", last
        ? h("span", { class: "bu-last" }, outcome(last), h("span", { text: `${when(last.startedAt)}${last.bytes != null ? ` · ${formatBytes(last.bytes)}` : ""}` }))
        : "None yet"),
      fact("Next run", data.nextRun ? when(data.nextRun) : "Not scheduled"),
      cloud ? fact("On premises", h("code", { text: data.onpremBucket ?? "" }), " (earlier backups stay there)")
        : (data.awsBucket ? fact("In AWS", h("code", { text: data.awsBucket }), " (earlier backups stay there)") : null));
  }

  function settingsCard() {
    const s = data.settings;
    const st = data.bucketState ?? {};
    const time = h("input", { class: "input bu-time", id: "bu-time", type: "time", step: "60", required: true, value: s.time });
    const days = h("input", { class: "input bu-days", id: "bu-days", type: "number", min: String(s.retentionDays), max: "36500", step: "1", value: String(s.retentionDays) });
    const save = h("button", { class: "btn", type: "submit", text: "Save" });
    const now = h("button", { class: "btn btn-primary", type: "button", disabled: data.running }, data.running ? h("span", { class: "spinner", "aria-hidden": "true" }) : icon("lock"), data.running ? "Backing up…" : "Back up now");
    const form = h("form", { class: "bl-form", novalidate: true },
      h("div", { class: "field" }, h("label", { for: "bu-time", text: "Backup time (server time)" }), time),
      h("div", { class: "field" }, h("label", { for: "bu-days", text: "Retention in days" }), days),
      h("p", { class: "set-hint bu-span", text: "Retention can only be lengthened. Each backup is locked (COMPLIANCE) for the period in force when it was written and is deleted the day after it ends; a longer period applies to backups written from now on, older ones keep their own lock." }),
      h("div", { class: "set-actions" }, save, now));
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      save.disabled = true;
      try {
        const r = await api("/admin/backup/settings", { method: "PUT", json: { time: time.value, retentionDays: Number(days.value) } });
        if (r.warning) toast.error(r.warning); else toast.show("Backup settings saved.");
        await load();
      } catch (err) {
        if (err.status !== 401) toast.error(err.message);
      } finally {
        save.disabled = false;
      }
    });
    now.addEventListener("click", async () => {
      now.disabled = true;
      now.replaceChildren(h("span", { class: "spinner", "aria-hidden": "true" }), "Backing up…");
      try {
        const run = await api("/admin/backup/run", { method: "POST" });
        if (run.outcome === "ok") toast.show(`Backup written (${formatBytes(run.bytes)}).`);
        else toast.error(`Backup failed: ${run.error}`);
      } catch (err) {
        if (err.status !== 401) toast.error(err.message);
      }
      await load();
    });
    return h("section", { class: "card set-card", "aria-labelledby": "bu-h" },
      h("div", { class: "set-head" }, h("h2", { id: "bu-h", text: "Backup" }), chip("On", "chip-ok")),
      h("p", { class: "set-lead", text: "Every night Bucketeer writes its database and its .env file to a bucket only its own backup account can read. Object lock keeps each backup from being deleted or changed before its retention ends, by anyone using S3, Bucketeer included. The archive is not encrypted: keep the bucket private." }),
      st.error ? h("div", { class: "off-alert", role: "alert" }, icon("alert"), h("span", { text: st.error })) : null,
      bucketFacts(), form);
  }

  function runsCard() {
    const runs = data.runs;
    return h("section", { class: "card set-card", "aria-labelledby": "bu-r" },
      h("div", { class: "set-head" }, h("h2", { id: "bu-r", text: "Last 30 backups" })),
      runs.length
        ? h("div", { class: "tbl-wrap" }, h("table", { class: "tbl bu-tbl" },
          h("caption", { class: "visually-hidden", text: "Backup runs, newest first" }),
          h("thead", {}, h("tr", {}, ["Started", "By", "Where", "Outcome", "Size", "Archive"].map((t, i) =>
            h("th", { scope: "col", class: i === 4 ? "num" : "", text: t })))),
          h("tbody", {}, runs.map((r) => h("tr", {},
            h("td", { "data-label": "Started", text: when(r.startedAt) }),
            h("td", { "data-label": "By", text: r.trigger === "schedule" ? "Schedule" : r.actor }),
            h("td", { "data-label": "Where", text: r.destination === "cloud" ? "AWS" : "On premises" }),
            h("td", { "data-label": "Outcome" }, outcome(r)),
            h("td", { class: "num", "data-label": "Size", text: r.bytes != null ? formatBytes(r.bytes) : "—" }),
            h("td", { "data-label": "Archive" }, r.error
              ? h("span", { class: "bu-err", text: r.error })
              : h("code", { class: "bu-key", text: r.key ?? "" })))))))
        : h("p", { class: "set-empty", text: "No backups yet. The first one runs at the backup time, or now with Back up now." }),
      h("p", { class: "set-hint" }, "To restore, stop Bucketeer and run ",
        h("code", { class: "bu-key", text: "python3 app.py --restore-backup latest" }),
        " (or an archive name instead of latest) in its folder; add ", h("code", { class: "bu-key", text: "--from cloud" }),
        " or ", h("code", { class: "bu-key", text: "--from onprem" }),
        " to choose the place (default: where the newest backup went). See the admin guide."));
  }

  function paint() {
    host.replaceChildren(...(data.configured ? [settingsCard(), runsCard()] : [offCard()]));
  }

  return { el: host, load };
}
