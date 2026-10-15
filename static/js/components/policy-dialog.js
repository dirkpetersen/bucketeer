import { h } from "../dom.js";
import { icon } from "../icons.js";
import { deleteBucketPolicy, getBucketPolicy, putBucketPolicy } from "../api.js";
import { toast } from "./toast.js";
import { confirmDialog, nameSpan } from "./confirm-dialog.js";

// policyDialog({ bucket }) -> Promise<{ error? } | { changed: boolean }>
// The raw bucket policy (Advanced only; the server does not care about the toggle). Shows the
// whole document. Statements whose Sid starts with "bucketeer-" belong to sharing and bucket keys:
// they are listed above the editor, and the server refuses to change them without confirmation.
// The etag the policy was loaded with travels with every save, so a share or key added in the
// meantime is never overwritten (409): the user then sees the current policy and keeps their text.
let seq = 0;
const TEMPLATE = { Version: "2012-10-17", Statement: [] };
const pretty = (doc) => JSON.stringify(doc ?? TEMPLATE, null, 2);

// Offset of the first JSON syntax error (a small recursive-descent scan), for engines whose
// JSON.parse message carries no position. Returns -1 when the text parses.
function errorOffset(t) {
  let i = 0;
  const ws = () => { while (i < t.length && " \t\r\n".includes(t[i])) i += 1; };
  const fail = () => { throw i; };
  const str = () => {
    i += 1;
    while (i < t.length && t[i] !== '"') { if (t[i] === "\\") i += 1; else if (t[i] < " ") fail(); i += 1; }
    if (i >= t.length) fail();
    i += 1;
  };
  const value = () => {
    ws();
    const c = t[i];
    if (c === "{") {
      i += 1; ws();
      if (t[i] === "}") { i += 1; return; }
      for (;;) {
        ws(); if (t[i] !== '"') fail(); str(); ws();
        if (t[i] !== ":") fail(); i += 1; value(); ws();
        if (t[i] === ",") { i += 1; continue; }
        if (t[i] === "}") { i += 1; return; }
        fail();
      }
    } else if (c === "[") {
      i += 1; ws();
      if (t[i] === "]") { i += 1; return; }
      for (;;) {
        value(); ws();
        if (t[i] === ",") { i += 1; continue; }
        if (t[i] === "]") { i += 1; return; }
        fail();
      }
    } else if (c === '"') str();
    else {
      const m = /^(-?\d+(\.\d+)?([eE][+-]?\d+)?|true|false|null)/.exec(t.slice(i));
      if (!m) fail();
      i += m[0].length;
    }
  };
  try { value(); ws(); if (i < t.length) fail(); } catch (at) { return typeof at === "number" ? at : -1; }
  return -1;
}

// Line and column of a JSON.parse failure, from the engine's message or else our own scan.
function where(text, err) {
  const msg = String(err?.message ?? "");
  let m = /line (\d+) column (\d+)/.exec(msg);
  if (m) return { line: Number(m[1]), col: Number(m[2]) };
  m = /position (\d+)/.exec(msg);
  const at = m ? Number(m[1]) : errorOffset(text);
  if (at < 0) return null;
  const lines = text.slice(0, at).split("\n");
  return { line: lines.length, col: lines[lines.length - 1].length + 1 };
}

