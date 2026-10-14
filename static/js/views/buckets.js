import { h } from "../dom.js";
import { icon } from "../icons.js";
import { deleteBucket, listBuckets, reconcileBuckets, setBucketVersioning } from "../api.js";
import { formatBytes, formatCount } from "../format.js";
import { toast } from "../components/toast.js";
import { confirmDialog, nameSpan } from "../components/confirm-dialog.js";
import { createBucketDialog } from "../components/create-bucket-dialog.js";
import { bucketKeysDialog } from "../components/bucket-keys-dialog.js";
import { lifecycleDialog } from "../components/lifecycle-dialog.js";

// The Buckets card on the home page: the user's own buckets with usage, quota and
// budget number; create and delete. `isAdvanced()` tells whether the global
// Advanced toggle is on at the moment the create dialog opens.

const VERSIONING = { enabled: "Versioned", suspended: "Versioning paused" };

function versionSwitch(b, onToggle) {
  const on = b.versioning === "enabled";
  const locked = on && b.objectLock === true;
  const known = b.versioning === "off" || b.versioning === "enabled" || b.versioning === "suspended";
  const note = locked ? "Object lock keeps versioning on for good."
    : b.versioning === "suspended" ? "Versioning is paused. Switch on to resume it."
    : b.versioning === "off" ? "Keep old versions of changed or deleted files."
    : "Versioning state unavailable.";
  const sw = h("button", {
    class: "switch bk-vsw", type: "button", role: "switch", "aria-checked": String(on),
    disabled: locked || !known, title: note, "aria-label": `Versioning for ${b.name}. ${note}`,
    on: { click: () => onToggle(b) },
  }, h("span", { class: "switch-label", text: "Versioning" }), h("span", { class: "switch-track", "aria-hidden": "true" }));
  return sw;
}

function skeleton(n = 3) {
  return h("div", { class: "skel", "aria-hidden": "true" },
    Array.from({ length: n }, () =>
      h("div", { class: "skel-row" }, h("div", { class: "skel-dot" }),
        h("div", { class: "skel-stack" }, h("div", { class: "skel-line w60" }), h("div", { class: "skel-line w40" })),
        h("div", { class: "skel-pill" }))));
}

function usageBlock(b) {
  const q = b.quota;
  const capBytes = q.enabled ? q.maxSize : null;
  const capObjects = q.enabled ? q.maxObjects : null;
  const pct = Math.max(capBytes ? b.bytes / capBytes : 0, capObjects ? b.objects / capObjects : 0) * 100;
  const text = h("span", { class: "bk-usage-text" },
    h("b", { text: formatBytes(b.bytes) }), capBytes ? ` of ${formatBytes(capBytes)}` : "",
    h("span", { class: "bk-sep", text: " · " }),
    `${formatCount(b.objects)}${capObjects ? ` of ${formatCount(capObjects)}` : ""} ${b.objects === 1 && !capObjects ? "object" : "objects"}`);
  const out = h("div", { class: "bk-usage" }, text);
  if (capBytes || capObjects) {
    const fill = h("span", { class: "meter-fill" });
    fill.style.width = `${Math.min(100, pct)}%`;
    out.append(h("span", {
      class: "meter bk-meter", "data-level": pct >= 90 ? "high" : pct >= 70 ? "mid" : "ok",
      role: "img", "aria-label": `${Math.round(pct)}% of this bucket's quota used`,
    }, fill));
  } else {
    out.append(h("span", { class: "bk-noquota", text: "No bucket quota" }));
  }
  return out;
}

