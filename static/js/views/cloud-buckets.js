import { h } from "../dom.js";
import { icon } from "../icons.js";
import { cloudBucketOptions, deleteCloudBucket, listCloudBuckets, listCloudDeleteJobs, retryCloudDeleteJob, setCloudBucketBudget, setCloudBucketQuota } from "../api.js";
import { formatBytes } from "../format.js";
import { toast } from "../components/toast.js";
import { deleteBucketDialog, deleteJobBlock } from "../components/bucket-delete.js";
import { cloudBucketDialog } from "../components/cloud-bucket-dialog.js";
import { budgetDialog } from "../components/budget-dialog.js";
import { bucketQuotaDialog } from "../components/bucket-quota-dialog.js";
import { formatMoney, formatTiB } from "../components/quota-picker.js";
import { menuButton } from "../components/menu-button.js";

// The Buckets card while the effective location is Cloud (P6-10): the person's AWS buckets with
// their storage class, size from CloudWatch (daily, a day or two late), quota and the estimated
// monthly cost from the catalog price (the real AWS cost is billed). Create, change quota or
// budget number, delete (the typed name always; a bucket with content is emptied by a
// background job with a temporary key of the owner, P6-13).

const CLASS = { intelligent: "Intelligent-Tiering", deep_archive: "Deep Archive" };

function skeleton(n = 2) {
  return h("div", { class: "skel", "aria-hidden": "true" },
    Array.from({ length: n }, () =>
      h("div", { class: "skel-row" }, h("div", { class: "skel-dot" }),
        h("div", { class: "skel-stack" }, h("div", { class: "skel-line w60" }), h("div", { class: "skel-line w40" })),
        h("div", { class: "skel-pill" }))));
}

