import { h } from "../dom.js";
import { icon } from "../icons.js";
import {
  clearDefaultQuota, deleteEndpoint, deleteUidMapping, getSettings, saveDefaultQuota, saveEndpoint,
  saveBudgetNumber, saveNaming, savePlacement, saveUidMapping, testBudgetNumber, testNaming,
} from "../api.js";
import { UNITS, quotaText } from "../format.js";
import { toast } from "../components/toast.js";
import { confirmDialog } from "../components/confirm-dialog.js";
import { SIZE_UNITS, splitSize } from "../components/quota-dialog.js";

// The "Settings" tab of the administration page: runtime policy edited here
// instead of in .env. Every section is a card with its own form, inline
// validation (server messages shown next to the field), Save and Cancel.
// Endpoint addresses are shown to admins only and carry no credentials.

let seq = 0;
const nid = (p) => `${p}${++seq}`;

// --- small building blocks -----------------------------------------------------

function field(label, input, { hint, id } = {}) {
  const error = h("p", { class: "set-err", role: "alert", hidden: true });
  const el = h("div", { class: "field" },
    h("label", { for: id ?? input.id, text: label }), input,
    hint && h("p", { class: "set-hint", text: hint }), error);
  return {
    el,
    error(msg) {
      error.textContent = msg || "";
      error.hidden = !msg;
      input.setAttribute("aria-invalid", String(!!msg));
      if (msg) input.setAttribute("aria-describedby", error.id ||= nid("e"));
      else input.removeAttribute("aria-describedby");
    },
  };
}

function textInput(attrs = {}) {
  return h("input", { class: "input", type: "text", autocomplete: "off", spellcheck: "false", id: nid("i"), ...attrs });
}

function check(label, input, sub) {
  input.id ||= nid("c");
  return h("label", { class: "qd-check set-check", for: input.id }, input,
    h("span", {}, h("b", { text: label }), sub && h("small", { text: sub })));
}

function chip(text, kind = "") {
  return h("span", { class: `chip ${kind}`.trim() }, kind ? h("span", { class: "dot" }) : null, text);
}

function card(title, status, lead, ...body) {
  const hid = nid("h");
  return h("section", { class: "card set-card", "aria-labelledby": hid },
    h("div", { class: "set-head" }, h("h2", { id: hid, text: title }), status),
    h("p", { class: "set-lead", text: lead }),
    ...body);
}

// Save / Cancel row. `dirty()` is re-evaluated by the caller through update().
function footer(label = "Save") {
  const cancel = h("button", { class: "btn", type: "button", text: "Cancel", disabled: true });
  const save = h("button", { class: "btn btn-primary", type: "submit", text: label, disabled: true });
  return {
    el: h("div", { class: "set-actions" }, cancel, save),
    cancel,
    update(dirty) { cancel.disabled = save.disabled = !dirty; },
    async busy(fn) {
      save.disabled = cancel.disabled = true;
      save.replaceChildren(h("span", { class: "spinner", "aria-hidden": "true" }), "Saving…");
      try { return await fn(); } finally { save.textContent = label; }
    },
  };
}

function wireForm(form, ft, run) {
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (ft.el.querySelector("[type=submit]").disabled) return;
    await ft.busy(run);
  });
}

function emptyRow(text) {
  return h("p", { class: "set-empty", text });
}

// --- sections --------------------------------------------------------------------