function row(b, onDelete, onToggle, onKeys, onLifecycle, busy) {
  const created = b.createdAt ? new Date(b.createdAt) : null;
  const meta = h("div", { class: "bk-meta" });
  if (created && !Number.isNaN(created.getTime())) {
    meta.append(h("time", { datetime: b.createdAt, text: `Created ${created.toLocaleDateString(undefined, { dateStyle: "medium" })}` }));
  }
  if (VERSIONING[b.versioning]) meta.append(h("span", { class: "chip chip-ok" }, h("span", { class: "dot" }), VERSIONING[b.versioning]));
  if (b.objectLock === true) meta.append(h("span", { class: "chip", title: "Object lock keeps versioning on for good." }, "Object lock"));
  const budget = b.budgetMissing
    ? h("span", { class: "chip chip-warn", title: "This bucket has no budget number yet." }, icon("alert", "icon"), "Missing budget number")
    : h("span", { class: "bk-budget" }, h("span", { class: "bk-budget-l", text: "Budget" }), h("code", { text: b.budgetNumber }));
  const del = h("button", {
    class: "btn btn-sm btn-quiet-danger", type: "button", disabled: busy, "aria-label": `Delete bucket ${b.name}`,
    on: { click: () => onDelete(b) },
  }, icon("trash"), h("span", { class: "bk-del-l", text: "Delete" }));
  const keysBtn = h("button", {
    class: "btn btn-sm", type: "button", disabled: busy, "aria-label": `Keys for bucket ${b.name}`,
    title: "Keys that work for this bucket only",
    on: { click: () => onKeys(b) },
  }, icon("key"), h("span", { class: "bk-del-l", text: b.keyCount ? `Keys (${b.keyCount})` : "Keys" }));
  const lcBtn = h("button", {
    class: "btn btn-sm", type: "button", disabled: busy, "aria-label": `Lifecycle rules for bucket ${b.name}`,
    title: "Delete old files or unfinished uploads automatically",
    on: { click: () => onLifecycle(b) },
  }, icon("refresh"), h("span", { class: "bk-del-l", text: "Lifecycle" }));
  const vsw = versionSwitch(b, onToggle);
  budget.classList.add("bk-budget-slot");
  return h("li", { class: "bk-row", "data-busy": String(!!busy) },
    h("span", { class: "bk-ic", "aria-hidden": "true" }, icon("bucket")),
    h("div", { class: "bk-main" }, h("b", { class: "bk-name", text: b.name }), meta),
    usageBlock(b),
    h("div", { class: "bk-tail" }, budget, h("div", { class: "bk-acts" }, vsw, lcBtn, keysBtn, del)));
}

