import { append, h } from "../dom.js";
import { icon } from "../icons.js";
import { copyText } from "../clipboard.js";
import { createCloudKey, deleteCloudKey, getCloudCredentials, rotateCloudKey } from "../api.js";
import { cloudAvailable, getLocation, subscribe as onLocation } from "../location.js";
import { isAdvanced, subscribe as onAdvanced } from "../advanced.js";
import { cloudConfigSnippet, credentialsSnippet, SECRET_PLACEHOLDER, testCommand } from "../awsconfig.js";
import { ageText, rotateSoonChip } from "../keyage.js";
import { toast } from "../components/toast.js";
import { confirmDialog } from "../components/confirm-dialog.js";

// The "Cloud (AWS)" section of the Credentials card (P6-9). Shown while cloud is available and
// either the effective location is Cloud or the person already has a cloud account.
// AWS hands out a secret only once, when a key is created, and Bucketeer stores none: the
// secret lives in this closure only, from the create/rotate answer until the person clicks
// "Done" (or the section is rebuilt). It is never put in an attribute, storage or the console.
const ONCE = "<the secret shown once when you created this key>";

function copyBtn(text, ariaLabel) {
  const btn = h("button", { class: "btn btn-ghost btn-icon", type: "button", "aria-label": ariaLabel }, icon("copy"));
  btn.addEventListener("click", async () => {
    if (!(await copyText(text))) return toast.error("Could not copy. Select the text and copy it manually.");
    btn.replaceChildren(icon("check"));
    toast.show("Copied");
    setTimeout(() => btn.replaceChildren(icon("copy")), 1800);
  });
  return btn;
}

const fmtDate = (iso) => {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
};

