import { h } from "../dom.js";
import { icon } from "../icons.js";
import { copyText } from "../clipboard.js";
import {
  createSubuser, createSubuserKey, deleteSubuser, deleteSubuserKey, listSubusers,
  revealSubuserKey, rotateSubuserKey, updateSubuser,
} from "../api.js";
import { isAdvanced, subscribe } from "../advanced.js";
import { configSnippet, credentialsSnippet } from "../awsconfig.js";
import { toast } from "../components/toast.js";
import { confirmDialog, nameSpan } from "../components/confirm-dialog.js";

// The Subusers section of the Credentials card (Advanced only). A subuser is an extra S3
// identity of the user's own account with one permission across ALL their buckets and up to two
// keys. Everything here is feedback; the server enforces limits, ownership and the full-control
// confirmation. A secret exists in the browser only while revealed (60 s), as for the user's own
// keys, and is dropped on re-mask, key change or when the section is rebuilt.
const SECRET_TTL = 60;
const NAME = /^[a-z0-9]([a-z0-9-]{0,18}[a-z0-9])?$/;
let seq = 0;

export const PERMISSIONS = {
  read: ["Read-only", "List and download files in all your buckets."],
  write: ["Write-only", "Upload and overwrite files in all your buckets."],
  "read-write": ["Read and write", "Read, upload and overwrite files in all your buckets."],
  "full-control": ["Full control", "Everything you can do, including bucket policies, permissions and deleting buckets."],
};
const label = (p) => PERMISSIONS[p]?.[0] ?? p ?? "No access";

export const checkName = (v) => {
  if (!v) return "Enter a name.";
  if (v.length > 20) return "Use at most 20 characters.";
  if (!NAME.test(v)) return "Use lowercase letters, digits and hyphens; start and end with a letter or digit.";
  return "";
};

const FULL_WARNING = "Full control can do everything you can do: change bucket policies and permissions, and delete buckets and their files. Only choose it if you really need that.";

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

// Confirm full control; resolves true when the user accepts.
const confirmFull = (what) => confirmDialog({
  title: what,
  message: `${FULL_WARNING} Anyone holding this subuser's key can do all of it.`,
  confirmLabel: "Give full control",
  danger: true,
});

