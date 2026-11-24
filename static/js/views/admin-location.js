// Administration > Settings: the "Default location" card (Phase 6). It loads on its
// own (GET /api/admin/settings/location runs a fresh AWS connection check, which can
// take a few seconds) so the other settings never wait for AWS. Cloud can be chosen
// only when cloud is set up in .env and the check passes; the server enforces it too.
import { h } from "../dom.js";
import { icon } from "../icons.js";
import { getLocationSetting, saveLocationSetting } from "../api.js";
import { toast } from "../components/toast.js";
import { confirmDialog } from "../components/confirm-dialog.js";

let seq = 0;
const nid = (p) => `loc-${p}${++seq}`;
const NAMES = { onprem: "On Premises", cloud: "Cloud (AWS)" };

function statusBox(data, checking) {
  if (checking) {
    return h("p", { class: "set-hint loc-check", role: "status" },
      h("span", { class: "spinner", "aria-hidden": "true" }), "Checking the connection to AWS…");
  }
  if (!data.configured) {
    return h("div", { class: "set-warn", role: "status" }, icon("cloud"),
      h("span", { text: "Cloud is not set up. An AWS administrator runs tools/aws-setup once, then the CLOUD_* lines go into .env and Bucketeer is restarted (see the admin guide)." }));
  }
  const c = data.check;
  if (c?.ok) {
    return h("p", { class: "set-ok", role: "status" }, icon("check"),
      `Connected to AWS${c.accountId ? ` account ${c.accountId}` : ""}, region ${c.region}. Buckets are named ${data.bucketPrefix}<user>-<name>.`);
  }
  return h("p", { class: "set-err", role: "alert" },
    `The AWS connection check failed: ${c?.reason || "no answer"}. Cloud can't be chosen until it passes.`);
}

export function locationCard() {
  const hid = nid("h");
  const status = h("span", { class: "chip" });
  const box = h("div", { class: "loc-status" });
  const name = nid("r");
  const radios = {};
  const option = (value, title, sub) => {
    const input = radios[value] = h("input", { type: "radio", name, value, id: nid("o") });
    return h("label", { class: "qd-check set-check", for: input.id }, input,
      h("span", {}, h("b", { text: title }), h("small", { text: sub })));
  };
  const save = h("button", { class: "btn btn-primary", type: "submit", text: "Save", disabled: true });
  const recheck = h("button", { class: "btn", type: "button" }, icon("refresh"), "Check again");
  const form = h("form", { class: "set-form", novalidate: true },
    h("fieldset", { class: "set-fieldset" },
      h("legend", { text: "Where people work by default" }),
      option("onprem", NAMES.onprem, "Buckets and keys on the Ceph cluster."),
      option("cloud", NAMES.cloud, "Buckets and keys in the shared AWS account. Also where backups go.")),
    box,
    h("div", { class: "set-actions" }, recheck, save));
  const el = h("section", { class: "card set-card", "aria-labelledby": hid, "data-section": "location" },
    h("div", { class: "set-head" }, h("h2", { id: hid, text: "Default location" }), status),
    h("p", { class: "set-lead", text: "Where buckets live for everyone. With Advanced mode on, people can switch between Cloud and On Premises themselves; with it off they always work in this default." }),
    form);

  let data = null;
  const picked = () => (radios.cloud.checked ? "cloud" : "onprem");
  const paint = (checking = false) => {
    const d = data ?? { default: "onprem", configured: false, check: null };
    status.replaceChildren(NAMES[d.default]);
    status.className = d.default === "cloud" ? "chip chip-accent" : "chip";
    const usable = d.configured && d.check?.ok;
    radios.cloud.disabled = checking || !usable;
    radios.onprem.disabled = checking;
    box.replaceChildren(statusBox(d, checking));
    save.disabled = checking || picked() === d.default;
    recheck.disabled = checking || !d.configured;
  };
  const setFrom = (d) => {
    data = d;
    radios[d.default].checked = true;
    paint();
  };

  async function load() {
    paint(true);
    try {
      setFrom(await getLocationSetting());
    } catch (err) {
      if (err.status === 401) return;
      box.replaceChildren(h("p", { class: "set-err", role: "alert", text: err.message }));
      recheck.disabled = false;
    }
  }

  for (const r of Object.values(radios)) r.addEventListener("change", () => paint());
  recheck.addEventListener("click", load);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (save.disabled) return;
    const value = picked();
    if (value === "cloud") {
      const ok = await confirmDialog({
        title: "Make Cloud the default?",
        message: "Everyone then works in the cloud by default, Advanced mode or not, and backups go to AWS. People with Advanced mode on can still switch back to On Premises.",
        confirmLabel: "Make Cloud the default",
      });
      if (!ok) return;
    }
    save.disabled = true;
    save.replaceChildren(h("span", { class: "spinner", "aria-hidden": "true" }), "Saving…");
    try {
      setFrom(await saveLocationSetting(value));
      toast.show(`Default location: ${NAMES[value]}.`);
    } catch (err) {
      if (err.status !== 401) toast.error(err.message || "Could not save the default location.");
      paint();
    } finally {
      save.textContent = "Save";
      paint();
    }
  });

  paint(true);
  load();
  return el;
}
