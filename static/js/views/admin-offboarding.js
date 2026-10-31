import { h } from "../dom.js";
import { icon } from "../icons.js";
import { getOffboarding, runOffboarding, saveOffboardingSettings } from "../api.js";
import { toast } from "../components/toast.js";
import { confirmDialog } from "../components/confirm-dialog.js";

// The "Offboarding" tab of the administration page: the daily job that suspends
// the S3 accounts (and bucket keys) of people who left AD. It starts in report mode
// (nobody is suspended; the list shows who would be). Switching to enforce is explicit.
// Names in results come from AD and the cluster: shown through h()/text only.

const REASONS = {
  disabled: "Account disabled in AD",
  not_in_group: "Left the required AD group",
  not_found: "Not found in AD",
};
const ACTIONS = {
  would_suspend: ["chip-warn", "Would suspend"],
  not_suspended: ["", "Not suspended"],
  suspended: ["chip-warn", "Suspended"],
  partial: ["chip-warn", "Suspended, keys incomplete"],
  failed: ["chip-warn", "Failed"],
  skipped: ["", "Skipped"],
};
const OUTCOMES = {
  ok: ["chip-ok", "Completed"],
  aborted: ["chip-warn", "Aborted"],
  failed: ["chip-warn", "Failed"],
  running: ["", "Running"],
};

const when = (iso) => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? String(iso ?? "") : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
};

const chip = (text, kind = "") =>
  h("span", { class: `chip ${kind}`.trim() }, kind ? h("span", { class: "dot" }) : null, text);

function runTitle(run) {
  const who = run.trigger === "schedule" ? "Scheduled" : `Started by ${run.actor}`;
  return `${when(run.startedAt)} · ${who}`;
}

function people(run) {
  if (!run.results.length) {
    return h("p", { class: "set-empty", text: run.outcome === "ok"
      ? "Nobody left AD: no account would be suspended."
      : "No people were listed for this run." });
  }
  return h("ul", { class: "off-people" }, run.results.map((r) => {
    const [kind, label] = ACTIONS[r.action] ?? ["", r.action];
    return h("li", { class: "off-person" },
      h("div", { class: "off-who" },
        h("b", { text: r.adUsernames.join(", ") }),
        h("code", { text: r.uid })),
      h("div", { class: "off-why" },
        h("span", { text: REASONS[r.reason] ?? r.reason }),
        r.keys?.length ? h("span", { class: "sub", text: `${r.keys.length} bucket key${r.keys.length === 1 ? "" : "s"} suspended` }) : null,
        r.note ? h("span", { class: "sub", text: r.note }) : null),
      chip(label, kind));
  }));
}

function limitsText(th) {
  const n = th.minPeopleForPercent;
  return `more than ${th.maxPercent}% of known people and at least ${n} ${n === 1 ? "person" : "people"}, or more than ${th.maxPeople} people`;
}

function limitsLine(run) {
  return run.thresholds
    ? h("p", { class: "set-hint off-limits-used" }, h("b", { text: "Abort limits in force: " }), limitsText(run.thresholds))
    : null;
}

const LIMIT_FIELDS = [
  { key: "maxPercent", label: "Largest share of people", unit: "%", min: 1, max: 100,
    help: "Abort when more than this share of known people would be suspended, provided the minimum below is also reached. 100 turns the share check off." },
  { key: "minPeopleForPercent", label: "Minimum people for that share", unit: "people", min: 1, max: 1000,
    help: "The share only counts from this many people on, so one or two genuine leavers on a small installation go through." },
  { key: "maxPeople", label: "Largest number of people", unit: "people", min: 1, max: 100000,
    help: "Abort when more than this many people would be suspended, whatever the share." },
];

function countsLine(run) {
  const c = run.counts ?? {};
  const bits = [];
  if (c.known != null) bits.push(`${c.known} known ${c.known === 1 ? "person" : "people"}`);
  if (c.checked != null) bits.push(`${c.checked} checked in AD`);
  if (c.alreadySuspended) bits.push(`${c.alreadySuspended} already suspended`);
  if (c.candidates != null) bits.push(`${c.candidates} ${c.candidates === 1 ? "candidate" : "candidates"}`);
  return bits.join(" · ");
}

