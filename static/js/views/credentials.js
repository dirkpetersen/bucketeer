import { h } from "../dom.js";
import { icon } from "../icons.js";
import { getAccount, getCredentials, revealSecret } from "../api.js";
import { quotaText } from "../format.js";
import { copyText } from "../clipboard.js";
import { toast } from "../components/toast.js";
import { configSnippet, credentialsSnippet, testCommand } from "../awsconfig.js";

// The Credentials card: account status first, then (account ready) the access key,
// the masked secret and the ready-to-paste ~/.aws snippets. The secret exists in
// the browser only between "reveal"/"copy" and the field's re-mask (60 s), and is
// cleared again when the key selection changes or the card leaves the page.
const SECRET_TTL = 60;

function copyButton(text, label) {
  const btn = h("button", { class: "btn btn-ghost btn-icon", type: "button", "aria-label": label }, icon("copy"));
  let timer = 0;
  btn.addEventListener("click", async () => {
    if (!(await copyText(text))) return toast.error("Could not copy. Select the text and copy it manually.");
    btn.replaceChildren(icon("check"));
    toast.show("Copied");
    clearTimeout(timer);
    timer = setTimeout(() => btn.replaceChildren(icon("copy")), 1800);
  });
  return btn;
}

function keysPanel(data) {
  const keys = data.keys.map((k) => k.accessKey);
  if (!keys.length) {
    return h("p", { class: "acct-sub", text: "You have no access keys yet. Contact your administrator." });
  }
  let selected = keys[0];
  let secret = ""; // only while revealed

  const field = h("secret-field", { label: "Secret access key", ttl: String(SECRET_TTL) });
  // The fetcher keeps nothing: the field holds the secret while revealed, and
  // `secret` below mirrors it only between sf-reveal and sf-mask.
  field.fetcher = async () => {
    const key = selected;
    const r = await revealSecret(key);
    return selected === key ? r.secretKey : ""; // the selection changed while waiting
  };

  const panel = h("code-panel");
  const paint = () => {
    const cfg = configSnippet(data);
    const cred = credentialsSnippet(data, selected, secret);
    panel.tabs = [
      cfg
        ? { label: "~/.aws/config", filename: "config", content: cfg }
        : { label: "~/.aws/config", filename: "config", content: "# The S3 endpoint is not configured yet.\n",
            locked: "Ask an administrator to set the S3 endpoint." },
      cred
        ? { label: "~/.aws/credentials", filename: "credentials", content: cred,
            ...(secret ? {} : { locked: "Reveal the secret to copy." }) }
        : { label: "~/.aws/credentials", filename: "credentials", content: "# This key cannot be shown as a file.\n",
            locked: "This key cannot be written to a credentials file." },
    ];
  };
  field.addEventListener("sf-reveal", () => { secret = field.value; paint(); });
  field.addEventListener("sf-mask", () => { secret = ""; paint(); });

  let keyEl;
  if (keys.length > 1) {
    const sel = h("select", { class: "input", id: "cred-key", "aria-label": "Access key" },
      keys.map((k) => h("option", { value: k, text: k })));
    sel.addEventListener("change", () => { field.clear(); selected = sel.value; secret = ""; paint(); });
    keyEl = h("div", { class: "field" }, h("label", { for: "cred-key", text: "Access key" }), sel);
  } else {
    keyEl = h("div", { class: "field" }, h("span", { class: "label", text: "Access key" }),
      h("div", { class: "cred-key" }, h("code", { text: selected }), copyButton(selected, "Copy access key")));
  }

  paint();
  panel.hint = "Merge these into your existing files; do not overwrite them. Downloads are named config and credentials.";
  const cmd = testCommand(data.profile);
  return h("div", { class: "creds" },
    !data.endpointUrl && h("p", { class: "cred-warn", role: "status",
      text: "The S3 endpoint is not configured. Ask an administrator to set S3_ENDPOINT_URL; the config file is unavailable until then." }),
    keyEl, field, panel,
    h("div", { class: "cred-test" }, h("span", { class: "label", text: "Test it" }),
      h("div", { class: "cred-cmd" }, h("code", { text: cmd }), copyButton(cmd, "Copy test command"))));
}

// Status states from /api/account: ready / suspended / creation_failed / unavailable.
function accountStatus() {
  const body = h("div", { class: "acct", "aria-live": "polite" });
  const chip = h("span", { class: "chip" });
  const paint = (cls, label, ...content) => {
    chip.className = `chip ${cls}`;
    chip.replaceChildren(h("span", { class: "dot" }), label);
    body.replaceChildren(...content);
  };
  const loadFailed = (err) => {
    if (err.name === "ApiError" && err.status === 401) return;
    paint("chip-warn", "Unavailable",
      h("p", { class: "acct-line", text: err.message || "Your keys could not be loaded." }),
      h("button", { class: "btn", type: "button", on: { click: refresh } }, icon("refresh"), "Try again"));
  };
  const refresh = async () => {
    paint("", "Setting up", h("div", { class: "acct-line" }, h("span", { class: "spinner", "aria-hidden": "true" }),
      h("span", { text: "Preparing your S3 account. This takes a moment the first time." })));
    let a;
    try {
      a = await getAccount();
    } catch (err) {
      if (err.name === "ApiError" && err.status === 401) return;
      a = { status: "unavailable", message: err.message };
    }
    if (a.status === "ready") {
      let data;
      try {
        data = await getCredentials();
      } catch (err) {
        return loadFailed(err);
      }
      paint("chip-ok", "Account ready",
        h("p", { class: "acct-line" }, "Signed in to S3 as ", h("code", { class: "acct-uid", text: a.uid }),
          h("span", { class: "acct-sub", text: `Storage limit: ${quotaText(a.quota)}` })),
        keysPanel(data));
    } else if (a.status === "suspended") {
      paint("chip-warn", "Suspended",
        h("p", { class: "acct-line", text: "Your S3 account is suspended. Contact your administrator to restore access." }));
    } else if (a.status === "creation_failed") {
      paint("chip-warn", "Needs an administrator",
        h("p", { class: "acct-line", text: a.message || "Your S3 account couldn't be created, contact your administrator." }));
    } else {
      paint("chip-warn", "Unavailable",
        h("p", { class: "acct-line", text: a.message || "The storage service is temporarily unavailable. Try again shortly." }),
        h("button", { class: "btn", type: "button", on: { click: refresh } }, icon("refresh"), "Try again"));
    }
  };
  refresh();
  return { chip, body };
}

export function credentialsCard() {
  const { chip, body } = accountStatus();
  return h("section", { class: "card panel panel-wide", "aria-labelledby": "h-Credentials" },
    h("div", { class: "panel-head" }, h("h2", { id: "h-Credentials", text: "Credentials" }), chip),
    h("p", { class: "panel-note", text: "Your S3 access key, and ready-to-paste AWS config and credentials files." }),
    body,
    h("p", { class: "adv-only", text: "Advanced: scoped keys and subusers will appear here." }));
}