// ---- Public / self-lockout screening (Q12) ----
// MIRRORS bucketeer/policy_guard.py so the editor can warn while typing. The SERVER IS
// AUTHORITATIVE: it re-checks every save (422) against the policy read inside the bucket lock.
// Conditions never change a verdict. Keep both files in step.
const POLICY_ACTIONS = ["s3:putbucketpolicy", "s3:getbucketpolicy", "s3:deletebucketpolicy"];
const PUBLIC_ARNS = ["arn:aws:iam::*:root", "arn:aws:iam::*:user/*"];
const REASONS = {
  public: "allows everyone, not just named users (a Condition does not change that). Bucketeer does not support public buckets.",
  lockout: "would lock you out of this bucket or its policy (a Condition does not change that).",
};
const escapeRe = (c) => c.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
function glob(pattern, text, fold = false) {
  const rx = [...pattern].map((c) => (c === "*" ? ".*" : c === "?" ? "." : escapeRe(c))).join("");
  return new RegExp(`^(?:${rx})$`, fold ? "is" : "s").test(text);
}
const strs = (v) => (typeof v === "string" ? [v.trim()] : Array.isArray(v) ? v.filter((x) => typeof x === "string").map((x) => x.trim()) : []);
// [type, value] pairs; a plain string or list is an AWS principal.
function principals(p) {
  if (p && typeof p === "object" && !Array.isArray(p)) return Object.entries(p).flatMap(([k, v]) => strs(v).map((x) => [k, x]));
  return strs(p).map((x) => ["AWS", x]);
}
// RGW reads arn:aws:iam::<t>:root and any plain string without ":" or "/" (even "") as tenant <t>.
function tenantOf(v) {
  if (v === "*") return null;
  const m = /^arn:aws:iam::([^:/]*):root$/.exec(v);
  if (m) return m[1];
  return !v.includes(":") && !v.includes("/") ? v : null;
}
const isPublicValue = (v) => v === "*" || PUBLIC_ARNS.includes(v);
function classify(st, owner) {
  if (!st || typeof st !== "object" || Array.isArray(st)) return null;
  if (st.Effect === "Allow") {
    if ("NotPrincipal" in st || principals(st.Principal).some(([k, v]) => isPublicValue(v) || (k === "AWS" && tenantOf(v) !== null))) return "public";
    return null;
  }
  if (st.Effect !== "Deny" || !owner) return null;
  const names = ([k, v]) => isPublicValue(v) || glob(v, owner.arn) || (k === "AWS" && tenantOf(v) === owner.tenant);
  const who = "NotPrincipal" in st ? !principals(st.NotPrincipal).some(names) : principals(st.Principal).some(names);
  let acts;
  if ("NotAction" in st) {
    const pats = strs(st.NotAction).map((a) => a.toLowerCase());
    acts = !POLICY_ACTIONS.every((a) => pats.some((p) => glob(p, a, true)));
  } else {
    const pats = strs(st.Action).map((a) => a.toLowerCase());
    acts = pats.some((p) => POLICY_ACTIONS.some((a) => glob(p, a, true)));
  }
  let res;
  if ("NotResource" in st) res = !strs(st.NotResource).some((p) => owner.bucketArns.some((a) => glob(p, a)));
  else res = strs(st.Resource).some((p) => owner.bucketArns.some((a) => glob(p, a)));
  return who && acts && res ? "lockout" : null;
}
function canon(v) {
  if (Array.isArray(v)) return `[${v.map(canon).join(",")}]`;
  if (v && typeof v === "object") return `{${Object.keys(v).sort().map((k) => `${JSON.stringify(k)}:${canon(v[k])}`).join(",")}}`;
  return JSON.stringify(v);
}
const statementsOf = (doc) => {
  const s = doc?.Statement;
  return Array.isArray(s) ? s : s && typeof s === "object" ? [s] : [];
};
// -> [{index, sid, kind, stored}] for every public/lockout statement of `doc`; `stored` = identical to one in `policy`.
function screen(doc, policy, owner) {
  const have = new Set(statementsOf(policy).map(canon));
  return statementsOf(doc).flatMap((st, index) => {
    const kind = classify(st, owner);
    return kind ? [{ index, sid: typeof st.Sid === "string" ? st.Sid : null, kind, stored: have.has(canon(st)) }] : [];
  });
}
const label = (f) => `Statement ${f.index + 1}${f.sid ? ` (${f.sid})` : ""}`;

function kindOf(sid) {
  if (sid.startsWith("bucketeer-share-")) return "Sharing";
  if (sid.startsWith("bucketeer-key-")) return "Bucket key";
  return "Managed";
}