function namingSection(data, apply) {
  const saved = data.naming;
  const prefix = textInput({ value: saved.prefix, placeholder: "{username}-", "aria-label": "Required prefix" });
  const pattern = textInput({ value: saved.pattern, placeholder: "e.g. [a-z0-9-]+-(data|scratch)", maxlength: String(data.limits.maxPatternLength), class: "input mono-input" });
  const fPrefix = field("Required prefix", prefix, { hint: "Every new bucket name must start with this. {username} stands for the user's name in lowercase. Leave empty for no prefix." });
  const fPattern = field("Extra pattern (regular expression)", pattern, { hint: "The whole name must match it. Leave empty for none. Slow or invalid patterns are refused." });
  const tName = textInput({ placeholder: "alice-data", "aria-label": "Bucket name to try" });
  const tUser = textInput({ placeholder: "alice", "aria-label": "Username to try", value: "alice" });
  const result = h("div", { class: "set-test-out", "aria-live": "polite" });
  const ft = footer();

  const draft = () => ({ prefix: prefix.value, pattern: pattern.value });
  const dirty = () => prefix.value.trim() !== saved.prefix || pattern.value.trim() !== saved.pattern;
  let timer;
  let ticket = 0;
  async function runTest() {
    const n = ++ticket;
    if (!tName.value) { result.replaceChildren(h("p", { class: "set-hint", text: "Type a bucket name to see which rules it breaks." })); return; }
    try {
      const r = await testNaming({ rules: draft(), name: tName.value, username: tUser.value || "user" });
      if (n !== ticket) return;
      fPattern.error(""); fPrefix.error("");
      if (r.rulesError) {
        (r.rulesError.toLowerCase().includes("pattern") ? fPattern : fPrefix).error(r.rulesError);
        result.replaceChildren();
        return;
      }
      result.replaceChildren(r.errors.length
        ? h("ul", { class: "set-problems" }, r.errors.map((m) => h("li", { text: m })))
        : h("p", { class: "set-ok" }, icon("check"), "This name is allowed."),
      r.prefix ? h("p", { class: "set-hint" }, "Prefix for this user: ", h("code", { text: r.prefix })) : null);
    } catch (err) {
      if (n === ticket && err.status !== 401) result.replaceChildren(h("p", { class: "set-err", text: err.message }));
    }
  }
  const changed = () => { ft.update(dirty()); clearTimeout(timer); timer = setTimeout(runTest, 300); };
  for (const el of [prefix, pattern, tName, tUser]) el.addEventListener("input", changed);

  const form = h("form", { novalidate: true, class: "set-form" },
    h("div", { class: "set-grid" }, fPrefix.el, fPattern.el),
    h("div", { class: "set-test" },
      h("h3", { text: "Try a name" }),
      h("div", { class: "set-grid" }, field("Bucket name", tName).el, field("As user", tUser).el),
      result),
    ft.el);
  wireForm(form, ft, async () => {
    try {
      const res = await saveNaming(draft());
      toast.show("Bucket naming rules saved.");
      apply({ naming: res });
    } catch (err) {
      (err.message.toLowerCase().includes("pattern") ? fPattern : fPrefix).error(err.message);
      ft.update(true);
    }
  });
  ft.cancel.addEventListener("click", () => {
    prefix.value = saved.prefix; pattern.value = saved.pattern; fPattern.error(""); fPrefix.error("");
    ft.update(false); runTest();
  });
  runTest();

  return card("Bucket naming", saved.prefix || saved.pattern ? chip("Custom rules", "chip-ok") : chip("Standard rules"),
    "S3 and RGW naming is always enforced: 3 to 63 lowercase letters, digits, hyphens and dots, starting and ending with a letter or digit, no IP-address form, no xn-- prefix or -s3alias suffix. You can add rules on top.",
    form);
}

