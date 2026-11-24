// Header switch "Cloud | On Premises" (Phase 6). A radiogroup with a roving tabindex:
// Tab reaches the checked option, arrow keys / Home / End move and select, as the
// ARIA radio pattern asks. Shown only while Advanced is on and cloud is available;
// the caller keeps it in the header and this module toggles `hidden`.
import { h } from "../dom.js";
import { icon } from "../icons.js";
import { isAdvanced, subscribe as onAdvanced } from "../advanced.js";
import { cloudAvailable, getLocation, setLocation, subscribe } from "../location.js";
import { toast } from "./toast.js";

const OPTIONS = [["cloud", "cloud", "Cloud"], ["onprem", "server", "On Premises"]];

export function locationSwitch() {
  const buttons = OPTIONS.map(([value, ic, label]) => h("button", {
    class: "loc-opt", type: "button", role: "radio", "data-value": value, "aria-label": label,
    title: value === "cloud" ? "Work with your buckets in the cloud (AWS)" : "Work with your buckets on premises",
  }, icon(ic), h("span", { class: "loc-text", text: label })));
  const group = h("div", { class: "loc-switch", role: "radiogroup", "aria-label": "Storage location" },
    h("span", { class: "loc-thumb", "aria-hidden": "true" }), ...buttons);

  const paint = () => {
    const loc = getLocation();
    group.hidden = !(isAdvanced() && cloudAvailable());
    group.dataset.value = loc;
    for (const b of buttons) {
      const on = b.dataset.value === loc;
      b.setAttribute("aria-checked", String(on));
      b.tabIndex = on ? 0 : -1;
    }
  };

  const choose = (value, focus = false) => {
    if (focus) buttons.find((b) => b.dataset.value === value)?.focus();
    if (value === getLocation()) return;
    setLocation(value).catch((err) => toast.error(err.message || "Could not save the location."));
  };

  for (const b of buttons) b.addEventListener("click", () => choose(b.dataset.value));
  group.addEventListener("keydown", (e) => {
    const i = buttons.indexOf(document.activeElement);
    if (i < 0) return;
    let j = null;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") j = (i + 1) % buttons.length;
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") j = (i - 1 + buttons.length) % buttons.length;
    else if (e.key === "Home") j = 0;
    else if (e.key === "End") j = buttons.length - 1;
    else if (e.key === " " || e.key === "Enter") { e.preventDefault(); choose(buttons[i].dataset.value); return; }
    if (j === null) return;
    e.preventDefault();
    choose(buttons[j].dataset.value, true);
  });

  // Both stores only change after the header is in the page; a replaced header unsubscribes.
  const update = () => { if (!group.isConnected) { offLoc(); offAdv(); return; } paint(); };
  const offLoc = subscribe(update);
  const offAdv = onAdvanced(update);
  paint();
  return group;
}