/** subusersSection(creds) -> element. `creds` is the GET /api/credentials data (endpoint, region). */
export function subusersSection(creds) {
  const id = `su${++seq}`;
  const root = h("section", { class: "sub", "aria-labelledby": `${id}-h` });
  let data = null; // {subusers, permissions, limits}
  let loadError = "";
  let open = null; // name of the subuser whose details are shown
  const picked = {}; // name -> selected access key
  let busy = false;
  let alive = true;
  let seen = false; // has been in the document (so a later detach means it was replaced)

  async function load() {
    try {
      data = await listSubusers();
      loadError = "";
    } catch (err) {
      if (err.status === 401) return;
      loadError = err.message || "Subusers could not be loaded.";
    }
    paint();
  }

  // Run one action: serialize, report errors as toasts, then re-read the list.
  async function act(fn) {
    if (busy) return;
    busy = true;
    paint();
    try {
      await fn();
    } catch (err) {
      if (!(err.name === "ApiError" && err.status === 401)) toast.error(err.message || "That did not work. Try again.", { timeout: 9000 });
    }
    busy = false;
    await load();
  }

  function keyPanel(s) {
    const keys = s.keys.map((k) => k.accessKey);
    const limits = data.limits;
    const profile = `bucketeer-sub-${s.name}`;
    let selected = keys.includes(picked[s.name]) ? picked[s.name] : keys[0];
    let secret = "";
    const field = h("secret-field", { label: "Secret access key", ttl: String(SECRET_TTL) });
    field.fetcher = async () => {
      const key = selected;
      const r = await revealSubuserKey(s.name, key);
      return selected === key ? r.secretKey : "";
    };
    const panel = h("code-panel");
    const paintSnippets = () => {
      const cfg = creds ? configSnippet({ ...creds, profile }) : null;
      const cr = selected ? credentialsSnippet({ profile }, selected, secret) : null;
      panel.tabs = [
        cfg ? { label: "~/.aws/config", filename: "config", content: cfg }
          : { label: "~/.aws/config", filename: "config", content: "# The S3 endpoint is not configured yet.\n", locked: "Ask an administrator to set the S3 endpoint." },
        cr ? { label: "~/.aws/credentials", filename: "credentials", content: cr, ...(secret ? {} : { locked: "Reveal the secret to copy." }) }
          : { label: "~/.aws/credentials", filename: "credentials", content: "# This key cannot be shown as a file.\n", locked: "This key cannot be written to a credentials file." },
      ];
    };
    field.addEventListener("sf-reveal", () => { secret = field.value; paintSnippets(); });
    field.addEventListener("sf-mask", () => { secret = ""; paintSnippets(); });
    paintSnippets();
    panel.hint = "Merge these into your existing files; do not overwrite them.";

    let keyEl;
    if (keys.length > 1) {
      const sel = h("select", { class: "input", id: `${id}-k-${s.name}`, "aria-label": "Access key" },
        keys.map((k) => h("option", { value: k, text: k, selected: k === selected })));
      sel.addEventListener("change", () => { field.clear(); selected = sel.value; picked[s.name] = selected; secret = ""; paintSnippets(); });
      keyEl = h("div", { class: "field" }, h("label", { for: sel.id, text: "Access key" }), sel);
    } else {
      keyEl = h("div", { class: "field" }, h("span", { class: "label", text: "Access key" }),
        h("div", { class: "cred-key" }, h("code", { text: selected }), copyBtn(selected, "Copy access key")));
    }

    const add = h("button", { class: "btn btn-sm", type: "button", disabled: busy || keys.length >= limits.maxKeys,
      title: keys.length >= limits.maxKeys ? `A subuser can have at most ${limits.maxKeys} keys.` : null }, icon("plus"), "Add key");
    add.addEventListener("click", () => act(async () => {
      const r = await createSubuserKey(s.name);
      picked[s.name] = r.accessKey;
      toast.show("Key added. Reveal its secret to use it.");
    }));
    const rotate = h("button", { class: "btn btn-sm", type: "button", disabled: busy || !selected }, icon("refresh"), "Rotate");
    rotate.addEventListener("click", () => act(async () => {
      const key = selected;
      const ok = await confirmDialog({
        title: "Rotate this access key?",
        message: `A new key is created first, then ${key} is removed. Anything still using ${key} stops working until it uses the new key.`,
        confirmLabel: "Rotate key", danger: true,
      });
      if (!ok) return;
      const r = await rotateSubuserKey(s.name, key);
      picked[s.name] = r.accessKey;
      if (r.oldKeyRemoved) toast.show("Key rotated. Update ~/.aws/credentials with the new key.");
      else toast.error(r.message || "The new key was created, but the old one could not be removed.", { timeout: 12000 });
    }));
    const del = h("button", { class: "btn btn-sm btn-quiet-danger", type: "button", disabled: busy || keys.length < 2,
      title: keys.length < 2 ? "This is the only key. Rotate it instead." : null }, icon("trash"), "Delete key");
    del.addEventListener("click", () => act(async () => {
      const key = selected;
      const ok = await confirmDialog({
        title: "Delete this access key?",
        message: `${key} stops working immediately. This cannot be undone.`,
        confirmLabel: "Delete key", danger: true,
      });
      if (!ok) return;
      await deleteSubuserKey(s.name, key);
      delete picked[s.name];
      toast.show("Key deleted.");
    }));

    const cmd = `aws --profile ${profile} s3 ls`;
    return [
      keyEl,
      h("div", { class: "cred-actions" }, add, rotate, del),
      field, panel,
      h("div", { class: "cred-test" }, h("span", { class: "label", text: "Test it" }),
        h("div", { class: "cred-cmd" }, h("code", { text: cmd }), copyBtn(cmd, "Copy test command"))),
    ];
  }

  function permissionSelect(s) {
    const sel = h("select", { class: "input", id: `${id}-p-${s.name}`, disabled: busy },
      Object.entries(PERMISSIONS).map(([value, [text]]) => h("option", { value, text, selected: value === s.permission })));
    sel.addEventListener("change", () => {
      const next = sel.value;
      if (next === s.permission) return;
      act(async () => {
        let confirm = false;
        if (next === "full-control") {
          if (!(await confirmFull(["Give ", nameSpan(s.name), " full control?"]))) return;
          confirm = true;
        }
        await updateSubuser(s.name, next, confirm);
        toast.show(`${s.name} is now ${label(next).toLowerCase()}.`);
      });
    });
    return h("div", { class: "field" }, h("label", { for: sel.id, text: "Permission" }), sel,
      h("p", { class: "set-hint", text: PERMISSIONS[s.permission]?.[1] ?? "This subuser has no access." }));
  }

  function row(s) {
    const shown = open === s.name;
    const toggle = h("button", { class: "btn btn-sm", type: "button", "aria-expanded": String(shown), "aria-controls": `${id}-d-${s.name}`,
      on: { click: () => { open = shown ? null : s.name; paint(); } } }, icon(shown ? "up" : "down"), shown ? "Hide" : "Use");
    const remove = h("button", { class: "btn btn-sm btn-quiet-danger", type: "button", disabled: busy, "aria-label": `Delete subuser ${s.name}`,
      on: { click: () => act(async () => {
        const ok = await confirmDialog({
          title: ["Delete the subuser ", nameSpan(s.name), "?"], danger: true, confirmLabel: "Delete subuser",
          message: "Its keys stop working immediately and are deleted. This cannot be undone, and the name can never be used again.",
        });
        if (!ok) return;
        await deleteSubuser(s.name);
        if (open === s.name) open = null;
        toast.show(`Subuser ${s.name} deleted.`);
      }) } }, icon("trash"), h("span", { class: "bk-del-l", text: "Delete" }));
    const full = s.permission === "full-control";
    return h("li", { class: "bkd-key", "data-open": String(shown) },
      h("div", { class: "bkd-head" },
        h("div", { class: "bkd-id" },
          h("b", { class: "bkd-label", text: s.name }),
          h("span", { class: "bkd-sub" },
            h("span", { class: `chip ${full ? "chip-warn" : ""}` }, full && icon("alert"), label(s.permission)),
            h("span", { class: "chip", text: `${s.keys.length} ${s.keys.length === 1 ? "key" : "keys"}` }))),
        h("div", { class: "bk-acts" }, toggle, remove)),
      shown && h("div", { class: "bkd-detail", id: `${id}-d-${s.name}` }, permissionSelect(s), keyPanel(s)));
  }

  // -- create form (kept across repaints so typing is not lost) --
  const name = h("input", { class: "input mono-input", id: `${id}-n`, autocomplete: "off", autocapitalize: "off", spellcheck: "false",
    maxlength: "20", placeholder: "e.g. ci-uploader", "aria-describedby": `${id}-nh ${id}-ne` });
  const nameErr = h("p", { class: "set-err", id: `${id}-ne`, role: "alert", hidden: true });
  const radios = Object.entries(PERMISSIONS).map(([value, [title, hint]]) =>
    h("label", { class: "qd-check bkd-opt", "data-danger": value === "full-control" ? "true" : null },
      h("input", { type: "radio", name: `${id}-perm`, value, checked: value === "read" }),
      h("span", {}, h("b", { text: title }), h("small", { text: hint }))));
  const fullNote = h("p", { class: "cred-warn", role: "status", hidden: true, text: FULL_WARNING });
  const formError = h("p", { class: "qd-error", role: "alert", hidden: true });
  const submit = h("button", { class: "btn btn-primary", type: "submit" }, icon("plus"), "Create subuser");
  const chosen = () => root.querySelector(`input[name="${id}-perm"]:checked`)?.value ?? "read";
  const showNameErr = (m) => { nameErr.textContent = m; nameErr.hidden = !m; name.setAttribute("aria-invalid", String(!!m)); };
  const showError = (m) => { formError.textContent = m; formError.hidden = !m; };
  name.addEventListener("input", () => { showError(""); showNameErr(name.value ? checkName(name.value) : ""); });
  for (const r of radios) r.addEventListener("change", () => { fullNote.hidden = chosen() !== "full-control"; });
  const form = h("form", { class: "bkd-form", novalidate: true },
    h("h3", { text: "Create a subuser" }),
    h("p", { class: "cd-msg", text: "A subuser is an extra identity of your account with its own keys, for a script or a tool. It can reach all your buckets with the permission you choose, so prefer a bucket key when one bucket is enough." }),
    h("div", { class: "field" }, h("label", { for: name.id, text: "Name" }), name,
      h("p", { class: "set-hint", id: `${id}-nh`, text: "Names the subuser and its AWS profile (bucketeer-sub-<name>). Lowercase letters, digits, hyphens; up to 20. A name is never reused." }), nameErr),
    h("fieldset", { class: "bkd-access" }, h("legend", { class: "label", text: "Permission" }), radios),
    fullNote, formError, h("div", { class: "cd-actions" }, submit));

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (busy) return;
    const bad = checkName(name.value);
    if (bad) { showNameErr(bad); name.focus(); return; }
    const permission = chosen();
    if (permission === "full-control" && !(await confirmFull(["Create ", nameSpan(name.value), " with full control?"]))) return;
    busy = true;
    submit.disabled = true;
    submit.replaceChildren(h("span", { class: "spinner", "aria-hidden": "true" }), "Creating…");
    try {
      const made = await createSubuser(name.value, permission, permission === "full-control");
      toast.show(`Subuser ${made.name} created. Reveal its key to use it.`);
      open = made.name;
      name.value = "";
      showNameErr("");
      showError("");
    } catch (err) {
      if (err.status === 401) return;
      showError(err.message || "Could not create the subuser.");
    }
    busy = false;
    submit.disabled = false;
    submit.replaceChildren(icon("plus"), "Create subuser");
    await load();
  });

  function paint() {
    if (!alive) return;
    if (!data) {
      root.replaceChildren(
        h("div", { class: "sub-head" }, h("h3", { id: `${id}-h`, text: "Subusers" })),
        loadError
          ? h("p", { class: "set-err", role: "alert", text: loadError })
          : h("div", { class: "acct-line" }, h("span", { class: "spinner", "aria-hidden": "true" }), h("span", { text: "Loading subusers…" })));
      return;
    }
    const { subusers, limits } = data;
    const atLimit = subusers.length >= limits.maxSubusers;
    submit.disabled = busy || atLimit;
    submit.title = atLimit ? `You can have at most ${limits.maxSubusers} subusers.` : "";
    const list = subusers.length
      ? h("ul", { class: "bkd-list" }, subusers.map(row))
      : h("div", { class: "bkd-empty" }, icon("users", "icon"),
        h("p", { text: "No subusers yet. Create one to give a script its own key with a limited permission." }));
    root.replaceChildren(
      h("div", { class: "sub-head" },
        h("h3", { id: `${id}-h`, text: "Subusers" }),
        h("span", { class: "chip", text: `${subusers.length} of ${limits.maxSubusers}` })),
      h("p", { class: "panel-note", text: "Extra identities of your account for scripts and tools. Each has its own keys and one permission across all your buckets. They are suspended together with your account." }),
      loadError && h("p", { class: "set-err", role: "alert", text: loadError }),
      h("div", { "aria-live": "polite" }, list),
      atLimit ? h("p", { class: "acct-sub", text: "You have reached the limit. Delete a subuser to create another." }) : form);
  }

  // Advanced only: built lazily, dropped again when the toggle goes off.
  function sync() {
    if (!alive) return;
    if (root.isConnected) seen = true;
    else if (seen) { alive = false; unsubscribe(); return; } // the card was rebuilt
    root.hidden = !isAdvanced();
    if (isAdvanced() && !data && !loadError) { paint(); load(); }
    if (!isAdvanced()) { open = null; data = null; loadError = ""; root.replaceChildren(); }
  }
  const unsubscribe = subscribe(sync);
  queueMicrotask(sync);
  root.hidden = !isAdvanced();
  return root;
}