function shortDay(iso) {
  const d = new Date(`${iso}T12:00:00Z`);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function usageBlock(b) {
  const cap = b.quotaTiB ? b.quotaTiB * 2 ** 40 : null;
  const out = h("div", { class: "bk-usage" });
  if (b.bytes == null) {
    out.append(h("span", { class: "bk-usage-text" }, h("b", { text: "Size not measured yet" }),
      h("span", { class: "bk-sep", text: " · " }), "AWS reports it once a day"));
  } else {
    out.append(h("span", { class: "bk-usage-text" }, h("b", { text: formatBytes(b.bytes) }),
      cap ? ` of ${formatTiB(b.quotaTiB)}` : "",
      h("span", { class: "bk-sep", text: " · " }),
      h("span", { class: "cbk-asof", text: `updated ${shortDay(b.sizeAsOf)}` })));
    if (cap) {
      const pct = (b.bytes / cap) * 100;
      const fill = h("span", { class: "meter-fill" });
      fill.style.width = `${Math.min(100, pct)}%`;
      out.append(h("span", {
        class: "meter bk-meter", "data-level": pct >= 100 ? "high" : pct >= 70 ? "mid" : "ok",
        role: "img", "aria-label": `${Math.round(pct)}% of this bucket's quota used`,
      }, fill));
    }
  }
  const p = b.product;
  const cost = [h("span", { class: "bk-cost-l", text: "Quota" }), h("b", { text: formatTiB(b.quotaTiB) })];
  if (b.estimatedCost != null) {
    cost.push(h("span", { class: "bk-sep", text: " · " }),
      h("span", { class: "bk-cost-m", text: `about ${formatMoney(b.estimatedCost, p.currency)} per month now` }));
  } else if (b.monthlyCost != null) {
    cost.push(h("span", { class: "bk-sep", text: " · " }),
      h("span", { class: "bk-cost-m", text: `about ${formatMoney(b.monthlyCost, p.currency)} per month if full` }));
  }
  out.append(h("span", { class: "bk-cost", title: `${p.name}: about ${formatMoney(Number(p.pricePerTiBMonth) || 0, p.currency)} per TiB and month. The real AWS cost is billed.` }, ...cost));
  return out;
}

function row(b, acts, busy, job) {
  const meta = h("div", { class: "bk-meta" });
  const created = b.createdAt ? new Date(b.createdAt) : null;
  if (created && !Number.isNaN(created.getTime())) {
    meta.append(h("time", { datetime: b.createdAt, text: `Created ${created.toLocaleDateString(undefined, { dateStyle: "medium" })}` }));
  }
  meta.append(h("span", { class: "chip cbk-class", "data-class": b.storageClass, title: b.product.name }, icon("cloud", "icon"), CLASS[b.storageClass] || b.product.name));
  if (b.overQuota) meta.append(h("span", { class: "chip chip-warn", title: "AWS does not stop uploads at the quota. Raise it or delete data." }, icon("alert", "icon"), "Over quota"));
  const budget = b.budgetMissing
    ? h("span", { class: "chip chip-warn" }, icon("alert", "icon"), "Missing budget number")
    : h("span", { class: "bk-budget" }, h("span", { class: "bk-budget-l", text: "Budget" }), h("code", { text: b.budgetNumber }));
  budget.classList.add("bk-budget-slot");
  const more = menuButton({
    label: "More", ariaLabel: `More actions for cloud bucket ${b.name}`, disabled: busy, className: "bk-more",
    items: [
      { label: "Change quota…", icon: "sliders", hint: `Now ${formatTiB(b.quotaTiB)}`, onSelect: () => acts.quota(b) },
      { label: "Change budget number…", icon: "edit", hint: "The index or worktag the AWS cost is charged to", onSelect: () => acts.budget(b) },
      { label: "Delete bucket", icon: "trash", danger: true, separatorBefore: true, disabled: !!job && (job.state === "queued" || job.state === "running"), onSelect: () => acts.remove(b) },
    ],
  });
  const deleting = !!job && (job.state === "queued" || job.state === "running");
  return h("li", { class: "bk-row", "data-busy": String(!!busy), "data-deleting": String(deleting) },
    h("span", { class: "bk-ic", "aria-hidden": "true" }, icon("cloud")),
    h("div", { class: "bk-main" }, h("b", { class: "bk-name", text: b.name, title: b.fullName }),
      h("code", { class: "cbk-full", text: b.fullName }), meta, job ? deleteJobBlock(job, acts.retry) : null),
    usageBlock(b),
    h("div", { class: "bk-tail" }, budget, h("div", { class: "bk-acts" }, more)));
}

export function cloudBucketsCard() {
  let buckets = [];
  let region = "";
  const busy = new Set();
  const jobs = new Map(); // short bucket name -> its delete job (not done)
  let pollTimer = null;
  const body = h("div", { class: "bk-body", "aria-live": "polite" }, skeleton());
  const createBtn = h("button", { class: "btn btn-primary btn-sm", type: "button", disabled: true }, icon("plus"), "Create cloud bucket");
  const count = h("span", { class: "chip", hidden: true });
  const note = h("p", { class: "panel-note", text: "Your buckets at AWS. Sizes come from AWS once a day; costs are estimates from the advertised price, and the real AWS cost is billed." });
  const card = h("section", { class: "card panel panel-wide bk cbk", "aria-labelledby": "h-cloud-buckets" },
    h("div", { class: "panel-head" },
      h("div", { class: "bk-title" }, h("h2", { id: "h-cloud-buckets" }, "Your cloud buckets"), count),
      createBtn),
    note, body);

  function paint() {
    count.hidden = !buckets.length;
    count.textContent = String(buckets.length);
    if (!buckets.length) {
      body.replaceChildren(h("div", { class: "empty" },
        icon("cloud", "empty-icon"), h("h3", { text: "No cloud buckets yet" }),
        h("p", { text: `Cloud buckets live at AWS${region ? ` (${region})` : ""}. Choose Intelligent-Tiering for data you may read again, or Deep Archive for data you keep but almost never read.` }),
        h("div", { class: "empty-acts" }, h("button", { class: "btn btn-primary", type: "button", on: { click: create } }, icon("plus"), "Create your first cloud bucket"))));
      return;
    }
    const acts = { quota: changeQuota, budget: changeBudget, remove, retry: retryJob };
    body.replaceChildren(h("ul", { class: "bk-list" }, buckets.map((b) => row(b, acts, busy.has(b.name), jobs.get(b.name)))));
  }

  async function load({ quiet = false } = {}) {
    if (!quiet) body.replaceChildren(skeleton());
    try {
      const res = await listCloudBuckets();
      buckets = res.buckets;
      region = res.region;
    } catch (err) {
      if (err.status === 401) return;
      body.replaceChildren(h("div", { class: "set-unavail" }, h("p", { text: err.message }),
        h("button", { class: "btn", type: "button", on: { click: () => load() } }, icon("refresh"), "Try again")));
      return;
    }
    createBtn.disabled = false;
    await refreshJobs({ repaint: false });
    paint();
  }

  // Delete jobs run on the server; poll while one is active, stop when none is or the card is gone.
  async function refreshJobs({ repaint = true } = {}) {
    let list;
    try {
      list = (await listCloudDeleteJobs()).jobs;
    } catch {
      return;
    }
    const prev = new Map(jobs);
    jobs.clear();
    for (const j of list) if (j.state !== "done") jobs.set(j.bucket, j);
    let finished = false;
    for (const [name, was] of prev) {
      if (was.state !== "queued" && was.state !== "running") continue;
      const now = jobs.get(name);
      if (!now) finished = true;
      else if (now.state === "failed" || now.state === "blocked") toast.error(`Deleting ${name} stopped: ${now.message || "try again."}`, { timeout: 12000 });
    }
    if (finished) toast.show("Cloud bucket deleted.");
    schedulePoll();
    if (finished) await load({ quiet: true });
    else if (repaint && buckets.length) paint();
  }

  function schedulePoll() {
    clearTimeout(pollTimer);
    pollTimer = null;
    if (![...jobs.values()].some((j) => j.state === "queued" || j.state === "running")) return;
    pollTimer = setTimeout(() => { if (card.isConnected) refreshJobs(); }, 3000);
  }

  async function retryJob(job) {
    try {
      const res = await retryCloudDeleteJob(job.id);
      jobs.set(res.job.bucket, res.job);
      paint();
      schedulePoll();
    } catch (err) {
      if (err.status !== 401) toast.error(err.message);
    }
  }

  async function create() {
    createBtn.disabled = true;
    const res = await cloudBucketDialog();
    createBtn.disabled = false;
    if (res.error) {
      if (res.error.status !== 401) toast.error(res.error.message);
      return;
    }
    if (!res.created) return;
    toast.show(`Cloud bucket ${res.created.fullName} created.`);
    load({ quiet: true });
  }
  createBtn.addEventListener("click", create);

  async function changeQuota(b) {
    const res = await bucketQuotaDialog({ bucket: b.name, current: b.quotaTiB, options: cloudBucketOptions, save: setCloudBucketQuota, product: b.product });
    if (res.error) { if (res.error.status !== 401) toast.error(res.error.message); return; }
    if (!res.saved) return;
    if (res.saved.warning) toast.show(res.saved.warning, { timeout: 9000 });
    else toast.show(res.saved.changed ? `Quota of ${b.name} is now ${formatTiB(res.saved.quotaTiB)}.` : "Nothing to change.");
    load({ quiet: true });
  }

  async function changeBudget(b) {
    const res = await budgetDialog({ bucket: b.name, current: b.budgetNumber, options: cloudBucketOptions, save: setCloudBucketBudget });
    if (res.error) { if (res.error.status !== 401) toast.error(res.error.message); return; }
    if (!res.saved) return;
    if (res.saved.warning) toast.show(res.saved.warning, { timeout: 9000 });
    else toast.show(res.saved.changed ? `Budget number of ${b.name} saved.` : "Nothing to change.");
    load({ quiet: true });
  }

  // Every delete asks for the typed name (owner 2026-11-25). The first request only asks the
  // server what the bucket holds (a live listing plus AWS's daily counts), so the dialog can say.
  async function remove(b) {
    busy.add(b.name);
    paint();
    let found = null;
    try {
      await deleteCloudBucket(b.name);
    } catch (err) {
      if (err.status === 409 && err.body?.requiresConfirmation) found = err.body;
      else if (err.status === 409 && err.body?.job) {
        jobs.set(err.body.job.bucket, err.body.job);
        schedulePoll();
      } else if (err.status !== 401) toast.error(err.message);
    }
    busy.delete(b.name);
    paint();
    if (!found) return;
    const ok = await deleteBucketDialog({
      name: b.name, objects: found.objects || 0, bytes: found.bytes, more: !!found.more,
      storageClass: found.storageClass || b.storageClass, cloud: true, countsMayLag: !!found.countsMayLag,
    });
    if (!ok) return;
    busy.add(b.name);
    paint();
    try {
      const res = await deleteCloudBucket(b.name, b.name);
      if (res?.job) {
        jobs.set(res.job.bucket, res.job);
        toast.show(`Deleting ${b.name} in the background.`);
        busy.delete(b.name);
        paint();
        schedulePoll();
        return;
      }
      toast.show(`Cloud bucket ${b.name} deleted.`);
    } catch (err) {
      if (err.status === 409 && err.body?.job) {
        jobs.set(err.body.job.bucket, err.body.job);
        schedulePoll();
      } else if (err.status !== 401) toast.error(err.message);
    }
    busy.delete(b.name);
    load({ quiet: true });
  }

  load();
  return card;
}
