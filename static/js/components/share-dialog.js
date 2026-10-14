import { h } from "../dom.js";
import { icon } from "../icons.js";
import { createBucketShare, listBucketShares, lookupShareRecipient, revokeBucketShare, updateBucketShare } from "../api.js";
import { toast } from "./toast.js";
import { confirmDialog, nameSpan } from "./confirm-dialog.js";

// shareDialog({ bucket }) -> Promise<{ error? } | { changed: boolean }>
// Share one bucket with an AD colleague: type a username or email, confirm the person the
// server found (exact lookup, no directory search), choose read-only or read and write.
// Lists current shares with change-access and revoke. The server decides who is eligible.
let seq = 0;
const ACCESS = { read: "Read-only", readwrite: "Read and write" };
const STATUS = {
  active: ["chip-ok", "Active"],
  out_of_sync: ["chip-warn", "Out of sync"],
  unknown: ["chip-warn", "Unknown share"],
  unchecked: ["chip", "Not verified"],
};

export async function shareDialog({ bucket }) {
  const id = `sh${++seq}`;
  let shares;
  let checked = true;
  try {
    ({ shares, checked } = await listBucketShares(bucket));
  } catch (err) {
    return { error: err };
  }
  let changed = false;
  let busy = false;
  let found = null; // person the server found, awaiting confirmation

  const list = h("ul", { class: "bkd-list" });
  const live = h("div", { "aria-live": "polite" }, list);

  function row(s) {
    const [cls, text] = STATUS[s.status] ?? STATUS.unchecked;
    const who = s.displayName || s.recipient || s.uid;
    const sel = h("select", { class: "input shd-acc", "aria-label": `Access for ${who}` },
      Object.entries(ACCESS).map(([v, l]) => h("option", { value: v, selected: v === s.access, text: l })));
    sel.addEventListener("change", () => onAccess(s, sel.value, sel));
    const acts = [sel];
    if (s.status === "out_of_sync") {
      acts.push(h("button", { class: "btn btn-sm", type: "button", on: { click: () => onAccess(s, s.access, null, true) } }, icon("refresh"), "Repair"));
    }
    acts.push(h("button", { class: "btn btn-sm btn-quiet-danger", type: "button", "aria-label": `Stop sharing with ${who}`, on: { click: () => onRevoke(s) } },
      icon("trash"), h("span", { class: "bk-del-l", text: "Revoke" })));
    if (s.status === "unknown") acts.shift(); // nothing to change on a statement we have no record of
    return h("li", { class: "bkd-key" },
      h("div", { class: "bkd-head" },
        h("div", { class: "bkd-id" },
          h("b", { class: "shd-who", text: who }),
          h("span", { class: "bkd-sub" },
            s.displayName && h("code", { class: "shd-user", text: s.recipient }),
            h("span", { class: `chip ${cls}` }, h("span", { class: "dot" }), text),
            s.status === "unknown" && h("span", { class: "chip" }, ACCESS[s.access] ?? s.access))),
        h("div", { class: "bk-acts shd-acts" }, acts)));
  }

  function paint() {
    if (!shares.length) {
      list.replaceChildren(h("li", { class: "bkd-empty" }, icon("users", "icon"),
        h("p", { text: "Not shared with anyone yet." })));
      return;
    }
    list.replaceChildren(...shares.map(row));
  }
  async function reload() {
    ({ shares, checked } = await listBucketShares(bucket));
    note.hidden = checked;
    paint();
  }

  async function onAccess(s, access, sel, repair = false) {
    if (busy) return;
    busy = true;
    try {
      await updateBucketShare(bucket, s.id, access);
      changed = true;
      toast.show(repair ? "Share repaired." : `${s.displayName || s.recipient} now has ${ACCESS[access].toLowerCase()} access.`);
    } catch (err) {
      if (err.status !== 401) toast.error(err.message, { timeout: 9000 });
      if (sel) sel.value = s.access;
    }
    try { await reload(); } catch (err) { if (err.status !== 401) toast.error(err.message); }
    busy = false;
  }

  async function onRevoke(s) {
    if (busy) return;
    const who = s.displayName || s.recipient || s.uid;
    const ok = await confirmDialog({
      title: ["Stop sharing with ", nameSpan(who), "?"], danger: true, confirmLabel: "Stop sharing",
      message: "They lose access to this bucket right away. Files stay where they are. You can share again later.",
    });
    if (!ok) return;
    busy = true;
    try {
      await revokeBucketShare(bucket, s.id);
      changed = true;
      toast.show(`Stopped sharing with ${who}.`);
    } catch (err) {
      if (err.status !== 401) toast.error(err.message, { timeout: 9000 });
    }
    try { await reload(); } catch (err) { if (err.status !== 401) toast.error(err.message); }
    busy = false;
  }

  // -- add form: step 1 find, step 2 confirm --
  const login = h("input", { class: "input", id: `${id}-l`, type: "text", autocomplete: "off", autocapitalize: "off", spellcheck: "false",
    maxlength: "256", placeholder: "username or name@example.org", "aria-describedby": `${id}-lh` });
  const error = h("p", { class: "qd-error", role: "alert", hidden: true });
  const showError = (m) => { error.textContent = m; error.hidden = !m; };
  const find = h("button", { class: "btn", type: "submit" }, icon("search"), "Find");
  const confirmBox = h("div", { class: "shd-confirm", hidden: true });
  const note = h("p", { class: "lcd-tip", role: "note", hidden: checked }, icon("alert", "icon"),
    "The bucket policy could not be read, so the list below is not verified against it.");

  const radio = (value, title, hint, on) =>
    h("label", { class: "qd-check bkd-opt" },
      h("input", { type: "radio", name: `${id}-a`, value, checked: on }),
      h("span", {}, h("b", { text: title }), h("small", { text: hint })));

  function clearFound() {
    found = null;
    confirmBox.hidden = true;
    confirmBox.replaceChildren();
  }

  function showFound(p) {
    found = p;
    const go = h("button", { class: "btn btn-primary", type: "button" }, icon("check"), "Share");
    const cancel = h("button", { class: "btn", type: "button", text: "Cancel" });
    cancel.addEventListener("click", () => { clearFound(); login.focus(); });
    go.addEventListener("click", () => submitShare(go));
    confirmBox.replaceChildren(
      h("p", { class: "shd-found" }, icon("user", "icon"),
        h("span", {}, "Share with ", h("b", { text: p.displayName || p.username }),
          p.displayName && h("code", { class: "shd-user", text: p.username }), "?")),
      h("fieldset", { class: "bkd-access" },
        h("legend", { class: "label", text: "Access" }),
        radio("read", "Read-only", "List and download files.", true),
        radio("readwrite", "Read and write", "Also upload, overwrite and delete files.", false)),
      h("p", { class: "set-hint", text: "If this person has no S3 account yet, one is created for them. They find the bucket under Shared with you." }),
      h("div", { class: "cd-actions" }, cancel, go));
    confirmBox.hidden = false;
    go.focus();
  }

  async function submitShare(go) {
    if (busy || !found) return;
    busy = true;
    showError("");
    go.disabled = true;
    go.replaceChildren(h("span", { class: "spinner", "aria-hidden": "true" }), "Sharing…");
    const person = found;
    try {
      const access = confirmBox.querySelector("input[type=radio]:checked").value;
      const res = await createBucketShare(bucket, person.username, access);
      changed = true;
      toast.show(res.warning ?? `Shared with ${person.displayName || person.username}${res.accountCreated ? " (S3 account created)" : ""}.`, res.warning ? { timeout: 9000 } : undefined);
      clearFound();
      login.value = "";
      await reload();
    } catch (err) {
      if (err.status === 401) { dlg.close(); return; }
      showError(err.message || "Could not share the bucket.");
      go.disabled = false;
      go.replaceChildren(icon("check"), "Share");
    } finally {
      busy = false;
    }
  }

  login.addEventListener("input", () => { showError(""); if (found) clearFound(); });
  const form = h("form", { class: "bkd-form", novalidate: true },
    h("h3", { text: "Share with a person" }),
    h("p", { class: "cd-msg", text: "Type the username or email address of a colleague. You choose whether they can only read or also change files." }),
    h("div", { class: "field" }, h("label", { for: login.id, text: "Username or email" }),
      h("div", { class: "shd-find" }, login, find),
      h("p", { class: "set-hint", id: `${id}-lh`, text: "Only people who can sign in to Bucketeer can be added. Their access uses their own S3 keys." })),
    error, confirmBox);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (busy) return;
    const q = login.value.trim();
    if (!q) { showError("Enter a username or email address."); login.focus(); return; }
    busy = true;
    clearFound();
    showError("");
    find.disabled = true;
    find.replaceChildren(h("span", { class: "spinner", "aria-hidden": "true" }), "Looking…");
    try {
      const p = await lookupShareRecipient(bucket, q);
      if (p.isYou) showError("That is you. The bucket is already yours.");
      else showFound(p);
    } catch (err) {
      if (err.status === 401) { dlg.close(); return; }
      showError(err.message || "Could not look that person up.");
    } finally {
      busy = false;
      find.disabled = false;
      find.replaceChildren(icon("search"), "Find");
    }
  });

  const close = h("button", { class: "btn", type: "button", text: "Close" });
  const dlg = h("dialog", { class: "cd qd bkd shd", "aria-labelledby": `${id}-t`, "aria-describedby": `${id}-m` },
    h("div", { class: "bkd-body" },
      h("div", { class: "qd-head" },
        h("h2", { id: `${id}-t` }, "Share ", nameSpan(bucket)),
        h("p", { class: "cd-msg", id: `${id}-m`, text: "Give colleagues access to this bucket with their own S3 keys. Revoke it at any time." })),
      note, live, form,
      h("div", { class: "cd-actions" }, close)));
  paint();
  close.addEventListener("click", () => dlg.close());
  dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });

  return new Promise((resolve) => {
    dlg.addEventListener("close", () => { dlg.remove(); resolve({ changed }); });
    document.body.append(dlg);
    dlg.showModal();
    login.focus();
  });
}