function budgetSection(data, apply) {
  const saved = data.budgetNumber;
  const pattern = textInput({ value: saved.pattern, placeholder: "e.g. [0-9]{6}|WT-[A-Z0-9]+", maxlength: String(data.limits.maxPatternLength), class: "input mono-input" });
  const example = textInput({ value: saved.example, placeholder: "e.g. 123456", maxlength: "64", class: "input mono-input" });
  const fPattern = field("Format (regular expression)", pattern, { hint: "The whole budget number must match it. Leave empty to accept any number made of letters, digits, spaces and + - = . _ : / @. Slow or invalid patterns are refused." });
  const fExample = field("Example shown to users", example, { hint: "Shown as a hint in the profile and when creating a bucket. It must itself match the format." });
  const tValue = textInput({ placeholder: "123456", "aria-label": "Budget number to try", class: "input mono-input" });
  const result = h("div", { class: "set-test-out", "aria-live": "polite" });
  const ft = footer();

  const draft = () => ({ pattern: pattern.value, example: example.value });
  const dirty = () => pattern.value.trim() !== saved.pattern || example.value.trim() !== saved.example;
  const route = (msg) => (msg.toLowerCase().includes("example") ? fExample : fPattern);
  let timer;
  let ticket = 0;
  async function runTest() {
    const n = ++ticket;
    if (!tValue.value.trim()) { result.replaceChildren(h("p", { class: "set-hint", text: "Type a budget number to see whether it passes." })); return; }
    try {
      const r = await testBudgetNumber({ rules: draft(), value: tValue.value });
      if (n !== ticket) return;
      fPattern.error(""); fExample.error("");
      if (r.rulesError) { route(r.rulesError).error(r.rulesError); result.replaceChildren(); return; }
      result.replaceChildren(r.ok
        ? h("p", { class: "set-ok" }, icon("check"), "This number is allowed.")
        : h("ul", { class: "set-problems" }, h("li", { text: r.error })));
    } catch (err) {
      if (n === ticket && err.status !== 401) result.replaceChildren(h("p", { class: "set-err", text: err.message }));
    }
  }
  const changed = () => { ft.update(dirty()); clearTimeout(timer); timer = setTimeout(runTest, 300); };
  for (const el of [pattern, example, tValue]) el.addEventListener("input", changed);

  const form = h("form", { novalidate: true, class: "set-form" },
    h("div", { class: "set-grid" }, fPattern.el, fExample.el),
    h("div", { class: "set-test" },
      h("h3", { text: "Try a number" }),
      field("Budget number", tValue).el,
      result),
    ft.el);
  wireForm(form, ft, async () => {
    try {
      const res = await saveBudgetNumber(draft());
      toast.show("Budget number format saved.");
      apply({ budgetNumber: res });
    } catch (err) {
      route(err.message).error(err.message);
      ft.update(true);
    }
  });
  ft.cancel.addEventListener("click", () => {
    pattern.value = saved.pattern; example.value = saved.example; fPattern.error(""); fExample.error("");
    ft.update(false); runTest();
  });
  runTest();

  return card("Budget numbers", saved.pattern ? chip("Custom format", "chip-ok") : chip("Any valid number"),
    "Users give every new bucket a budget number for chargeback (an index or a worktag). Formats are specific to each site, so describe yours here; there is no lookup against a finance system. Changing the format does not alter numbers that buckets already have, and the Buckets table flags the ones that no longer match.",
    form);
}

function placementSection(data, apply, reload) {
  const p = data.placement;
  if (!p.available) {
    return card("Placement", chip("Unavailable", "chip-warn"),
      "Which storage placement new buckets use, and whether users may choose one.",
      h("div", { class: "set-unavail" }, h("p", { text: p.error || "The placements could not be read from the cluster." }),
        h("button", { class: "btn", type: "button", on: { click: reload } }, icon("refresh"), "Try again")));
  }
  const names = p.available.map((a) => a.name);
  const def = h("select", { class: "input", id: nid("s") },
    h("option", { value: "", text: `Cluster default (${p.croitDefault ?? "none set"})`, selected: !p.default }),
    p.available.map((a) => h("option", { value: a.name, text: a.name, selected: a.name === p.default })));
  const boxes = p.available.map((a) => ({ name: a.name, input: h("input", { type: "checkbox", checked: p.allowed.includes(a.name) }), a }));
  const fDef = field("Default for new buckets", def, { hint: "Used when a bucket is created without a choice." });
  const list = h("fieldset", { class: "set-fieldset" },
    h("legend", { text: "Placements users may choose" }),
    boxes.map((b) => check(b.name, b.input, `Storage classes: ${b.a.storageClasses.join(", ")}${b.a.isDefault ? " · cluster default" : ""}`)));
  const note = h("p", { class: "set-hint" });
  const ft = footer();
  const sel = () => boxes.filter((b) => b.input.checked).map((b) => b.name);
  const dirty = () => (def.value || null) !== (p.default ?? null) || sel().join() !== p.allowed.join();
  const paintNote = () => {
    const n = new Set([...sel(), ...(def.value ? [def.value] : [])]).size;
    note.textContent = names.length < 2
      ? "This cluster has only one placement, so users never get a choice."
      : sel().length === 0 ? "Nothing ticked: every new bucket uses the default and users get no choice."
        : n > 1 ? "Users will see a placement choice (under Advanced) when they create a bucket."
          : "Only one placement is offered, so users still get no choice.";
  };
  const changed = () => { ft.update(dirty()); paintNote(); };
  def.addEventListener("change", changed);
  for (const b of boxes) b.input.addEventListener("change", changed);
  paintNote();
  const stale = p.stale.length
    ? h("p", { class: "set-warn", role: "status" }, icon("alert"), `No longer on the cluster: ${p.stale.join(", ")}. Save to drop ${p.stale.length > 1 ? "them" : "it"}.`)
    : null;
  const form = h("form", { novalidate: true, class: "set-form" }, stale, fDef.el, list, note, ft.el);
  wireForm(form, ft, async () => {
    try {
      const res = await savePlacement({ default: def.value || null, allowed: sel() });
      toast.show("Placement settings saved.");
      apply({ placement: res });
    } catch (err) {
      fDef.error(err.message);
      ft.update(true);
    }
  });
  ft.cancel.addEventListener("click", () => {
    def.value = p.default ?? "";
    for (const b of boxes) b.input.checked = p.allowed.includes(b.name);
    fDef.error(""); changed(); ft.update(false);
  });
  if (stale) ft.update(true);
  return card("Placement", chip(p.default ?? `Default: ${p.croitDefault ?? "none"}`),
    "Placements decide which pools store a bucket's data. By default every bucket lands on the cluster's own default and users get no choice.",
    form);
}

