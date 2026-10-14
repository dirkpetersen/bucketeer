import { h } from "../dom.js";
import { icon } from "../icons.js";
import { getAcl, putAcl, lookupShareRecipient } from "../api.js";
import { toast } from "./toast.js";
import { confirmDialog, nameSpan } from "./confirm-dialog.js";
import { keyPicker } from "./key-picker.js";

// aclDialog({ bucket, object }) -> Promise<{ error? } | { changed }>
// View and edit the legacy S3 ACL of a bucket, or of one object chosen by key (autocomplete,
// not a file browser). Edits stay in the dialog until "Save"; the server re-checks everything.
// People are added exactly like shares (exact lookup, confirm step). Public/group grants are
// shown with a warning and can only be removed: the server refuses to add them (Q12).
let seq = 0;
const PERMS = {
  READ: { label: "Read", bucket: "List the files in the bucket.", object: "Download the file." },
  WRITE: { label: "Write", bucket: "Upload, overwrite and delete files in the bucket.", object: "" },
  READ_ACP: { label: "Read permissions", bucket: "See who has access to the bucket.", object: "See who has access to the file." },
  WRITE_ACP: { label: "Change permissions", bucket: "Change who has access, including giving themselves more.", object: "Change who has access, including giving themselves more." },
  FULL_CONTROL: { label: "Full control", bucket: "Everything above, including changing permissions.", object: "Everything above, including changing permissions." },
};
const SCOPES = {
  public: "Anyone on the internet",
  signed_in: "Any signed-in S3 user",
  logs: "Log delivery service",
};
const RISKY = new Set(["WRITE_ACP", "FULL_CONTROL"]);

const keyOf = (r) => `${r.id ?? ""}|${r.uri ?? ""}|${r.email ?? ""}|${r.login ?? ""}|${r.permission}`;