export function cloudSection() {
  const host = h("section", { class: "ck", "aria-labelledby": "ck-title", hidden: true });
  let data = null; // last GET answer (never holds a secret)
  let fresh = null; // {accessKey, secretKey} right after create/rotate, until "Done"
  let busy = false;
  let loading = false;

  // Only while the storage location is Cloud (AWS) and Advanced is on, like the on-premises keys.
  const shouldShow = () => cloudAvailable() && getLocation() === "cloud" && isAdvanced();

  const act = async (btn, fn) => {
    if (busy) return;
    busy = true;
    btn.disabled = true;
    try {
      await fn();
    } catch (err) {
      if (!(err.name === "ApiError" && err.status === 401)) toast.error(err.message || "That did not work. Try again.");
    } finally {
      busy = false;
      if (btn.isConnected) btn.disabled = false;
    }
  };

  const secretBox = () => {
    const { accessKey, secretKey } = fresh;
    const done = h("button", { class: "btn btn-primary", type: "button" }, icon("check"), "Done, I saved it");
    done.addEventListener("click", async () => {
      const ok = await confirmDialog({
        title: "Saved the secret?",
        message: "After this the secret is gone for good. If you lose it, create a new key.",
        confirmLabel: "I saved it",
      });
      if (!ok) return;
      fresh = null;
      paint();
    });
    return h("div", { class: "ck-once", role: "status" },
      h("p", { class: "ck-once-head" }, icon("alert"),
        h("span", {}, h("b", { text: "Copy the secret now. " }), "You won't see it again: AWS shows a secret only once, and Bucketeer does not keep it.")),
      h("p", { class: "set-hint", text: "A new key can take up to a minute before AWS accepts it. If a command is refused right away, wait a moment and try again." }),
      h("div", { class: "field" }, h("span", { class: "label", text: "Access key" }),
        h("div", { class: "cred-key" }, h("code", { text: accessKey }), copyBtn(accessKey, "Copy access key"))),
      h("div", { class: "field" }, h("span", { class: "label", text: "Secret access key" }),
        h("div", { class: "cred-key ck-secret" }, h("code", { text: secretKey }), copyBtn(secretKey, "Copy secret access key"))),
      h("div", { class: "cred-actions" }, done));
  };

  const snippets = (selected) => {
    const panel = h("code-panel");
    const secret = fresh && fresh.accessKey === selected ? fresh.secretKey : "";
    const cfg = cloudConfigSnippet(data);
    let cred = credentialsSnippet(data, selected, secret);
    if (cred && !secret) cred = cred.replace(SECRET_PLACEHOLDER, ONCE);
    panel.tabs = [
      { label: "~/.aws/config", filename: "config", content: cfg || "# This profile cannot be shown as a file.\n" },
      cred ? { label: "~/.aws/credentials", filename: "credentials", content: cred }
        : { label: "~/.aws/credentials", filename: "credentials", content: "# This key cannot be shown as a file.\n",
            locked: "This key cannot be written to a credentials file." },
    ];
    panel.hint = "Merge these into your existing files; do not overwrite them.";
    return panel;
  };

  const keyRow = (k, count) => {
    const rotate = h("button", { class: "btn", type: "button", disabled: count >= data.maxKeys,
      title: count >= data.maxKeys ? "AWS allows two keys. Delete the one you don't use, then rotate." : null }, icon("refresh"), "Rotate");
    const del = h("button", { class: "btn btn-danger", type: "button", disabled: count < 2,
      title: count < 2 ? "This is your only cloud key. Rotate it instead." : null }, icon("trash"), "Delete");
    rotate.addEventListener("click", () => act(rotate, async () => {
      const ok = await confirmDialog({
        title: "Rotate this cloud key?",
        message: `A new key is created first, then ${k.accessKey} is deleted. Anything still using it stops working. The new secret is shown once.`,
        confirmLabel: "Rotate key", danger: true,
      });
      if (!ok) return;
      const r = await rotateCloudKey(k.accessKey);
      fresh = { accessKey: r.accessKey, secretKey: r.secretKey };
      if (r.oldKeyRemoved) toast.show("Key rotated. Copy the new secret now.");
      else toast.error(r.message || "The new key was created, but the old one could not be removed.", { timeout: 12000 });
      await load();
    }));
    del.addEventListener("click", () => act(del, async () => {
      const ok = await confirmDialog({
        title: "Delete this cloud key?",
        message: `${k.accessKey} stops working immediately. This cannot be undone.`,
        confirmLabel: "Delete key", danger: true,
      });
      if (!ok) return;
      await deleteCloudKey(k.accessKey);
      if (fresh?.accessKey === k.accessKey) fresh = null;
      toast.show("Key deleted.");
      await load();
    }));
    const active = k.status === "Active";
    return h("li", { class: "ck-key" },
      h("div", { class: "ck-key-id" },
        h("code", { text: k.accessKey }),
        h("span", { class: `chip ${active ? "chip-ok" : "chip-warn"}` }, h("span", { class: "dot" }), active ? "Active" : "Inactive"),
        k.createdAt && h("span", { class: "acct-sub", text: `Created ${fmtDate(k.createdAt)}${ageText(k) ? `, ${ageText(k)}` : ""}` }),
        rotateSoonChip(k, data.maxKeyAgeDays)),
      h("div", { class: "cred-actions" }, rotate, del));
  };

  const paint = () => {
    host.hidden = !shouldShow();
    if (host.hidden || !data) return;
    const keys = data.keys;
    const add = h("button", { class: "btn", type: "button", disabled: keys.length >= data.maxKeys,
      title: keys.length >= data.maxKeys ? "AWS allows at most two keys per user." : null }, icon("plus"), keys.length ? "Create key" : "Create my cloud key");
    add.addEventListener("click", () => act(add, async () => {
      if (keys.length) {
        const ok = await confirmDialog({
          title: "Create a second cloud key?",
          message: "AWS allows two keys per user. The secret of the new key is shown once, right after it is made.",
          confirmLabel: "Create key",
        });
        if (!ok) return;
      }
      const r = await createCloudKey();
      fresh = { accessKey: r.accessKey, secretKey: r.secretKey };
      toast.show("Cloud key created. Copy the secret now.");
      await load();
    }));
    const selected = fresh?.accessKey || keys[0]?.accessKey;
    const cmd = testCommand(data.profile);
    // append() skips the null/undefined of the optional parts; replaceChildren would print them.
    host.replaceChildren();
    append(host, [
      h("div", { class: "ck-head" },
        h("span", { class: "ck-badge", "aria-hidden": "true" }, icon("cloud")),
        h("div", {},
          h("h3", { id: "ck-title", class: "sp-title", text: "Cloud (AWS)" }),
          h("p", { class: "acct-sub" }, "Keys for your cloud buckets, used as the AWS profile ",
            h("code", { text: data.profile }), ` in ${data.region}.`))),
      fresh && secretBox(),
      keys.length
        ? h("ul", { class: "ck-keys", "aria-label": "Cloud access keys" }, keys.map((k) => keyRow(k, keys.length)))
        : h("p", { class: "acct-sub", text: "You have no cloud key yet. The first one also sets up your AWS user." }),
      h("div", { class: "cred-actions" }, add),
      selected && snippets(selected),
      selected && h("div", { class: "cred-test" }, h("span", { class: "label", text: "Test it" }),
        h("div", { class: "cred-cmd" }, h("code", { text: cmd }), copyBtn(cmd, "Copy test command")))]);
  };

  const load = async () => {
    if (loading || gone()) return;
    loading = true;
    try {
      data = await getCloudCredentials();
      if (fresh && !data.keys.some((k) => k.accessKey === fresh.accessKey)) fresh = null;
    } catch (err) {
      data = null;
      if (gone()) return teardown();
      if (!(err.name === "ApiError" && (err.status === 401 || err.status === 503))) {
        host.hidden = !shouldShow();
        host.replaceChildren(h("p", { class: "cred-warn", role: "status", text: err.message || "Your cloud keys could not be loaded." }));
        return;
      }
    } finally {
      loading = false;
    }
    if (gone()) return teardown(); // replaced while the answer was on its way
    paint();
  };

  // The Credentials card rebuilds its sections on every key reload. A replaced copy must stop
  // reacting: `seen` marks that it was in the document once, so being detached later means gone.
  let seen = false;
  let unsubLoc = () => {};
  let unsubAdv = () => {};
  const gone = () => {
    if (host.isConnected) { seen = true; return false; }
    return seen;
  };
  const teardown = () => { fresh = null; data = null; unsubLoc(); unsubAdv(); };
  const onChange = () => {
    if (gone()) return teardown(); // the card was rebuilt
    if (!host.isConnected) return; // not placed yet: never load or paint from here
    if (shouldShow() && !data) load();
    else paint();
  };
  unsubLoc = onLocation(onChange);
  unsubAdv = onAdvanced(onChange);
  requestAnimationFrame(() => { if (host.isConnected) seen = true; });
  if (shouldShow()) load();
  return host;
}