export function bucketsCard({ isAdvanced = () => false } = {}) {
  let buckets = [];
  const deleting = new Set();
  const body = h("div", { class: "bk-body", "aria-live": "polite" }, skeleton());
  const createBtn = h("button", { class: "btn btn-primary btn-sm", type: "button", disabled: true }, icon("plus"), "Create bucket");
  const count = h("span", { class: "chip", hidden: true });
  const card = h("section", { class: "card panel panel-wide bk", "aria-labelledby": "h-buckets" },
    h("div", { class: "panel-head" },
      h("div", { class: "bk-title" }, h("h2", { id: "h-buckets", text: "Buckets" }), count),
      createBtn),
    h("p", { class: "panel-note", text: "Your buckets, with usage against quota and the budget number each is charged to." }),
    body,
    h("p", { class: "adv-only", text: "Advanced: policies, CORS, lifecycle and tagging will appear here." }));

  function paint() {
    count.hidden = !buckets.length;
    count.textContent = String(buckets.length);
    if (!buckets.length) {
      body.replaceChildren(h("div", { class: "empty" },
        icon("bucket", "empty-icon"), h("h3", { text: "No buckets yet" }),
        h("p", { text: "A bucket is a container for your files. Create your first one to start storing data." }),
        h("div", { class: "empty-acts" }, h("button", { class: "btn btn-primary", type: "button", on: { click: create } }, icon("plus"), "Create your first bucket"))));
      return;
    }
    body.replaceChildren(h("ul", { class: "bk-list" }, buckets.map((b) => row(b, remove, toggleVersioning, manageKeys, manageLifecycle, deleting.has(b.name) || deleting.has(`v:${b.name}`)))));
  }

  function failed(err) {
    body.replaceChildren(h("div", { class: "set-unavail" }, h("p", { text: err.message }),
      h("button", { class: "btn", type: "button", on: { click: () => load() } }, icon("refresh"), "Try again")));
  }

  async function load({ quiet = false } = {}) {
    if (!quiet) body.replaceChildren(skeleton());
    try {
      buckets = (await listBuckets()).buckets;
    } catch (err) {
      if (err.status !== 401) failed(err);
      return;
    }
    createBtn.disabled = false;
    paint();
    // A budget tag that could not be written at creation is retried here (a POST, never a GET).
    if (buckets.some((b) => b.tagPending)) {
      reconcileBuckets().then(() => load({ quiet: true })).catch(() => {});
    }
  }

  async function create() {
    createBtn.disabled = true;
    const res = await createBucketDialog({ advanced: isAdvanced() });
    createBtn.disabled = false;
    if (res.error) {
      if (res.error.status !== 401) toast.error(res.error.message);
      return;
    }
    if (!res.created) return;
    if (res.created.warning) toast.show(res.created.warning, { timeout: 9000 });
    else toast.show(`Bucket ${res.created.name} created.`);
    load({ quiet: true });
  }
  createBtn.addEventListener("click", create);

  async function remove(b, confirmed = false) {
    const nonEmpty = b.objects > 0;
    if (!confirmed) {
      const ok = await confirmDialog(nonEmpty
        ? {
          title: ["Delete ", nameSpan(b.name), " and its data?"], danger: true, confirmLabel: "Delete bucket and data", requireText: b.name,
          message: dataWarning(b.objects, b.bytes),
        }
        : { title: ["Delete ", nameSpan(b.name), "?"], danger: true, confirmLabel: "Delete bucket", message: "This bucket is empty. Its name becomes available to everyone again." });
      if (!ok) return;
    }
    deleting.add(b.name);
    paint();
    try {
      const res = await deleteBucket(b.name, confirmed || nonEmpty ? b.name : undefined);
      if (res?.warning) toast.show(res.warning, { timeout: 9000 });
      else toast.show(`Bucket ${b.name} deleted.`);
    } catch (err) {
      deleting.delete(b.name);
      if (err.status === 409 && err.body?.requiresConfirmation) {
        // The statistics said "empty" but the bucket holds data: ask again with the real numbers.
        paint();
        const ok = await confirmDialog({
          title: ["Delete ", nameSpan(b.name), " and its data?"], danger: true, confirmLabel: "Delete bucket and data", requireText: b.name,
          message: dataWarning(err.body.objects, err.body.bytes),
        });
        if (ok) await remove({ ...b, objects: Math.max(1, err.body.objects) }, true);
        return;
      }
      if (err.status !== 401) toast.error(err.message);
    }
    deleting.delete(b.name);
    await load({ quiet: true });
  }

  async function manageKeys(b) {
    const res = await bucketKeysDialog({ bucket: b.name });
    if (res.error) {
      if (res.error.status !== 401) toast.error(res.error.message);
      return;
    }
    if (res.changed) load({ quiet: true });
  }

  async function manageLifecycle(b) {
    const res = await lifecycleDialog({ bucket: b.name });
    if (res.error && res.error.status !== 401) toast.error(res.error.message);
  }

  async function toggleVersioning(b) {
    const enable = b.versioning !== "enabled";
    if (!enable) {
      const ok = await confirmDialog({
        title: ["Pause versioning on ", nameSpan(b.name), "?"], confirmLabel: "Pause versioning",
        message: "New uploads will no longer create versions. Versions that already exist are kept and still count toward your quota. Versioning can never be turned off completely, only paused and resumed.",
      });
      if (!ok) return;
    }
    const key = `v:${b.name}`;
    deleting.add(key);
    paint();
    try {
      await setBucketVersioning(b.name, enable);
      toast.show(enable ? `Versioning is on for ${b.name}.` : `Versioning is paused for ${b.name}.`);
    } catch (err) {
      if (err.status !== 401) toast.error(err.message);
    }
    deleting.delete(key);
    await load({ quiet: true });
  }

  function dataWarning(objects, bytes) {
    return h("span", {},
      "This permanently deletes ", h("strong", { text: `${formatCount(objects)} ${objects === 1 ? "object" : "objects"}` }),
      objects ? ` (${formatBytes(bytes)})` : "", " and every version of them. It cannot be undone.");
  }

  load();
  return card;
}
