import { h } from "../dom.js";
import { icon } from "../icons.js";
import { formatBytes, formatCount } from "../format.js";
import { confirmDialog, nameSpan } from "./confirm-dialog.js";

// Deleting a bucket (owner, 2026-11-25): every delete asks for the typed bucket name, also
// for an empty bucket; a bucket with content shows how many objects and how much space go.
// Shared by the on-premises card (views/buckets.js) and the cloud card (views/cloud-buckets.js).
//   const ok = await deleteBucketDialog({ name, objects, bytes, more, objectLock, storageClass, cloud, countsMayLag });

function what(objects, bytes, more) {
  const n = `${formatCount(objects)}${more ? " or more" : ""} ${objects === 1 && !more ? "object" : "objects"}`;
  const size = bytes == null ? "size not measured yet" : formatBytes(bytes);
  return `${n} (${size})`;
}

export function deleteBucketDialog({ name, objects = 0, bytes = 0, more = false, objectLock = false, storageClass = null, cloud = false, countsMayLag = false }) {
  const held = objects > 0 || more;
  const lines = [];
  if (held) {
    lines.push(h("span", { class: "dbd-data" }, icon("alert", "icon"),
      h("span", {}, "This bucket holds ", h("strong", { class: "dbd-count", text: what(objects, bytes, more) }),
        ". They will be deleted permanently, with every old version. This cannot be undone.")));
    if (countsMayLag) {
      lines.push(h("span", { class: "dbd-note", text: "AWS counts objects and space once a day, so these numbers may lag by a day." }));
    }
    if (objectLock) {
      lines.push(h("span", { class: "dbd-note", text: "Files under object lock or legal hold are not removed: the deletion then stops until their lock ends." }));
    }
    if (storageClass === "deep_archive") {
      lines.push(h("span", { class: "dbd-note dbd-fee", text: "Deep Archive: objects stored for less than 180 days are still charged for the full 180 days (early deletion fee)." }));
    }
  } else {
    lines.push(h("span", { class: "dbd-note", text: `This bucket is empty. Its name becomes available to everyone${cloud ? " on AWS" : ""} again. This cannot be undone.` }));
  }
  return confirmDialog({
    title: held ? ["Delete ", nameSpan(name), " and its data?"] : ["Delete ", nameSpan(name), "?"],
    message: h("span", { class: "dbd" }, ...lines),
    confirmLabel: held ? "Delete bucket and data" : "Delete bucket",
    danger: true,
    requireText: name,
  });
}

// The delete job of a bucket: progress while it runs, the reason and Retry when it stopped.
export function deleteJobBlock(job, onRetry) {
  if (job.state === "queued" || job.state === "running") {
    const done = job.deletedObjects || 0;
    // Versions and delete markers are counted, so the number can exceed the object count.
    const text = job.state === "queued" ? "Waiting to start deleting…"
      : `Deleting… ${formatCount(done)} object ${done === 1 ? "version" : "versions"} removed`;
    return h("div", { class: "bk-job", role: "status" },
      h("span", { class: "spinner", "aria-hidden": "true" }), h("span", { class: "bk-job-t", text }));
  }
  const blocked = job.state === "blocked";
  return h("div", { class: "bk-job bk-job-stopped", "data-kind": job.state, role: "status" },
    icon(blocked ? "lock" : "alert", "icon"),
    h("div", { class: "bk-job-body" },
      h("b", { text: blocked ? "Deletion is blocked" : "Deletion paused" }),
      h("span", { class: "bk-job-msg", text: job.message || "The bucket could not be deleted yet." })),
    h("button", { class: "btn btn-sm", type: "button", on: { click: () => onRetry(job) } }, icon("refresh"), "Retry"));
}
