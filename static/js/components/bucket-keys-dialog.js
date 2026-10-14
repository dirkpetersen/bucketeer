import { h } from "../dom.js";
import { icon } from "../icons.js";
import { copyText } from "../clipboard.js";
import { createBucketKey, getCredentials, listBucketKeys, revealBucketKey, revokeBucketKey } from "../api.js";
import { configSnippet, credentialsSnippet } from "../awsconfig.js";
import { toast } from "./toast.js";
import { confirmDialog, nameSpan } from "./confirm-dialog.js";

// bucketKeysDialog({ bucket }) -> Promise<{ error? } | { changed: boolean }>
// The keys of one bucket: list, create, reveal (secret fetched on demand, audited by the
// server, in memory for 60 s), ready-to-paste snippets (profile bucketeer-<label>) and
// revoke. Everything here is feedback only; the server enforces ownership and limits.
let seq = 0;
const SECRET_TTL = 60;
const LABEL = /^[a-z0-9]([a-z0-9-]{0,18}[a-z0-9])?$/;
const ACCESS = { read: "Read-only", readwrite: "Read and write" };
const STATUS = {
  active: ["chip-ok", "Active"],
  suspended: ["chip-warn", "Suspended"],
  pending: ["chip-warn", "Needs clean-up"],
  failed: ["chip-warn", "Needs clean-up"],
};

export const checkLabel = (v) => {
  if (!v) return "Enter a label.";
  if (v.length > 20) return "Use at most 20 characters.";
  if (!LABEL.test(v)) return "Use lowercase letters, digits and hyphens; start and end with a letter or digit.";
  return "";
};

function copyBtn(text, label) {
  const btn = h("button", { class: "btn btn-ghost btn-icon", type: "button", "aria-label": label }, icon("copy"));
  btn.addEventListener("click", async () => {
    if (!(await copyText(text))) return toast.error("Could not copy. Select the text and copy it manually.");
    btn.replaceChildren(icon("check"));
    toast.show("Copied");
    setTimeout(() => btn.replaceChildren(icon("copy")), 1800);
  });
  return btn;
}

