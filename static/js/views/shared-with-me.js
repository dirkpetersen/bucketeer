import { h } from "../dom.js";
import { icon } from "../icons.js";
import { cloudSharedWithMe, sharedWithMe } from "../api.js";
import { copyText } from "../clipboard.js";
import { toast } from "../components/toast.js";
import { getLocation, subscribe as onLocation } from "../location.js";

// "Shared with you": buckets colleagues shared with the signed-in user. Read-only. Hidden
// when there are none (or the list cannot be loaded): it is a convenience, not a gate.
const ACCESS = { read: "Read-only", readwrite: "Read and write" };

// The card follows the effective location: on premises it lists shared on-premises buckets,
// in Cloud the shared cloud (AWS) buckets (full AWS name, used with the cloud profile).
export function sharedWithMeCard() {
  const slot = h("div", { class: "swm-slot" });
  let shown = null;
  const show = (loc) => {
    if (loc === shown) return;
    shown = loc;
    slot.replaceChildren(loc === "cloud" ? cloudSharedCard() : onpremSharedCard());
  };
  show(getLocation());
  const unsub = onLocation((loc) => {
    if (!slot.isConnected && slot.dataset.mounted === "true") { unsub(); return; } // page replaced
    if (slot.isConnected) slot.dataset.mounted = "true";
    show(loc);
  });
  return slot;
}

function onpremSharedCard() {
  const card = h("section", { class: "card panel panel-wide swm", "aria-labelledby": "h-swm", hidden: true });
  sharedWithMe().then(({ shares }) => {
    if (!shares.length) return;
    card.append(
      h("div", { class: "panel-head" }, h("div", { class: "bk-title" }, h("h2", { id: "h-swm", text: "Shared with you" }),
        h("span", { class: "chip", text: String(shares.length) }))),
      h("p", { class: "panel-note" }, "Buckets colleagues gave you access to. Your own access keys work for them. They do not appear in a plain ",
        h("code", { text: "aws s3 ls" }), "; name the bucket instead."),
      h("ul", { class: "bk-list" }, shares.map((s) => {
        const cmd = `aws s3 ls s3://${s.address}/`;
        const copy = copyButton(cmd, `Copy the command to list ${s.bucket}`);
        return h("li", { class: "bk-row swm-row" },
          h("span", { class: "bk-ic", "aria-hidden": "true" }, icon("bucket")),
          h("div", { class: "bk-main" }, h("b", { class: "bk-name", text: s.bucket }),
            h("div", { class: "bk-meta" }, `From ${s.owner}`, h("span", { class: "chip" }, ACCESS[s.access] ?? s.access))),
          h("div", { class: "swm-cmd" }, h("code", { text: cmd }), copy));
      })));
    card.hidden = false;
  }).catch(() => {});
  return card;
}

function copyButton(cmd, label) {
  const copy = h("button", { class: "btn btn-sm", type: "button", "aria-label": label }, icon("copy"), h("span", { class: "bk-del-l", text: "Copy command" }));
  copy.addEventListener("click", async () => {
    const ok = await copyText(cmd);
    toast[ok ? "show" : "error"](ok ? "Command copied." : "Could not copy. Select the command and copy it by hand.");
  });
  return copy;
}

function cloudSharedCard() {
  const card = h("section", { class: "card panel panel-wide swm", "aria-labelledby": "h-swm", hidden: true });
  cloudSharedWithMe().then(({ shares, profile }) => {
    if (!shares.length) return;
    card.append(
      h("div", { class: "panel-head" }, h("div", { class: "bk-title" }, h("h2", { id: "h-swm", text: "Shared with you" }),
        h("span", { class: "chip", text: String(shares.length) }))),
      h("p", { class: "panel-note" }, "Cloud buckets colleagues gave you access to. Your cloud profile ",
        h("code", { text: profile }), " works for them; set it up with Set up this computer. Use the full AWS bucket name."),
      h("ul", { class: "bk-list" }, shares.map((s) => {
        const cmd = `aws --profile ${profile} s3 ls s3://${s.bucket}/`;
        return h("li", { class: "bk-row swm-row" },
          h("span", { class: "bk-ic", "aria-hidden": "true" }, icon("cloud")),
          h("div", { class: "bk-main" }, h("b", { class: "bk-name", text: s.bucket }),
            h("div", { class: "bk-meta" }, `From ${s.owner}`, h("span", { class: "chip" }, ACCESS[s.access] ?? s.access))),
          h("div", { class: "swm-cmd" }, h("code", { text: cmd }), copyButton(cmd, `Copy the command to list ${s.bucket}`)));
      })));
    card.hidden = false;
  }).catch(() => {});
  return card;
}