function quotaSection(data, apply) {
  const q = data.defaultQuota;
  const mode0 = !q.isSet ? "unset" : q.quota.enabled ? "limit" : "none";
  const mode = h("select", { class: "input", id: nid("s") },
    [["unset", `Default (${quotaText(q.default)})`], ["none", "No limit (unlimited)"], ["limit", "Limit new users to…"]].map(([v, t]) => h("option", { value: v, text: t, selected: v === mode0 })));
  const [sizeText, sizeUnit] = splitSize(q.quota?.maxSize);
  const size = h("input", { class: "input", type: "number", id: nid("n"), min: "0", step: "any", inputMode: "decimal", value: sizeText, placeholder: "No size limit" });
  const unit = h("select", { class: "input", id: nid("u"), "aria-label": "Size unit" }, SIZE_UNITS.map(([u]) => h("option", { value: u, text: u, selected: u === sizeUnit })));
  const objs = h("input", { class: "input", type: "number", id: nid("n"), min: "0", step: "1", inputMode: "numeric", value: q.quota?.maxObjects ?? "", placeholder: "No object limit" });
  const fMode = field("Quota for new users", mode);
  const fSize = field("Size limit", h("div", { class: "qd-pair" }, size, unit), { id: size.id, hint: "Leave empty for no size limit. 0 is a real limit: no data at all." });
  const fObjs = field("Object limit", objs, { hint: "Leave empty for no object limit." });
  const limits = h("div", { class: "set-grid" }, fSize.el, fObjs.el);
  const warn = h("p", { class: "set-hint", role: "status" }, `Built-in default: new users get ${quotaText(q.default)}. Choose "No limit" to give them no quota, or set your own limit.`);
  const ft = footer();
  const read = () => {
    if (mode.value === "none") return { enabled: false };
    const sv = size.value.trim(), ov = objs.value.trim();
    const bytes = sv === "" ? null : Math.round(Number(sv) * UNITS.find(([u]) => u === unit.value)[1]);
    const count = ov === "" ? null : Number(ov);
    if (bytes != null && (!Number.isFinite(bytes) || bytes < 0 || !Number.isSafeInteger(bytes))) throw new Error("Enter a valid size, or leave it empty.");
    if (count != null && (!Number.isInteger(count) || count < 0)) throw new Error("Enter a whole number of objects, or leave it empty.");
    if (bytes == null && count == null) throw new Error("Set a size or an object limit, or choose \"No limit\".");
    return { enabled: true, maxSize: bytes, maxObjects: count };
  };
  const paint = () => {
    limits.hidden = mode.value !== "limit";
    warn.hidden = mode.value !== "unset";
  };
  const dirty = () => {
    if (mode.value !== mode0) return true;
    if (mode.value !== "limit") return false;
    try { const n = read(); return n.maxSize !== q.quota.maxSize || n.maxObjects !== q.quota.maxObjects; } catch { return true; }
  };
  const changed = () => { paint(); fSize.error(""); ft.update(dirty()); };
  for (const el of [mode, size, unit, objs]) el.addEventListener("input", changed);
  paint();
  const form = h("form", { novalidate: true, class: "set-form" }, fMode.el, limits, warn, ft.el);
  wireForm(form, ft, async () => {
    try {
      const body = mode.value === "unset" ? null : read();
      const res = body ? await saveDefaultQuota(body) : await clearDefaultQuota();
      toast.show(body ? "Default quota saved." : "Default quota reset to the built-in default.");
      apply({ defaultQuota: res });
    } catch (err) {
      fSize.error(err.message);
      limits.hidden = false;
      ft.update(true);
    }
  });
  ft.cancel.addEventListener("click", () => apply({ defaultQuota: q }));
  const status = !q.isSet ? chip(`${quotaText(q.default)} (default)`) : chip(q.quota.enabled ? quotaText(q.quota) : "No limit", "chip-ok");
  return card("Default quota", status,
    "The per-user quota Bucketeer applies when it creates an S3 user at first login. Unless you change it, that is 1 TiB with unlimited objects. \"No limit\" is a deliberate choice and differs from the default.", form);
}