export async function aclDialog({ bucket, object = false }) {
  const id = `acl${++seq}`;
  const what = object ? "file" : "bucket";
  let loaded = null; // server view
  let rows = []; // working copy
  let original = "";
  let busy = false;
  let found = null;
  let changed = false;
  let objectKey = null;

  if (!object) {
    try { loaded = await getAcl(bucket); } catch (err) { return { error: err }; }
  }

  const error = h("p", { class: "qd-error", role: "alert", hidden: true }); // loading the object
  const showError = (m) => { error.textContent = m; error.hidden = !m; };
  const saveError = h("p", { class: "qd-error", role: "alert", hidden: true });
  const showSaveError = (m) => { saveError.textContent = m; saveError.hidden = !m; };

  const warn = h("p", { class: "lcd-tip acl-warn", role: "note", hidden: true }, icon("alert", "icon"), h("span"));
  const list = h("ul", { class: "bkd-list" });
  const live = h("div", { "aria-live": "polite" }, list);
  const save = h("button", { class: "btn btn-primary", type: "button", disabled: true }, icon("check"), "Save changes");
  const revert = h("button", { class: "btn", type: "button", disabled: true, text: "Discard changes" });
  const close = h("button", { class: "btn", type: "button", text: "Close" });

  const perms = () => loaded.permissions;
  const desc = (p) => (PERMS[p] ? PERMS[p][object ? "object" : "bucket"] : "");
  const dirty = () => rows.map(keyOf).join("\n") !== original;

  function initRows(view) {
    loaded = view;
    rows = view.grants.map((g) => ({ ...g }));
    original = rows.map(keyOf).join("\n");
  }
  function sync() {
    save.disabled = busy || !dirty();
    revert.disabled = busy || !dirty();
  }

  function title(r) {
    if (r.kind === "group") return SCOPES[r.scope] ?? `Group ${r.name}`;
    if (r.kind === "unknown") return r.email ? `Unknown user ${r.email}` : `Unknown user ${r.id}`;
    return r.name || r.id;
  }

  function row(r) {
    const chips = [];
    if (r.kind === "owner") chips.push(h("span", { class: "chip chip-accent" }, "Owner"));
    if (r.isNew) chips.push(h("span", { class: "chip chip-ok" }, h("span", { class: "dot" }), "New, not saved yet"));
    if (r.kind === "group" && r.scope !== "logs") chips.push(h("span", { class: "chip chip-warn" }, h("span", { class: "dot" }), r.scope === "public" ? "Public" : "Wide open"));
    if (r.kind === "group" && r.scope === "logs") chips.push(h("span", { class: "chip" }, "Service"));
    if (r.kind === "unknown") chips.push(h("span", { class: "chip chip-warn" }, h("span", { class: "dot" }), "Unknown"));
    if (RISKY.has(r.permission) && r.kind !== "owner") chips.push(h("span", { class: "chip chip-warn" }, "Can change permissions"));
    const who = title(r);
    const acts = [];
    if (r.kind === "owner" || r.kind === "group") {
      acts.push(h("span", { class: "chip acl-perm" }, PERMS[r.permission]?.label ?? r.permission));
    } else {
      const choices = perms().includes(r.permission) ? perms() : [r.permission, ...perms()];
      const sel = h("select", { class: "input shd-acc", "aria-label": `Permission for ${who}` },
        choices.map((p) => h("option", { value: p, selected: p === r.permission, text: PERMS[p]?.label ?? p })));
      sel.addEventListener("change", () => { r.permission = sel.value; paint(); });
      acts.push(sel);
    }
    if (r.removable) {
      acts.push(h("button", { class: "btn btn-sm btn-quiet-danger", type: "button", "aria-label": `Remove ${who}`,
        on: { click: () => { rows = rows.filter((x) => x !== r); paint(); } } },
      icon("trash"), h("span", { class: "bk-del-l", text: "Remove" })));
    }
    const sub = r.kind === "user" && r.id && r.id !== r.name ? h("code", { class: "shd-user", text: r.id })
      : r.kind === "unknown" && r.id ? null
      : r.kind === "group" ? h("code", { class: "shd-user", text: r.uri ?? r.id })
      : r.isNew && r.login ? h("code", { class: "shd-user", text: r.login }) : null;
    return h("li", { class: "bkd-key acl-row", "data-risk": String(r.kind === "group" && r.scope !== "logs") },
      h("div", { class: "bkd-head" },
        h("div", { class: "bkd-id" },
          h("b", { class: "shd-who", text: who }),
          h("span", { class: "bkd-sub" }, sub, chips),
          h("span", { class: "acl-desc", text: desc(r.permission) })),
        h("div", { class: "bk-acts shd-acts" }, acts)));
  }

  function paint() {
    list.replaceChildren(...rows.map(row));
    const open = rows.filter((r) => r.kind === "group" && r.scope !== "logs");
    warn.hidden = open.length === 0;
    if (open.length) {
      warn.querySelector("span").textContent = `This ${what} is open to ${open.some((r) => r.scope === "public") ? "anyone on the internet" : "every signed-in S3 user"}. `
        + "Remove the grant to close it. Bucketeer cannot add public access yet.";
    }
    sync();
  }

  // -- add a person: find, then confirm --
  const login = h("input", { class: "input", id: `${id}-l`, type: "text", autocomplete: "off", autocapitalize: "off", spellcheck: "false",
    maxlength: "256", placeholder: "username or name@example.org", "aria-describedby": `${id}-lh` });
  const find = h("button", { class: "btn", type: "submit" }, icon("search"), "Find");
  const confirmBox = h("div", { class: "shd-confirm", hidden: true });
  const addError = h("p", { class: "qd-error", role: "alert", hidden: true });
  const showAddError = (m) => { addError.textContent = m; addError.hidden = !m; };

  function clearFound() { found = null; confirmBox.hidden = true; confirmBox.replaceChildren(); }
  function showFound(p) {
    found = p;
    const sel = h("select", { class: "input shd-acc", id: `${id}-p`, "aria-label": "Permission" },
      perms().map((x) => h("option", { value: x, selected: x === "READ", text: PERMS[x].label })));
    const note = h("span", { class: "set-hint", text: desc("READ") });
    sel.addEventListener("change", () => {
      note.textContent = desc(sel.value) + (RISKY.has(sel.value) ? " Only give this to people you trust." : "");
    });
    const go = h("button", { class: "btn btn-primary", type: "button" }, icon("plus"), "Add to list");
    const cancel = h("button", { class: "btn", type: "button", text: "Cancel" });
    cancel.addEventListener("click", () => { clearFound(); login.focus(); });
    go.addEventListener("click", () => {
      rows.push({ kind: "user", id: null, uri: null, email: null, login: p.username, name: p.displayName || p.username,
        permission: sel.value, scope: null, removable: true, isNew: true });
      clearFound();
      login.value = "";
      paint();
    });
    confirmBox.replaceChildren(
      h("p", { class: "shd-found" }, icon("user", "icon"),
        h("span", {}, "Give ", h("b", { text: p.displayName || p.username }),
          p.displayName && h("code", { class: "shd-user", text: p.username }), " access?")),
      h("div", { class: "field" }, h("label", { for: sel.id, text: "Permission" }), sel, note),
      h("p", { class: "set-hint", text: "If this person has no S3 account yet, one is created when you save. Nothing changes until you press Save changes." }),
      h("div", { class: "cd-actions" }, cancel, go));
    confirmBox.hidden = false;
    sel.focus();
  }

  login.addEventListener("input", () => { showAddError(""); if (found) clearFound(); });
  const addForm = h("form", { class: "bkd-form", novalidate: true },
    h("h3", { text: "Give a person access" }),
    h("div", { class: "field" }, h("label", { for: login.id, text: "Username or email" }),
      h("div", { class: "shd-find" }, login, find),
      h("p", { class: "set-hint", id: `${id}-lh`, text: "Only people who can sign in to Bucketeer can be added." })),
    addError, confirmBox);
  addForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (busy) return;
    const q = login.value.trim();
    if (!q) { showAddError("Enter a username or email address."); login.focus(); return; }
    busy = true;
    clearFound();
    showAddError("");
    find.disabled = true;
    find.replaceChildren(h("span", { class: "spinner", "aria-hidden": "true" }), "Looking…");
    try {
      const p = await lookupShareRecipient(bucket, q);
      if (p.isYou) showAddError("That is you. The owner always has full control.");
      else if (rows.some((r) => r.isNew && r.login === p.username)) showAddError("That person is already in the list below.");
      else showFound(p);
    } catch (err) {
      if (err.status === 401) { dlg.close(); return; }
      showAddError(err.message || "Could not look that person up.");
    } finally {
      busy = false;
      find.disabled = false;
      find.replaceChildren(icon("search"), "Find");
      sync();
    }
  });

  // -- save / discard --
  // New or raised WRITE_ACP / FULL_CONTROL for someone else needs an extra confirm.
  function raisedRisk() {
    const before = new Map(loaded.grants.map((g) => [`${g.id ?? g.uri ?? g.email}|${g.permission}`, true]));
    return rows.filter((r) => r.kind !== "owner" && RISKY.has(r.permission)
      && (r.isNew || !before.has(`${r.id ?? r.uri ?? r.email}|${r.permission}`)));
  }

  save.addEventListener("click", async () => {
    if (busy || !dirty()) return;
    const risky = raisedRisk();
    if (risky.length) {
      const ok = await confirmDialog({
        title: "Allow changing permissions?", confirmLabel: "Save permissions", danger: true,
        message: `${risky.map(title).join(", ")} will be able to change the permissions of this ${what}, including removing yours as a grantee. You stay the owner and keep full control. Only continue for people you trust.`,
      });
      if (!ok) return;
    }
    busy = true;
    showSaveError("");
    save.disabled = true;
    save.replaceChildren(h("span", { class: "spinner", "aria-hidden": "true" }), "Saving…");
    const keep = rows.filter((r) => r.kind !== "owner" && !r.isNew).map((r) => {
      const k = { permission: r.permission };
      if (r.uri) k.uri = r.uri; else if (r.id) k.id = r.id; else k.email = r.email;
      return k;
    });
    const add = rows.filter((r) => r.isNew).map((r) => ({ login: r.login, permission: r.permission }));
    try {
      const res = await putAcl(bucket, objectKey, { etag: loaded.etag, keep, add });
      changed = changed || res.changed;
      initRows(res);
      toast.show(res.changed ? "Permissions saved." : "Nothing needed to change.");
    } catch (err) {
      if (err.status === 401) { dlg.close(); return; }
      if (err.body?.etagMismatch) {
        initRows(err.body.current);
        showSaveError("The permissions were changed by someone else while you were editing. The current ones are shown; your edits were discarded.");
      } else {
        showSaveError(err.message || "Could not save the permissions.");
      }
    } finally {
      busy = false;
      save.replaceChildren(icon("check"), "Save changes");
      paint();
    }
  });
  revert.addEventListener("click", () => { initRows(loaded); showSaveError(""); clearFound(); paint(); });

  const editor = h("div", { class: "acl-editor", hidden: object },
    warn, live,
    addForm, saveError,
    h("div", { class: "cd-actions" }, revert, save));

  // -- object mode: pick the key first --
  let picker = null;
  let pickerBox = null;
  const loadBtn = h("button", { class: "btn", type: "button", disabled: true }, icon("search"), "Show permissions");
  if (object) {
    picker = keyPicker({
      id,
      bucket,
      onChange: () => {
        showError("");
        showSaveError("");
        editor.hidden = true;
        objectKey = null;
        loadBtn.disabled = busy || !picker.input.value.trim() || picker.input.value.endsWith("/");
      },
      onError: showError,
      onUnauthorized: () => dlg.close(),
      onEnter: () => loadBtn.click(),
    });
    loadBtn.addEventListener("click", async () => {
      if (busy || loadBtn.disabled) return;
      busy = true;
      showError("");
      picker.close();
      loadBtn.disabled = true;
      const key = picker.input.value;
      try {
        const view = await getAcl(bucket, key);
        objectKey = key;
        initRows(view);
        clearFound();
        editor.hidden = false;
        paint();
      } catch (err) {
        if (err.status === 401) { dlg.close(); return; }
        showError(err.message || "Could not read the permissions.");
      } finally {
        busy = false;
        loadBtn.disabled = !picker.input.value.trim() || picker.input.value.endsWith("/");
      }
    });
    pickerBox = h("div", { class: "field" },
      h("label", { for: picker.input.id, text: "File name (key)" }),
      ...picker.nodes,
      h("div", { class: "acl-load" }, loadBtn));
  } else {
    initRows(loaded);
    paint();
  }

  const dlg = h("dialog", { class: "cd qd bkd shd acl", "aria-labelledby": `${id}-t`, "aria-describedby": `${id}-m` },
    h("div", { class: "bkd-body" },
      h("div", { class: "qd-head" },
        h("h2", { id: `${id}-t` }, object ? "File permissions in " : "Permissions of ", nameSpan(bucket)),
        h("p", { class: "cd-msg", id: `${id}-m`, text: object
          ? "Access control lists (ACLs) are the legacy way to give people access to a single file. Most of the time a bucket share is simpler."
          : "Access control lists (ACLs) are the legacy way to give people access to a bucket. Most of the time sharing the bucket is simpler." })),
      pickerBox, error, editor,
      h("div", { class: "cd-actions" }, close)));
  close.addEventListener("click", () => dlg.close());
  dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });

  return new Promise((resolve) => {
    dlg.addEventListener("close", () => { picker?.dispose(); dlg.remove(); resolve({ changed }); });
    document.body.append(dlg);
    dlg.showModal();
    (picker ? picker.input : login).focus();
  });
}
