import { h } from "../dom.js";
import { icon } from "../icons.js";
import { checkCloudBucketName, cloudBucketOptions, createCloudBucket } from "../api.js";
import { checkBudget } from "../budget.js";
import { formatMoney, quotaPicker } from "./quota-picker.js";

// cloudBucketDialog() -> Promise<{ created } | { error }>
// Create a cloud (AWS) bucket: the person types only the last part of the name; the full AWS
// name is `<prefix><name>`. Availability is checked as they type (debounced, the server asks
// AWS) and free alternatives are offered when the name is taken. The storage class is chosen
// once and never changes. The server is authoritative for every rule.
let seq = 0;
const NAME = /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/;
const DEBOUNCE_MS = 450;

const CLASS_TEXT = {
  intelligent: {
    title: "Intelligent-Tiering",
    lead: "For data you may read again.",
    points: [
      "AWS moves each object between access tiers automatically, so rarely read data costs less.",
      "Objects can be read at any time, with any S3 tool.",
    ],
  },
  deep_archive: {
    title: "Glacier Deep Archive",
    lead: "For data you keep but almost never read.",
    points: [
      "The cheapest storage, but objects can't be read directly: each one needs a restore first, which takes 12 to 48 hours and is done with another tool, not with Bucketeer.",
      "AWS charges every object for at least 180 days, even if you delete it sooner. Restores and downloads cost extra.",
    ],
  },
};

export function checkCloudName(name, room) {
  if (!name) return "Enter a bucket name.";
  if (name.length > room) return `Use at most ${room} characters.`;
  if (!NAME.test(name)) return "Use lowercase letters, digits and hyphens (no dots); start and end with a letter or digit.";
  return "";
}