function mappingsSection(data, apply) {
  const rows = data.uidMappings;
  const ad = textInput({ placeholder: "jdoe", "aria-label": "AD username" });
  const uid = textInput({ placeholder: "legacy-jdoe or tenant$jdoe", "aria-label": "RGW user id" });
  const fAd = field("AD username", ad);
  const fUid = field("RGW user id", uid, { hint: "An existing RGW user. A tenant is written tenant$uid." });
  const add = h("button", { class: "btn btn-primary", type: "submit" }, icon("plus"), "Save mapping");
  const clear = h("button", { class: "btn", type: "button", text: "Clear", hidden: true });
  const form = h("form", { novalidate: true, class: "set-form set-inline" },
    h("div", { class: "set-grid" }, fAd.el, fUid.el), h("div", { class: "set-actions" }, clear, add));
  clear.addEventListener("click", () => { ad.value = uid.value = ""; clear.hidden = true; add.lastChild.textContent = "Save mapping"; fAd.error(""); fUid.error(""); });
  let busy = false;
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (busy) return;
    busy = true; add.disabled = true;
    fAd.error(""); fUid.error("");
    try {
      const res = await saveUidMapping(ad.value.trim(), uid.value.trim());
      const next = [...rows.filter((r) => r.adUsername.toLowerCase() !== res.adUsername.toLowerCase()), res]
        .sort((a, b) => a.adUsername.localeCompare(b.adUsername));
      res.warning ? toast.show(res.warning) : toast.show("Mapping saved.");
      apply({ uidMappings: next });
    } catch (err) {
      (err.message.toLowerCase().includes("username") ? fAd : fUid).error(err.message);
    } finally { busy = false; add.disabled = false; }
  });
  const list = rows.length
    ? h("ul", { class: "set-list" }, rows.map((r) => h("li", { class: "set-item" },
      h("div", { class: "set-item-main" },
        h("span", { class: "set-pair" }, h("b", { text: r.adUsername }), icon("right", "icon set-arrow"), h("code", { text: r.rgwUid })),
        h("div", { class: "chips" }, r.exists === false ? chip("Not found in croit", "chip-warn") : r.exists === true ? chip("RGW user exists", "chip-ok") : chip("Not checked"))),
      h("div", { class: "acts" },
        h("button", { class: "btn btn-sm", type: "button", "aria-label": `Edit mapping of ${r.adUsername}`, on: { click: () => { ad.value = r.adUsername; uid.value = r.rgwUid; clear.hidden = false; add.lastChild.textContent = "Update mapping"; uid.focus(); } } }, icon("edit"), "Edit"),
        h("button", { class: "btn btn-sm btn-quiet-danger", type: "button", "aria-label": `Remove mapping of ${r.adUsername}`, on: { click: async () => {
          if (!(await confirmDialog({ title: `Remove mapping for ${r.adUsername}?`, message: `${r.adUsername} will use their own name as the RGW user id again.`, confirmLabel: "Remove", danger: true }))) return;
          try { await deleteUidMapping(r.adUsername); toast.show("Mapping removed."); apply({ uidMappings: rows.filter((x) => x !== r) }); } catch (err) { toast.error(err.message); }
        } } }, icon("trash"), "Remove")))))
    : emptyRow("No mappings. Everyone uses their AD username as their RGW user id.");
  return card("User id mappings", chip(`${rows.length} mapping${rows.length === 1 ? "" : "s"}`),
    "By default a person's RGW user id is their AD username. Map the exceptions here, for example people who already have an RGW user under another name.",
    list, form);
}

