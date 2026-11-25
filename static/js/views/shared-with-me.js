import { h } from "../dom.js";
import { icon } from "../icons.js";
import { sharedWithMe } from "../api.js";
import { copyText } from "../clipboard.js";
import { toast } from "../components/toast.js";
import { getLocation, subscribe as onLocation } from "../location.js";

// "Shared with you": buckets colleagues shared with the signed-in user. Read-only. Hidden
// when there are none (or the list cannot be loaded): it is a convenience, not a gate.
const ACCESS = { read: "Read-only", readwrite: "Read and write" };

// Shares are an on-premises feature for now: cloud buckets have no sharing yet (PLAN.md),
// so the card is shown only while the effective location is on premises.
export function sharedWithMeCard() {
  const slot = h("div", { class: "swm-slot" });
  let shown = null;
  const show = (loc) => {
    if (loc === shown) return;
    shown = loc;
    if (loc === "cloud") slot.replaceChildren();
    else slot.replaceChildren(onpremSharedCard());
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
        const copy = h("button", { class: "btn btn-sm", type: "button", "aria-label": `Copy the command to list ${s.bucket}` }, icon("copy"), h("span", { class: "bk-del-l", text: "Copy command" }));
        copy.addEventListener("click", async () => {
          const ok = await copyText(cmd);
          toast[ok ? "show" : "error"](ok ? "Command copied." : "Could not copy. Select the command and copy it by hand.");
        });
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