export async function policyDialog({ bucket }) {
  const id = `pol${++seq}`;
  let state;
  try {
    state = await getBucketPolicy(bucket);
  } catch (err) {
    return { error: err };
  }
  let changed = false;
  let busy = false;
  let loadedText = pretty(state.policy);

  const error = h("p", { class: "qd-error", role: "alert", hidden: true });
  const showError = (m) => { error.textContent = m; error.hidden = !m; };
  const status = h("p", { class: "pol-status", role: "status" });

  const guardNew = h("ul", { class: "pol-guard-list" });
  const guardOld = h("ul", { class: "pol-guard-list" });
  const guard = h("section", { class: "pol-guard", hidden: true, "aria-live": "polite" },
    h("div", { class: "pol-banner-h" }, icon("alert", "icon"), h("b", { text: "This policy cannot be saved" })),
    guardNew,
    h("p", { text: "Bucketeer does not allow adding or changing statements that make a bucket public or lock you out. Remove or fix them to save. You can still do this with the AWS CLI and your own keys, but public buckets are not supported here." }));
  const keep = h("section", { class: "pol-banner", hidden: true },
    h("div", { class: "pol-banner-h" }, icon("alert", "icon"), h("b", { text: "Risky statements already on this bucket" })),
    guardOld,
    h("p", { text: "These can be kept exactly as they are or removed, but not changed." }));
  function paintGuard(doc) {
    const found = doc ? screen(doc, state.policy, state.owner) : [];
    const line = (f) => h("li", {}, `${label(f)} ${REASONS[f.kind]}`);
    guardNew.replaceChildren(...found.filter((f) => !f.stored).map(line));
    guardOld.replaceChildren(...found.filter((f) => f.stored).map(line));
    guard.hidden = !guardNew.children.length;
    keep.hidden = !guardOld.children.length;
    return guardNew.children.length > 0;
  }

  const managedList = h("ul", { class: "pol-managed" });
  const managed = h("section", { class: "pol-banner", hidden: true, "aria-labelledby": `${id}-mh` },
    h("div", { class: "pol-banner-h", id: `${id}-mh` }, icon("alert", "icon"),
      h("b", { text: "Some of these statements are managed by Bucketeer" })),
    h("p", { text: "Sharing and bucket keys write the statements below. Changing or removing them can cut off people you shared with and keys you created. You will be asked to confirm." }),
    managedList);
  function paintManaged() {
    managedList.replaceChildren(...state.managedSids.map((sid) =>
      h("li", {}, h("code", { text: sid }), h("span", { class: "chip", text: kindOf(sid) }))));
    managed.hidden = !state.managedSids.length;
  }

  // The editor: a line-number gutter next to a textarea that does not wrap (long lines scroll).
  const gutter = h("pre", { class: "pol-gutter", "aria-hidden": "true" });
  const area = h("textarea", {
    class: "pol-text", id: `${id}-text`, spellcheck: false, autocapitalize: "off", autocomplete: "off",
    wrap: "off", rows: 16, "aria-label": `Bucket policy of ${bucket}, JSON`, "aria-describedby": `${id}-err`,
  });
  area.value = loadedText;
  const syntax = h("p", { class: "set-err", id: `${id}-err`, role: "alert", hidden: true });
  const jump = h("button", { class: "btn btn-sm pol-jump", type: "button", hidden: true, text: "Go to error" });
  const editor = h("div", { class: "pol-editor" }, gutter, area);

  function paintGutter() {
    const n = area.value.split("\n").length;
    if (gutter.dataset.n !== String(n)) {
      gutter.dataset.n = String(n);
      gutter.textContent = Array.from({ length: n }, (_, i) => i + 1).join("\n");
    }
    gutter.scrollTop = area.scrollTop;
  }

  let errPos = null;
  // Returns the parsed object, or undefined (and shows where the JSON is wrong).
  function parse(show = true) {
    const text = area.value;
    let doc;
    try {
      doc = JSON.parse(text);
    } catch (err) {
      errPos = where(text, err);
      if (show) {
        syntax.textContent = errPos ? `Not valid JSON at line ${errPos.line}, column ${errPos.col}.` : "Not valid JSON.";
      }
      syntax.hidden = !show;
      jump.hidden = !show || !errPos;
      area.setAttribute("aria-invalid", "true");
      return undefined;
    }
    syntax.hidden = true;
    jump.hidden = true;
    area.removeAttribute("aria-invalid");
    if (doc === null || typeof doc !== "object" || Array.isArray(doc)) {
      syntax.textContent = "A bucket policy must be a JSON object.";
      syntax.hidden = false;
      return undefined;
    }
    return doc;
  }
  jump.addEventListener("click", () => {
    if (!errPos) return;
    const lines = area.value.split("\n");
    let pos = 0;
    for (let i = 0; i < errPos.line - 1; i += 1) pos += lines[i].length + 1;
    pos += Math.max(0, errPos.col - 1);
    area.focus();
    area.setSelectionRange(pos, Math.min(pos + 1, area.value.length));
  });

  const save = h("button", { class: "btn btn-primary", type: "submit" }, icon("check"), "Save policy");
  const cancel = h("button", { class: "btn", type: "button", text: "Close" });
  const format = h("button", { class: "btn btn-sm", type: "button", text: "Format" });
  const revert = h("button", { class: "btn btn-sm", type: "button", text: "Revert" });
  const del = h("button", { class: "btn btn-danger btn-sm pol-del", type: "button" }, icon("trash"), "Delete policy");

  const dirty = () => area.value !== loadedText;
  function update() {
    const parsed = parse(false);
    const ok = parsed !== undefined;
    const blocked = paintGuard(parsed);
    save.disabled = busy || !dirty() || !ok || blocked;
    revert.disabled = busy || !dirty();
    del.disabled = busy || state.policy === null;
    format.disabled = busy;
    paintGutter();
  }
  area.addEventListener("input", () => {
    showError("");
    conflict.hidden = true;
    const ok = parse(false) !== undefined;
    if (ok) syntax.hidden = true;
    update();
  });
  area.addEventListener("scroll", () => { gutter.scrollTop = area.scrollTop; });
  area.addEventListener("blur", () => { if (area.value.trim()) parse(true); });

  format.addEventListener("click", () => {
    const doc = parse(true);
    if (doc === undefined) return;
    area.value = JSON.stringify(doc, null, 2);
    update();
  });
  revert.addEventListener("click", () => { area.value = loadedText; showError(""); syntax.hidden = true; conflict.hidden = true; update(); });

  // Their version vs mine, after another change landed.
  const mine = h("textarea", { class: "pol-text pol-mine", readonly: true, rows: 6, "aria-label": "Your unsaved text" });
  const copyMine = h("button", { class: "btn btn-sm", type: "button" }, icon("copy"), "Copy my text");
  const conflict = h("section", { class: "pol-conflict", hidden: true, role: "alert" },
    h("b", { text: "The policy changed while you were editing" }),
    h("p", { text: "A share, key or another edit was saved in the meantime. The editor now shows the current policy; your text is kept below so nothing is lost." }),
    mine, copyMine);
  copyMine.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(mine.value); toast.show("Copied your text."); } catch { toast.error("Could not copy. Select the text and copy it."); }
  });

  function adopt(view, { keepMine = false } = {}) {
    if (keepMine) { mine.value = area.value; conflict.hidden = false; conflict.scrollIntoView({ block: "nearest" }); }
    state = { policy: view.policy, managedSids: view.managedSids, etag: view.etag, owner: view.owner };
    loadedText = pretty(view.policy);
    area.value = loadedText;
    syntax.hidden = true;
    paintManaged();
    update();
  }

  function busyUi(on, label) {
    busy = on;
    save.replaceChildren(...(on ? [h("span", { class: "spinner", "aria-hidden": "true" }), label] : [icon("check"), "Save policy"]));
    update();
  }

  const sidList = (sids) => h("ul", { class: "pol-confirm-list" }, sids.map((s) => h("li", {}, h("code", { text: s }))));

  // Run one write; handles the two 409 answers. Returns the response or null.
  async function write(send, describe) {
    showError("");
    busyUi(true, "Saving…");
    try {
      let res;
      try {
        res = await send(false);
      } catch (err) {
        if (err.status === 409 && err.body?.etagMismatch) { adopt(err.body.current, { keepMine: true }); return null; }
        if (!(err.status === 409 && err.body?.requiresConfirmation)) throw err;
        busyUi(false);
        const ok = await confirmDialog({
          title: describe.title, danger: true, confirmLabel: describe.confirm,
          message: h("span", {}, describe.lead, sidList(err.body.managedChanges)),
        });
        if (!ok) return null;
        busyUi(true, "Saving…");
        res = await send(true);
      }
      return res;
    } catch (err) {
      if (err.status === 401) { dlg.close(); return null; }
      if (err.status === 409 && err.body?.etagMismatch) { adopt(err.body.current, { keepMine: true }); return null; }
      showError(err.message || "Could not save the policy.");
      return null;
    } finally {
      busyUi(false);
    }
  }

  async function onSave() {
    const doc = parse(true);
    if (doc === undefined || busy || save.disabled) return;
    const res = await write((confirmManaged) => putBucketPolicy(bucket, { policy: doc, etag: state.etag, confirmManaged }), {
      title: ["Change managed statements of ", nameSpan(bucket), "?"], confirm: "Change them anyway",
      lead: "This edit changes statements that Bucketeer manages. Shares and keys that rely on them may stop working or show as out of sync:",
    });
    if (!res) return;
    changed = changed || res.changed;
    adopt(res);
    toast.show(res.changed ? `Policy saved for ${bucket}.` : "Nothing to change.");
    if (res.policy === null) toast.show("The policy has no statements, so it was removed.", { timeout: 7000 });
  }

  async function onDelete() {
    const warn = state.managedSids.length
      ? h("span", {}, "This also removes the statements Bucketeer manages for sharing and bucket keys. People you shared with lose access, shares show as out of sync, and bucket keys stop working until you share again or create new keys:", sidList(state.managedSids))
      : "Everything the policy allowed or denied stops applying. Only the bucket owner keeps access unless other permissions apply.";
    const ok = await confirmDialog({
      title: ["Delete the policy of ", nameSpan(bucket), "?"], danger: true, confirmLabel: "Delete policy", message: warn,
    });
    if (!ok) return;
    const res = await write((confirmManaged) => deleteBucketPolicy(bucket, { etag: state.etag, confirmManaged }), {
      title: ["Remove managed statements of ", nameSpan(bucket), "?"], confirm: "Delete policy",
      lead: "The policy holds statements that Bucketeer manages:",
    });
    if (!res) return;
    changed = changed || res.changed;
    adopt(res);
    toast.show(`Policy of ${bucket} deleted.`);
  }
  del.addEventListener("click", onDelete);

  const form = h("form", { class: "pol-form", novalidate: true },
    managed, keep,
    h("div", { class: "pol-bar" },
      h("label", { class: "pol-lbl", for: `${id}-text`, text: "Policy (JSON)" }),
      h("div", { class: "pol-tools" }, format, revert)),
    editor, syntax, jump, guard, conflict, error, status,
    h("div", { class: "cd-actions pol-actions" }, del, h("span", { class: "pol-spacer" }), cancel, save));
  form.addEventListener("submit", (e) => { e.preventDefault(); onSave(); });

  const dlg = h("dialog", { class: "cd qd pol", "aria-labelledby": `${id}-t`, "aria-describedby": `${id}-m` },
    h("div", { class: "pol-body" },
      h("div", { class: "qd-head" },
        h("h2", { id: `${id}-t` }, "Policy of ", nameSpan(bucket)),
        h("p", { class: "cd-msg", id: `${id}-m`, text: state.policy === null ? "This bucket has no policy yet. Add statements to allow or deny access." : "Edit the raw JSON that decides who may do what with this bucket. This is an Advanced tool: a wrong policy can lock people out." })),
      form));
  paintManaged();
  update();
  cancel.addEventListener("click", () => dlg.close());
  dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });

  return new Promise((resolve) => {
    dlg.addEventListener("close", () => { dlg.remove(); resolve({ changed }); });
    document.body.append(dlg);
    dlg.showModal();
    area.focus();
    area.setSelectionRange(0, 0);
    area.scrollTop = 0;
    paintGutter();
  });
}