const EP_TYPES = [["https", "HTTPS webhook"], ["http", "HTTP webhook"], ["amqp", "AMQP"], ["kafka", "Kafka"]];
const SCHEME_HINT = { https: "https", http: "http", amqp: "amqp or amqps", kafka: "kafka" };
const TOPIC_LABEL = { amqp: "Exchange", kafka: "Topic" };

function endpointsSection(data, apply) {
  const rows = data.notificationEndpoints;
  const name = textInput({ placeholder: "ci-webhook", "aria-label": "Endpoint name" });
  const type = h("select", { class: "input", id: nid("s") }, EP_TYPES.map(([v, t]) => h("option", { value: v, text: t })));
  const url = textInput({ placeholder: "host.example.org:8080/path" });
  const topic = textInput({});
  const verify = h("input", { type: "checkbox", checked: true });
  const fName = field("Name", name, { hint: "What users see when they pick an endpoint." });
  const fType = field("Type", type);
  const fUrl = field("Address", url);
  const fTopic = field("Topic", topic, { hint: "Optional." });
  const add = h("button", { class: "btn btn-primary", type: "submit" }, icon("plus"), "Save endpoint");
  const clear = h("button", { class: "btn", type: "button", text: "Clear", hidden: true });
  const verifyEl = check("Verify TLS certificate", verify, "Turn off only for internal brokers with self-signed certificates.");
  const paint = () => {
    const t = type.value;
    const label = fUrl.el.querySelector("label");
    label.textContent = `Address (starts with ${SCHEME_HINT[t]}://)`;
    fTopic.el.hidden = !(t in TOPIC_LABEL);
    fTopic.el.querySelector("label").textContent = `${TOPIC_LABEL[t] ?? "Topic"} (optional)`;
    verifyEl.hidden = !(t === "https" || t === "amqp");
  };
  type.addEventListener("change", paint);
  paint();
  const form = h("form", { novalidate: true, class: "set-form set-inline" },
    h("div", { class: "set-grid" }, fName.el, fType.el, fUrl.el, fTopic.el), verifyEl,
    h("div", { class: "set-actions" }, clear, add));
  const reset = () => { name.value = url.value = topic.value = ""; type.value = "https"; verify.checked = true; name.readOnly = false; clear.hidden = true; add.lastChild.textContent = "Save endpoint"; for (const f of [fName, fType, fUrl, fTopic]) f.error(""); paint(); };
  clear.addEventListener("click", reset);
  let busy = false;
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (busy) return;
    busy = true; add.disabled = true;
    for (const f of [fName, fType, fUrl, fTopic]) f.error("");
    try {
      const res = await saveEndpoint(name.value.trim(), { type: type.value, url: url.value.trim(), topic: topic.value.trim(), verifyTls: verify.checked });
      toast.show("Endpoint saved.");
      apply({ notificationEndpoints: [...rows.filter((r) => r.name.toLowerCase() !== res.name.toLowerCase()), res].sort((a, b) => a.name.localeCompare(b.name)) });
    } catch (err) {
      const m = err.message.toLowerCase();
      (m.includes("name") ? fName : m.includes("topic") || m.includes("exchange") ? fTopic : fUrl).error(err.message);
    } finally { busy = false; add.disabled = false; }
  });
  const list = rows.length
    ? h("ul", { class: "set-list" }, rows.map((r) => h("li", { class: "set-item" },
      h("div", { class: "set-item-main" },
        h("span", { class: "set-pair" }, h("b", { text: r.name }), chip(r.type.toUpperCase())),
        h("code", { class: "set-url", text: r.url }),
        h("div", { class: "chips" },
          r.topic ? chip(`${TOPIC_LABEL[r.type] ?? "Topic"}: ${r.topic}`) : null,
          (r.type === "https" || r.type === "amqp") && (r.url.startsWith("https") || r.url.startsWith("amqps")) ? chip(r.verifyTls ? "TLS verified" : "TLS not verified", r.verifyTls ? "chip-ok" : "chip-warn") : null)),
      h("div", { class: "acts" },
        h("button", { class: "btn btn-sm", type: "button", "aria-label": `Edit endpoint ${r.name}`, on: { click: () => { name.value = r.name; name.readOnly = true; type.value = r.type; url.value = r.url; topic.value = r.topic; verify.checked = r.verifyTls; clear.hidden = false; add.lastChild.textContent = "Update endpoint"; paint(); url.focus(); } } }, icon("edit"), "Edit"),
        h("button", { class: "btn btn-sm btn-quiet-danger", type: "button", "aria-label": `Remove endpoint ${r.name}`, on: { click: async () => {
          if (!(await confirmDialog({ title: `Remove ${r.name}?`, message: "Users can no longer choose this endpoint for new notifications. Existing notification configurations are not changed.", confirmLabel: "Remove", danger: true }))) return;
          try { await deleteEndpoint(r.name); toast.show("Endpoint removed."); apply({ notificationEndpoints: rows.filter((x) => x !== r) }); } catch (err) { toast.error(err.message); }
        } } }, icon("trash"), "Remove")))))
    : emptyRow("No endpoints. Users cannot set up bucket notifications until you add one.");
  return card("Notification endpoints", chip(`${rows.length} endpoint${rows.length === 1 ? "" : "s"}`),
    "The only destinations users may send bucket notifications to. Credentials are not supported yet: addresses cannot contain a user name, password, query string or token.",
    list, form);
}