export async function bucketKeysDialog({ bucket }) {
  const id = `bk${++seq}`;
  let keys;
  let cred = null;
  try {
    keys = (await listBucketKeys(bucket)).keys;
  } catch (err) {
    return { error: err };
  }
  try {
    cred = await getCredentials(); // region, endpoint and addressing style for the snippets
  } catch { /* snippets then say so */ }

  let open = null; // id of the key whose details are shown
  let changed = false;
  let busy = false;
  const list = h("ul", { class: "bkd-list" });
  const live = h("div", { "aria-live": "polite" }, list);

  function details(k) {
    const profile = `bucketeer-${k.label}`;
    const field = h("secret-field", { label: "Secret access key", ttl: String(SECRET_TTL) });
    field.fetcher = async () => (await revealBucketKey(k.id)).secretKey;
    const panel = h("code-panel");
    let secret = "";
    const paint = () => {
      const cfg = cred ? configSnippet({ ...cred, profile }) : null;
      const cr = credentialsSnippet({ profile }, k.accessKey, secret);
      panel.tabs = [
        cfg ? { label: "~/.aws/config", filename: "config", content: cfg }
          : { label: "~/.aws/config", filename: "config", content: "# The S3 endpoint is not configured yet.\n", locked: "Ask an administrator to set the S3 endpoint." },
        cr ? { label: "~/.aws/credentials", filename: "credentials", content: cr, ...(secret ? {} : { locked: "Reveal the secret to copy." }) }
          : { label: "~/.aws/credentials", filename: "credentials", content: "# This key cannot be shown as a file.\n", locked: "This key cannot be written to a credentials file." },
      ];
    };
    field.addEventListener("sf-reveal", () => { secret = field.value; paint(); });
    field.addEventListener("sf-mask", () => { secret = ""; paint(); });
    paint();
    panel.hint = "Merge these into your existing files; do not overwrite them.";
    const cmd = `aws --profile ${profile} s3 ls s3://${bucket}/`;
    return h("div", { class: "bkd-detail" },
      k.status !== "active"
        ? h("p", { class: "cred-warn", role: "status", text: k.status === "suspended"
          ? "This key is suspended by an administrator, so its secret cannot be shown."
          : "This key was not set up completely. Revoke it and create a new one." })
        : [
          h("div", { class: "field" }, h("span", { class: "label", text: "Access key" }),
            h("div", { class: "cred-key" }, h("code", { text: k.accessKey }), copyBtn(k.accessKey, "Copy access key"))),
          field, panel,
          h("div", { class: "cred-test" }, h("span", { class: "label", text: "Test it" }),
            h("div", { class: "cred-cmd" }, h("code", { text: cmd }), copyBtn(cmd, "Copy test command")))]);
  }

  function row(k) {
    const [cls, text] = STATUS[k.status] ?? STATUS.failed;
    const shown = open === k.id;
    const created = new Date(k.createdAt);
    const toggle = h("button", { class: "btn btn-sm", type: "button", "aria-expanded": String(shown), "aria-controls": `${id}-d${k.id}`,
      on: { click: () => { open = shown ? null : k.id; paint(); } } },
    icon(shown ? "up" : "down"), shown ? "Hide" : "Use");
    const revoke = h("button", { class: "btn btn-sm btn-quiet-danger", type: "button", "aria-label": `Revoke key ${k.label}`,
      on: { click: () => onRevoke(k) } }, icon("trash"), h("span", { class: "bk-del-l", text: "Revoke" }));
    return h("li", { class: "bkd-key", "data-open": String(shown) },
      h("div", { class: "bkd-head" },
        h("div", { class: "bkd-id" },
          h("b", { class: "bkd-label", text: k.label }),
          h("span", { class: "bkd-sub" },
            h("span", { class: "chip" }, ACCESS[k.access] ?? k.access),
            h("span", { class: `chip ${cls}` }, h("span", { class: "dot" }), text),
            !Number.isNaN(created.getTime()) && h("time", { datetime: k.createdAt, text: `Created ${created.toLocaleDateString(undefined, { dateStyle: "medium" })}` }))),
        h("div", { class: "bk-acts" }, toggle, revoke)),
      shown && h("div", { id: `${id}-d${k.id}` }, details(k)));
  }

  function paint() {
    if (!keys.length) {
      list.replaceChildren(h("li", { class: "bkd-empty" }, icon("key", "icon"),
        h("p", { text: "No keys for this bucket yet. Create one to give a script or a colleague access to just this bucket." })));
      return;
    }
    list.replaceChildren(...keys.map(row));
  }

  async function reload() {
    keys = (await listBucketKeys(bucket)).keys;
    paint();
  }

  async function onRevoke(k) {
    if (busy) return;
    const ok = await confirmDialog({
      title: ["Revoke the key ", nameSpan(k.label), "?"], danger: true, confirmLabel: "Revoke key",
      message: "Anything using this key stops working immediately, and its secret is deleted. This cannot be undone. A label is never reused.",
    });
    if (!ok) return;
    busy = true;
    try {
      await revokeBucketKey(k.id);
      changed = true;
      toast.show(`Key ${k.label} revoked.`);
    } catch (err) {
      if (err.status !== 401) toast.error(err.message, { timeout: 9000 });
    }
    try { await reload(); } catch (err) { if (err.status !== 401) toast.error(err.message); }
    busy = false;
  }

  // -- create form --
  const label = h("input", { class: "input mono-input", id: `${id}-l`, autocomplete: "off", autocapitalize: "off", spellcheck: "false",
    maxlength: "20", placeholder: "e.g. backup-job", "aria-describedby": `${id}-lh ${id}-le` });
  const labelErr = h("p", { class: "set-err", id: `${id}-le`, role: "alert", hidden: true });
  const radio = (value, title, hint, checked) =>
    h("label", { class: "qd-check bkd-opt" },
      h("input", { type: "radio", name: `${id}-a`, value, checked }),
      h("span", {}, h("b", { text: title }), h("small", { text: hint })));
  const access = h("fieldset", { class: "bkd-access" },
    h("legend", { class: "label", text: "Access" }),
    radio("read", "Read-only", "List and download files.", true),
    radio("readwrite", "Read and write", "Also upload, overwrite and delete files.", false));
  const error = h("p", { class: "qd-error", role: "alert", hidden: true });
  const submit = h("button", { class: "btn btn-primary", type: "submit" }, icon("plus"), "Create key");
  const form = h("form", { class: "bkd-form", novalidate: true },
    h("h3", { text: "Create a key" }),
    h("p", { class: "cd-msg", text: "A key is a separate S3 identity that works for this bucket only. It cannot see your other buckets or create data anywhere else; files it writes here count against your quota." }),
    h("div", { class: "field" }, h("label", { for: label.id, text: "Label" }), label,
      h("p", { class: "set-hint", id: `${id}-lh`, text: "Names the key and its AWS profile (bucketeer-<label>). Lowercase letters, digits, hyphens; up to 20." }), labelErr),
    access, error, h("div", { class: "cd-actions" }, submit));
  const showLabelErr = (m) => { labelErr.textContent = m; labelErr.hidden = !m; label.setAttribute("aria-invalid", String(!!m)); };
  label.addEventListener("input", () => { showError(""); showLabelErr(label.value ? checkLabel(label.value) : ""); });
  function showError(m) { error.textContent = m; error.hidden = !m; }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (busy) return;
    const bad = checkLabel(label.value);
    if (bad) { showLabelErr(bad); label.focus(); return; }
    busy = true;
    submit.disabled = true;
    submit.replaceChildren(h("span", { class: "spinner", "aria-hidden": "true" }), "Creating…");
    try {
      const k = await createBucketKey(bucket, label.value, form.querySelector("input[type=radio]:checked").value);
      changed = true;
      label.value = "";
      showLabelErr("");
      showError("");
      open = k.id;
      toast.show(`Key ${k.label} created. Reveal its secret to use it.`);
      await reload();
    } catch (err) {
      if (err.status === 401) { dlg.close(); return; }
      showError(err.message || "Could not create the key.");
      try { await reload(); } catch { /* keep the list as it was */ }
    } finally {
      busy = false;
      submit.disabled = false;
      submit.replaceChildren(icon("plus"), "Create key");
    }
  });

  const close = h("button", { class: "btn", type: "button", text: "Close" });
  const dlg = h("dialog", { class: "cd qd bkd", "aria-labelledby": `${id}-t`, "aria-describedby": `${id}-m` },
    h("div", { class: "bkd-body" },
      h("div", { class: "qd-head" },
        h("h2", { id: `${id}-t` }, "Keys for ", nameSpan(bucket)),
        h("p", { class: "cd-msg", id: `${id}-m`, text: "Give a script, a service or a colleague access to just this bucket, without sharing your own key. Revoke a key at any time." })),
      live, form,
      h("div", { class: "cd-actions" }, close)));
  paint();
  close.addEventListener("click", () => dlg.close());
  dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });

  return new Promise((resolve) => {
    dlg.addEventListener("close", () => { dlg.remove(); resolve({ changed }); });
    document.body.append(dlg);
    dlg.showModal();
    close.focus();
  });
}