export function renderOffboarding() {
  let data = null;
  let poll = 0;
  const host = h("div", { class: "settings off", "aria-label": "Offboarding" });

  const stop = () => { clearTimeout(poll); poll = 0; };

  async function load() {
    stop();
    if (!data) host.replaceChildren(h("div", { class: "set-loading", "aria-hidden": "true" }, [1, 2].map(() => h("div", { class: "skel-field set-skel" }))));
    try {
      data = await getOffboarding();
      paint();
      // A scheduled run may be in progress: look again until it ends.
      if (data.running && host.isConnected) poll = setTimeout(load, 3000);
    } catch (err) {
      if (err.status === 401) return;
      data = null;
      host.replaceChildren(h("div", { class: "empty" }, icon("alert", "icon empty-icon"),
        h("h3", { text: "Could not load the offboarding job" }), h("p", { text: err.message }),
        h("div", { class: "empty-acts" }, h("button", { class: "btn btn-primary", type: "button", on: { click: load } }, icon("refresh"), "Try again"))));
    }
  }

  async function setMode(mode) {
    if (mode === "enforce") {
      const ok = await confirmDialog({
        title: "Switch to enforce mode?",
        message: "From the next run on, people who left AD will really be suspended, together with their bucket keys. Nothing is ever deleted, and a suspended person is restored automatically at their next login. Check the report-mode results first.",
        confirmLabel: "Enforce",
        danger: true,
      });
      if (!ok) return;
    }
    try {
      const res = await saveOffboardingSettings({ mode });
      data = { ...data, settings: res };
      toast.show(mode === "enforce" ? "Offboarding will suspend people who left AD." : "Offboarding is report-only again.");
      load();
    } catch (err) {
      if (err.status !== 401) toast.error(err.message);
    }
  }

  async function saveTime(input, button) {
    button.disabled = true;
    try {
      const res = await saveOffboardingSettings({ time: input.value });
      data = { ...data, settings: res };
      toast.show(`The job now runs daily at ${res.time}.`);
      load();
    } catch (err) {
      if (err.status !== 401) toast.error(err.message);
      button.disabled = false;
    }
  }

  async function runNow(button) {
    button.disabled = true;
    button.replaceChildren(h("span", { class: "spinner", "aria-hidden": "true" }), "Running…");
    try {
      const run = await runOffboarding();
      if (run.outcome === "aborted") toast.error("The run was aborted; nobody was suspended.");
      else if (run.outcome === "failed") toast.error("The run failed. See the details below.");
      else toast.show(run.mode === "enforce" ? "The run finished." : "The report run finished. Nobody was suspended.");
    } catch (err) {
      if (err.status !== 401) toast.error(err.message);
    }
    load();
  }

  function controls() {
    const { settings } = data;
    const enforce = settings.mode === "enforce";
    const time = h("input", { class: "input off-time", type: "time", value: settings.time, id: "off-time", required: true, step: "60" });
    const save = h("button", { class: "btn", type: "button", text: "Save time", disabled: true });
    time.addEventListener("input", () => { save.disabled = !time.value || time.value === settings.time; });
    save.addEventListener("click", () => saveTime(time, save));
    const run = h("button", { class: "btn btn-primary", type: "button", disabled: data.running }, icon("play"), "Run now");
    run.addEventListener("click", () => runNow(run));
    if (data.running) run.replaceChildren(h("span", { class: "spinner", "aria-hidden": "true" }), "Running…");
    const next = new Date(data.nextRun);

    return h("section", { class: "card set-card", "aria-labelledby": "off-h" },
      h("div", { class: "set-head" }, h("h2", { id: "off-h", text: "Offboarding" }),
        chip(enforce ? "Enforcing" : "Report only", enforce ? "chip-warn" : "chip-ok")),
      h("p", { class: "set-lead", text: `Once a day Bucketeer checks every person it knows (${data.knownPeople}) against AD. Accounts that are disabled, gone, or no longer in the S3 group are suspended with their bucket keys. Nothing is ever deleted, and a person is restored automatically at their next login. If too many people would be suspended at once (see the limits below), or AD cannot be read, the run stops and suspends nobody.` }),
      h("div", { class: "off-grid" },
        h("div", { class: "off-panel" },
          h("h3", { text: "Mode" }),
          h("p", { class: "set-hint", text: enforce
            ? "Enforce: people who left AD are suspended in the next run."
            : "Report only: runs list who would be suspended, and change nothing." }),
          enforce
            ? h("button", { class: "btn", type: "button", on: { click: () => setMode("report") } }, "Back to report only")
            : h("button", { class: "btn btn-danger", type: "button", on: { click: () => setMode("enforce") } }, icon("shield"), "Switch to enforce…")),
        h("div", { class: "off-panel" },
          h("h3", { text: "Schedule" }),
          h("div", { class: "field" },
            h("label", { for: "off-time", text: "Daily at (server time)" }),
            h("div", { class: "off-inline" }, time, save)),
          h("p", { class: "set-hint" }, "Next run: ",
            h("b", { text: Number.isNaN(next.getTime()) ? "?" : next.toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) }))),
        h("div", { class: "off-panel" },
          h("h3", { text: "Run now" }),
          h("p", { class: "set-hint", text: enforce ? "Runs the job immediately, with the real effect." : "Runs the check immediately, in report mode." }),
          run)));
  }

  async function saveLimits(form, inputs, button, errBox) {
    errBox.hidden = true;
    for (const input of Object.values(inputs)) input.removeAttribute("aria-invalid");
    const body = {};
    const bad = [];
    for (const f of LIMIT_FIELDS) {
      const raw = inputs[f.key].value.trim();
      const n = Number(raw);
      if (!/^\d+$/.test(raw) || n < f.min || n > f.max) {
        bad.push({ field: f.key, message: `Enter a whole number from ${f.min} to ${f.max}.` });
      } else if (n !== data.settings[f.key]) {
        body[f.key] = n;
      }
    }
    if (bad.length) return showLimitErrors(bad, inputs, errBox);
    for (const f of LIMIT_FIELDS) inputs[f.key].value = String(Number(inputs[f.key].value.trim()));
    if (!Object.keys(body).length) { button.disabled = true; return; }  // nothing changed
    button.disabled = true;
    try {
      const res = await saveOffboardingSettings(body);
      data = { ...data, settings: res };
      toast.show("Abort limits saved.");
      load();
    } catch (err) {
      if (err.status === 401) return;
      if (err.status === 422 && err.body?.errors) showLimitErrors(err.body.errors, inputs, errBox);
      else toast.error(err.message);
      button.disabled = false;
    }
  }

  function showLimitErrors(errors, inputs, errBox) {
    const lines = [];
    for (const e of errors) {
      const label = LIMIT_FIELDS.find((f) => f.key === e.field)?.label ?? e.field;
      inputs[e.field]?.setAttribute("aria-invalid", "true");
      lines.push(h("li", { text: `${label}: ${e.message}` }));
    }
    errBox.replaceChildren(h("ul", {}, lines));
    errBox.hidden = false;
    const first = errors.map((e) => inputs[e.field]).find(Boolean);
    first?.focus();
  }

  function limits() {
    const { settings } = data;
    const inputs = {};
    const save = h("button", { class: "btn", type: "submit", text: "Save limits", disabled: true });
    const errBox = h("div", { class: "set-err off-limit-err", role: "alert", id: "off-limit-err", hidden: true });
    const dirty = () => {
      save.disabled = LIMIT_FIELDS.every((f) => inputs[f.key].value.trim() === String(settings[f.key]));
    };
    const fields = LIMIT_FIELDS.map((f) => {
      const id = `off-${f.key}`;
      const input = h("input", { class: "input off-num", type: "number", inputmode: "numeric", id,
        min: String(f.min), max: String(f.max), step: "1", required: true, value: String(settings[f.key]),
        "aria-describedby": `${id}-help` });
      input.addEventListener("input", dirty);
      inputs[f.key] = input;
      return h("div", { class: "field off-limit" },
        h("label", { for: id, text: f.label }),
        h("div", { class: "off-inline off-unit" }, input, h("span", { class: "off-unit-t", text: f.unit })),
        h("p", { class: "set-hint", id: `${id}-help`, text: f.help }));
    });
    const form = h("form", { class: "off-limits", novalidate: true }, fields, errBox,
      h("div", { class: "off-limits-act" }, save));
    form.addEventListener("submit", (ev) => { ev.preventDefault(); saveLimits(form, inputs, save, errBox); });
    return h("section", { class: "card set-card", "aria-labelledby": "off-lim-h" },
      h("div", { class: "set-head" }, h("h2", { id: "off-lim-h", text: "Abort limits" })),
      h("p", { class: "set-lead", text: "A safety net against a misreading of AD. A run stops and suspends nobody when more than the share below would be suspended and that is at least the minimum number of people, or when more than the largest number would be suspended. An AD error always stops the run." }),
      h("p", { class: "set-hint", text: `Today: ${limitsText(settings)}.` }),
      form);
  }

  function latest(run) {
    const [kind, label] = OUTCOMES[run.outcome] ?? ["", run.outcome];
    return h("section", { class: "card set-card", "aria-labelledby": "off-last" },
      h("div", { class: "set-head" }, h("h2", { id: "off-last", text: "Latest run" }), chip(label, kind)),
      h("p", { class: "set-lead" }, runTitle(run), " · ", run.mode === "enforce" ? "enforce mode" : "report mode",
        run.finishedAt && new Date(run.finishedAt) - new Date(run.startedAt) >= 1000 ? ` · took ${Math.round((new Date(run.finishedAt) - new Date(run.startedAt)) / 1000)} s` : ""),
      run.reason ? h("div", { class: run.outcome === "ok" ? "set-warn" : "off-alert", role: "alert" }, icon("alert"), h("span", { text: run.reason })) : null,
      h("p", { class: "set-hint", text: countsLine(run) }),
      limitsLine(run),
      h("h3", { class: "off-sub", text: run.mode === "enforce" && run.outcome !== "aborted" ? "People suspended" : run.outcome === "aborted" ? "Who the run found (not suspended)" : "Who would be suspended" }),
      people(run));
  }

  function older(runs) {
    if (!runs.length) return null;
    return h("section", { class: "card set-card", "aria-labelledby": "off-old" },
      h("details", { class: "off-older" },
        h("summary", {}, h("h2", { id: "off-old", text: `Earlier runs (${runs.length})` }), icon("down", "icon off-chev")),
        h("ul", { class: "off-runs" }, runs.map((run) => {
          const [kind, label] = OUTCOMES[run.outcome] ?? ["", run.outcome];
          return h("li", {},
            h("details", { class: "off-run" },
              h("summary", {}, h("span", { class: "off-run-t", text: runTitle(run) }),
                h("span", { class: "off-run-m", text: `${run.mode === "enforce" ? "Enforce" : "Report"} · ${run.results.length} listed` }),
                chip(label, kind)),
              h("div", { class: "off-run-body" },
                run.reason ? h("p", { class: run.outcome === "ok" ? "set-hint" : "off-alert" }, run.reason) : null,
                h("p", { class: "set-hint", text: countsLine(run) }),
                limitsLine(run),
                people(run))));
        }))));
  }

  function paint() {
    const [first, ...rest] = data.runs;
    host.replaceChildren(
      controls(),
      limits(),
      first ? latest(first) : h("section", { class: "card set-card" }, h("h2", { class: "off-none", text: "No run yet" }),
        h("p", { class: "set-lead", text: "The first scheduled run is shown here. Press Run now to see who a report would list." })),
      older(rest));
  }

  return { el: host, load };
}