// --- the tab -------------------------------------------------------------------

export function renderSettings() {
  const host = h("div", { class: "settings", "aria-label": "Settings" });
  let data = null;
  const slots = {};

  const PATCH_SECTION = { naming: "naming", budgetNumber: "budget", placement: "placement", defaultQuota: "quota", uidMappings: "mappings", notificationEndpoints: "endpoints" };
  function paint(only = null) {
    const make = {
      naming: () => namingSection(data, apply),
      budget: () => budgetSection(data, apply),
      placement: () => placementSection(data, apply, load),
      quota: () => quotaSection(data, apply),
      mappings: () => mappingsSection(data, apply),
      endpoints: () => endpointsSection(data, apply),
    };
    for (const [k, fn] of Object.entries(make)) {
      if (only && !only.includes(k)) continue;
      const next = fn();
      next.dataset.section = k;
      if (slots[k]) slots[k].replaceWith(next); else host.append(next);
      slots[k] = next;
    }
  }
  // Merge a server-confirmed change and rebuild (sections are cheap to rebuild).
  function apply(patch) {
    data = { ...data, ...patch };
    paint(Object.keys(patch).map((k) => PATCH_SECTION[k]));
  }

  async function load() {
    if (!data) host.replaceChildren(h("div", { class: "set-loading", "aria-hidden": "true" }, [1, 2, 3].map(() => h("div", { class: "skel-field set-skel" }))));
    try {
      data = await getSettings();
      host.replaceChildren();
      for (const k of Object.keys(slots)) delete slots[k];
      paint();
    } catch (err) {
      if (err.status === 401) return;
      data = null;
      host.replaceChildren(h("div", { class: "empty" }, icon("alert", "icon empty-icon"), h("h3", { text: "Could not load the settings" }), h("p", { text: err.message }),
        h("div", { class: "empty-acts" }, h("button", { class: "btn btn-primary", type: "button", on: { click: load } }, icon("refresh"), "Try again"))));
    }
  }
  return { el: host, load };
}