export async function cloudBucketDialog() {
  const id = `ccb${++seq}`;
  let opts;
  try {
    opts = await cloudBucketOptions();
  } catch (err) {
    return { error: err };
  }
  if (opts.refused) return { error: new Error(opts.refused) };
  const room = opts.maxNameLength;
  const products = Object.fromEntries(opts.storageClasses.map((c) => [c.id, c.product]));
  let storageClass = opts.defaultStorageClass || "intelligent";

  const name = h("input", {
    class: "input mono-input", id: `${id}-n`, autocomplete: "off", autocapitalize: "off",
    spellcheck: "false", maxlength: String(room), placeholder: "my-data", "aria-describedby": `${id}-nf ${id}-ns`,
  });
  const full = h("p", { class: "set-hint ccb-full", id: `${id}-nf` });
  const status = h("div", { class: "ccb-status", id: `${id}-ns`, "aria-live": "polite" });
  const budget = h("input", {
    class: "input mono-input", id: `${id}-b`, value: opts.defaultBudgetNumber ?? "", autocomplete: "off",
    spellcheck: "false", maxlength: "64", placeholder: opts.budgetExample ? `e.g. ${opts.budgetExample}` : "e.g. DN099999",
    "aria-describedby": `${id}-be`, required: opts.budgetRequired,
  });
  const budgetErr = h("p", { class: "set-err", id: `${id}-be`, role: "alert", hidden: true });
  const quota = quotaPicker({ id: `${id}-qp`, value: opts.quota?.defaultTiB, info: opts.quota, product: products[storageClass] });
  const versioning = h("input", { type: "checkbox", id: `${id}-v`, checked: true });
  const error = h("p", { class: "qd-error", role: "alert", hidden: true });
  const cancel = h("button", { class: "btn", type: "button", text: "Cancel" });
  const submit = h("button", { class: "btn btn-primary", type: "submit", text: "Create cloud bucket" });

  // storage class: two radio cards
  const radios = {};
  const classCards = opts.storageClasses.map((c) => {
    const t = CLASS_TEXT[c.id];
    const p = c.product;
    const input = h("input", { type: "radio", name: `${id}-sc`, value: c.id, checked: c.id === storageClass, class: "ccb-radio" });
    radios[c.id] = input;
    input.addEventListener("change", () => {
      if (!input.checked) return;
      storageClass = c.id;
      quota.setProduct(products[c.id]);
      paintClasses();
    });
    return h("label", { class: "ccb-class", "data-class": c.id },
      input,
      h("span", { class: "ccb-class-body" },
        h("span", { class: "ccb-class-head" },
          h("b", { text: t.title }),
          h("span", { class: "chip", text: p.name }),
          h("span", { class: "ccb-price", text: p.advertised || `about ${formatMoney(Number(p.pricePerTiBMonth) || 0, p.currency)} per TiB-month` })),
        h("span", { class: "ccb-lead", text: t.lead }),
        h("ul", { class: "ccb-points" }, t.points.map((x) => h("li", { text: x })))));
  });
  const archiveWarn = h("div", { class: "set-warn", hidden: true },
    icon("alert", "icon"),
    h("span", { text: "Deep Archive: reading data back needs a restore (12 to 48 hours, with another tool), and every object is charged for at least 180 days." }));
  function paintClasses() {
    for (const [k, r] of Object.entries(radios)) r.closest(".ccb-class").dataset.on = String(r.checked && k === storageClass);
    archiveWarn.hidden = storageClass !== "deep_archive";
  }

  const form = h("form", { novalidate: true },
    h("div", { class: "qd-head" },
      h("h2", { id: `${id}-t` }, icon("cloud"), " Create a cloud bucket"),
      h("p", { class: "cd-msg", id: `${id}-m`, text: `Stored at AWS (${opts.region}). The storage class is chosen now and can't be changed later. Bucket names are shared by everyone on AWS.` })),
    h("div", { class: "field" },
      h("label", { for: name.id, text: "Bucket name" }),
      h("div", { class: "ccb-name" }, h("span", { class: "ccb-prefix", text: opts.prefix, "aria-hidden": "true" }), name),
      full, status),
    h("fieldset", { class: "ccb-classes" },
      h("legend", { text: "Storage class" }), ...classCards, archiveWarn),
    quota.el,
    h("div", { class: "field" },
      h("label", { for: budget.id }, "Budget number ", h("span", { class: "cb-opt", text: opts.budgetRequired ? "(required)" : "(optional for administrators)" })),
      budget, budgetErr),
    h("label", { class: "qd-check", for: versioning.id }, versioning,
      h("span", {}, h("b", { text: "Keep every version of each object" }),
        h("small", { text: "Protects against overwrites and deletes. Old versions are deleted after 30 days and are billed until then." }))),
    h("p", { class: "set-hint", text: "Real AWS costs are billed to your budget number. To delete the bucket later, empty it first with your own cloud key and an S3 tool." }),
    error,
    h("div", { class: "cd-actions" }, cancel, submit));
  const dlg = h("dialog", { class: "cd qd cb ccb", "aria-labelledby": `${id}-t`, "aria-describedby": `${id}-m` }, form);

  // -- availability (debounced; only the latest answer counts) --
  let timer = null;
  let asked = 0;
  let available = null; // null = unknown, true/false from the server
  const showStatus = (kind, text, suggestions = []) => {
    status.dataset.kind = kind;
    const kids = [];
    if (kind === "busy") kids.push(h("span", { class: "spinner", "aria-hidden": "true" }));
    else if (kind === "ok") kids.push(icon("check", "icon"));
    else if (kind) kids.push(icon("alert", "icon"));
    if (text) kids.push(h("span", { text }));
    const row = h("span", { class: "ccb-status-row" }, ...kids);
    status.replaceChildren(row);
    if (suggestions.length) {
      status.append(h("div", { class: "ccb-sugg" }, h("span", { text: "Free instead:" }),
        suggestions.map((s) => h("button", {
          class: "btn btn-sm ccb-sugg-btn", type: "button", text: s.name, title: s.fullName,
          on: { click: () => { name.value = s.name; onName(); name.focus(); } },
        }))));
    }
  };
  function onName() {
    clearTimeout(timer);
    showError("");
    const v = name.value;
    full.textContent = v ? `Full AWS name: ${opts.prefix}${v}` : `Full AWS name: ${opts.prefix}…`;
    available = null;
    const m = checkCloudName(v, room);
    name.setAttribute("aria-invalid", String(!!(m && v)));
    if (m) { showStatus(v ? "bad" : "", v ? m : ""); return; }
    showStatus("busy", "Checking…");
    const mine = ++asked;
    timer = setTimeout(async () => {
      try {
        const res = await checkCloudBucketName(v);
        if (mine !== asked) return;
        available = res.available;
        showStatus(res.available ? "ok" : "bad", res.message, res.suggestions || []);
      } catch (err) {
        if (mine !== asked) return;
        showStatus("bad", err.message || "Could not check the name.");
      }
    }, DEBOUNCE_MS);
  }
  name.addEventListener("input", onName);
  onName();
  paintClasses();

  const validateBudget = () => {
    const v = budget.value.trim();
    const m = !v && opts.budgetRequired ? "Enter a budget number." : checkBudget(budget.value);
    budgetErr.textContent = !v ? "" : m;
    budgetErr.hidden = !v || !m;
    budget.setAttribute("aria-invalid", String(!!(m && v)));
    return m;
  };
  budget.addEventListener("input", () => { validateBudget(); showError(""); });
  function showError(msg) { error.textContent = msg; error.hidden = !msg; }

  let busy = false;
  let result = null;
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (busy) return;
    const nm = checkCloudName(name.value, room);
    if (nm) { showStatus("bad", nm); name.focus(); return; }
    if (available === false) { name.focus(); return; }
    const bm = validateBudget();
    if (bm) { budgetErr.textContent = bm; budgetErr.hidden = false; budget.focus(); return; }
    if (quota.validate()) { quota.input.focus(); return; }
    showError("");
    busy = true;
    submit.disabled = true;
    submit.replaceChildren(h("span", { class: "spinner", "aria-hidden": "true" }), "Creating at AWS…");
    try {
      result = await createCloudBucket({
        name: name.value, storageClass, quotaTiB: quota.value(), versioning: versioning.checked,
        budgetNumber: budget.value.trim() || null,
      });
      dlg.close();
    } catch (err) {
      if (err.status === 401) { dlg.close(); return; }
      if (err.status === 409 && err.body?.remainingTiB != null) {
        quota.update({ remainingTiB: err.body.remainingTiB, nextReleaseAt: err.body.freesUpAt ?? null });
      }
      // The limit note under the quota field already explains a weekly-limit refusal.
      showError(quota.over() ? "" : err.message || "Could not create the bucket.");
      if (quota.over()) quota.input.focus();
      submit.disabled = false;
      submit.textContent = "Create cloud bucket";
    } finally {
      busy = false;
    }
  });
  cancel.addEventListener("click", () => dlg.close());
  dlg.addEventListener("cancel", (e) => { if (busy) e.preventDefault(); });
  dlg.addEventListener("click", (e) => { if (e.target === dlg && !busy) dlg.close(); });

  return new Promise((resolve) => {
    dlg.addEventListener("close", () => { clearTimeout(timer); dlg.remove(); resolve({ created: result }); });
    document.body.append(dlg);
    dlg.showModal();
    name.focus();
  });
}
