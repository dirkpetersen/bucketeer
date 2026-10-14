import { h } from "../dom.js";
import { icon } from "../icons.js";
import { getBucketCors, putBucketCors } from "../api.js";
import { toast } from "./toast.js";
import { nameSpan } from "./confirm-dialog.js";

// corsDialog({ bucket }) -> Promise<{ error? } | { changed }>
// Structured editor for the bucket's CORS rules (Advanced only; the server does not care about the
// toggle). CORS grants no access by itself: it only lets browser JavaScript on the listed websites
// read responses the bucket already allows. The etag the rules were loaded with travels with Save,
// so a change made elsewhere shows up as a 409 and the user's draft is kept for comparison.
let seq = 0;
const METHODS = ["GET", "HEAD", "PUT", "POST", "DELETE"];
const WRITE = new Set(["PUT", "POST", "DELETE"]);
const ORIGIN = /^https?:\/\/[A-Za-z0-9*]([A-Za-z0-9.*-]*[A-Za-z0-9*])?(:\d{1,5})?$/; // the form browsers send
const TOKEN = /^[!#$%&'*+.^_`|~0-9A-Za-z-]+$/;
const MAX_RULES = 100;

const PRESETS = [
  { label: "Allow GET from one website", rule: () => ({ allowedOrigins: ["https://www.example.org"], allowedMethods: ["GET", "HEAD"], allowedHeaders: ["*"], exposeHeaders: ["ETag"], maxAgeSeconds: 3000 }) },
  { label: "Allow uploads from one website", rule: () => ({ allowedOrigins: ["https://app.example.org"], allowedMethods: ["GET", "PUT", "POST", "HEAD"], allowedHeaders: ["*"], exposeHeaders: ["ETag"], maxAgeSeconds: 3000 }) },
  { label: "Public read from any website", rule: () => ({ allowedOrigins: ["*"], allowedMethods: ["GET", "HEAD"], allowedHeaders: [], exposeHeaders: [], maxAgeSeconds: 3000 }) },
];

const blank = () => ({ allowedOrigins: [], allowedMethods: ["GET"], allowedHeaders: [], exposeHeaders: [], maxAgeSeconds: null });
const copy = (rules) => JSON.parse(JSON.stringify(rules));
const clean = (r) => ({
  ...(r.id ? { id: r.id } : {}),
  allowedOrigins: r.allowedOrigins, allowedMethods: METHODS.filter((m) => r.allowedMethods.includes(m)),
  allowedHeaders: r.allowedHeaders, exposeHeaders: r.exposeHeaders, maxAgeSeconds: r.maxAgeSeconds ?? null,
});
const sameRules = (a, b) => JSON.stringify(a.map(clean)) === JSON.stringify(b.map(clean));

// A list of removable chips plus a text box: Enter, comma or leaving the box adds the entry.
function chipInput({ label, values, placeholder, check, onChange, mono = true, id }) {
  const list = h("ul", { class: "cors-chips", "aria-label": label });
  const err = h("p", { class: "set-err", role: "alert", hidden: true });
  const input = h("input", { class: "input cors-chip-in", type: "text", id, placeholder, autocomplete: "off", autocapitalize: "off", spellcheck: false, "aria-label": label });
  const paint = () => {
    list.replaceChildren(...values.map((v, i) => h("li", { class: "cors-chip" },
      h("span", { class: mono ? "cors-chip-t mono" : "cors-chip-t", text: v }),
      h("button", { class: "cors-chip-x", type: "button", "aria-label": `Remove ${v}`, on: { click: () => { values.splice(i, 1); paint(); onChange(); input.focus(); } } }, icon("x")))));
    list.hidden = !values.length;
  };
  const add = () => {
    const parts = input.value.split(/[,\s]+/).map((s) => s.trim()).filter(Boolean);
    if (!parts.length) return true;
    for (const p of parts) {
      const msg = check(p);
      if (msg) { err.textContent = msg; err.hidden = false; input.setAttribute("aria-invalid", "true"); return false; }
    }
    for (const p of parts) if (!values.includes(p)) values.push(p);
    input.value = "";
    err.hidden = true;
    input.removeAttribute("aria-invalid");
    paint();
    onChange();
    return true;
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === ",") { e.preventDefault(); add(); } else if (e.key === "Backspace" && !input.value && values.length) { values.pop(); paint(); onChange(); }
  });
  input.addEventListener("blur", add);
  input.addEventListener("input", () => { err.hidden = true; input.removeAttribute("aria-invalid"); });
  paint();
  return { el: h("div", { class: "cors-chipbox" }, list, input, err), flush: add };
}

const checkOrigin = (o) => {
  if (/\s/.test(o)) return `"${o}" is not an origin (no spaces).`;
  if ((o.match(/\*/g) || []).length > 1) return "Only one * wildcard is allowed.";
  return null;
};
const checkHeader = (wild) => (v) => (!TOKEN.test(v) ? `"${v}" is not a header name.` : (!wild && v.includes("*")) || (v.match(/\*/g) || []).length > 1 ? "Wildcards are not allowed here (one * at most in allowed headers)." : null);

export async function corsDialog({ bucket }) {
  const id = `cors${++seq}`;
  let state;
  try { state = await getBucketCors(bucket); } catch (err) { return { error: err }; }
  let rules = copy(state.rules);
  let busy = false;
  let changed = false;
  let flushers = [];
  let warnings = state.warnings;

  const error = h("p", { class: "qd-error", role: "alert", hidden: true });
  const showError = (m) => { error.textContent = m; error.hidden = !m; };
  const cards = h("ol", { class: "cors-rules" });
  const jsonView = h("pre", { class: "cors-json", hidden: true, tabindex: "0", "aria-label": "CORS rules as JSON" });
  const jsonBtn = h("button", { class: "btn btn-sm", type: "button", "aria-pressed": "false", text: "View JSON" });
  const addBtn = h("button", { class: "btn btn-sm", type: "button" }, icon("plus"), "Add rule");
  const presetSel = h("select", { class: "input cors-preset", "aria-label": "Start from an example" },
    h("option", { value: "", text: "Add an example…" }), ...PRESETS.map((p, i) => h("option", { value: String(i), text: p.label })));
  const save = h("button", { class: "btn btn-primary", type: "submit" }, icon("check"), "Save rules");
  const revert = h("button", { class: "btn", type: "button", text: "Discard changes" });
  const close = h("button", { class: "btn", type: "button", text: "Close" });
  const mine = h("textarea", { class: "pol-text pol-mine", readonly: true, rows: 6, "aria-label": "Your unsaved rules, JSON" });
  const useMine = h("button", { class: "btn btn-sm", type: "button", text: "Restore my version" });
  const copyMine = h("button", { class: "btn btn-sm", type: "button" }, icon("copy"), "Copy my rules");
  const conflict = h("section", { class: "pol-conflict", hidden: true, role: "alert" },
    h("b", { text: "The rules changed while you were editing" }),
    h("p", { text: "The editor now shows the current rules. Your unsaved version is kept below: restore it to save it over the current rules, or copy it." }),
    mine, h("div", { class: "pol-tools" }, useMine, copyMine));
  let draft = null;
  useMine.addEventListener("click", () => { rules = copy(draft); conflict.hidden = true; render(); });
  copyMine.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(mine.value); toast.show("Copied your rules."); } catch { toast.error("Could not copy. Select the text and copy it."); }
  });
  const empty = h("p", { class: "cors-empty", text: "No CORS rules. Browsers on other websites cannot read this bucket's files with JavaScript." });

  // Wildcard warnings are computed locally so they react while typing; the server repeats them.
  const ruleWarn = (r, n) => {
    const out = [];
    const bad = r.allowedMethods.filter((m) => WRITE.has(m));
    if (r.allowedOrigins.includes("*") && bad.length) {
      out.push(`Rule ${n} lets JavaScript on any website send ${bad.join(", ")} requests to this bucket from a visitor's browser. They still need valid credentials (or a public bucket), but consider listing only the websites you trust.`);
    }
    const odd = r.allowedOrigins.filter((o) => o !== "*" && !ORIGIN.test(o));
    if (odd.length) out.push(`${odd.join(", ")} is not in the form https://example.com (scheme and host). Browsers send both, so this origin may never match.`);
    return out.join(" ");
  };

  const dirty = () => !sameRules(rules, state.rules);
  function update() {
    save.disabled = busy || !dirty();
    revert.disabled = busy || !dirty();
    addBtn.disabled = busy || rules.length >= MAX_RULES;
    presetSel.disabled = addBtn.disabled;
    empty.hidden = rules.length > 0;
    jsonView.textContent = JSON.stringify(rules.map(clean), null, 2);
    rules.forEach((r, i) => {
      const w = ruleWarn(r, i + 1);
      const el = cards.children[i]?.querySelector(".cors-warn");
      if (el) { el.hidden = !w; el.querySelector("span").textContent = w || ""; }
    });
  }

  function ruleCard(r, i) {
    const rid = `${id}-r${i}`;
    const origins = chipInput({ label: `Allowed origins, rule ${i + 1}`, values: r.allowedOrigins, placeholder: "https://example.org", check: checkOrigin, onChange: update, id: `${rid}-o` });
    const headers = chipInput({ label: `Allowed headers, rule ${i + 1}`, values: r.allowedHeaders, placeholder: "* or Content-Type", check: checkHeader(true), onChange: update });
    const expose = chipInput({ label: `Exposed headers, rule ${i + 1}`, values: r.exposeHeaders, placeholder: "ETag", check: checkHeader(false), onChange: update });
    flushers.push(origins.flush, headers.flush, expose.flush);
    const age = h("input", { class: "input cors-age", type: "number", min: 0, max: 2147483647, step: 1, inputmode: "numeric", id: `${rid}-a`, placeholder: "3000", value: r.maxAgeSeconds ?? "" });
    age.addEventListener("input", () => { r.maxAgeSeconds = age.value === "" ? null : Number(age.value); update(); });
    const methods = h("fieldset", { class: "cors-methods" }, h("legend", { text: "Methods" }),
      METHODS.map((m) => {
        const box = h("input", { type: "checkbox", checked: r.allowedMethods.includes(m) });
        box.addEventListener("change", () => {
          r.allowedMethods = box.checked ? [...r.allowedMethods, m] : r.allowedMethods.filter((x) => x !== m);
          update();
        });
        return h("label", { class: "qd-check cors-method", "data-write": String(WRITE.has(m)) }, box, h("span", { text: m }));
      }));
    const perr = h("p", { class: "set-err cors-rule-err", role: "alert", hidden: true });
    const del = h("button", { class: "btn btn-ghost btn-sm", type: "button", "aria-label": `Remove rule ${i + 1}` }, icon("trash"), "Remove");
    del.addEventListener("click", () => { rules.splice(i, 1); render(); });
    return h("li", { class: "cors-rule" },
      h("div", { class: "cors-rule-h" }, h("b", { text: `Rule ${i + 1}` }), r.id ? h("span", { class: "chip", text: r.id }) : null, h("span", { class: "pol-spacer" }), del),
      h("div", { class: "field" }, h("label", { for: `${rid}-o`, text: "Websites allowed (origins)" }), origins.el,
        h("small", { class: "cors-hint", text: `Type an origin and press Enter. One * wildcard is allowed, for example https://*.example.org, or * for any website.` })),
      methods,
      h("div", { class: "cors-grid" },
        h("div", { class: "field" }, h("label", { text: "Allowed request headers" }), headers.el),
        h("div", { class: "field" }, h("label", { text: "Response headers shown to scripts" }), expose.el)),
      h("div", { class: "field cors-agef" }, h("label", { for: `${rid}-a`, text: "Browser may cache the answer (seconds)" }), age),
      h("p", { class: "lcd-tip acl-warn cors-warn", role: "note", hidden: true }, icon("alert", "icon"), h("span")),
      perr);
  }

  function render() {
    flushers = [];
    cards.replaceChildren(...rules.map(ruleCard));
    showError("");
    update();
  }

  function showFieldErrors(errors) {
    const general = [];
    const perRule = new Map();
    for (const e of errors) {
      if (e.rule && cards.children[e.rule - 1]) perRule.set(e.rule, [...(perRule.get(e.rule) || []), e.message]);
      else general.push(e.message);
    }
    for (const [n, msgs] of perRule) {
      const slot = cards.children[n - 1].querySelector(".cors-rule-err");
      slot.replaceChildren(...msgs.map((m) => h("span", { class: "cors-err-line", text: m })));
      slot.hidden = false;
    }
    if (general.length) showError(general.join(" "));
  }

  jsonBtn.addEventListener("click", () => {
    const on = jsonView.hidden;
    jsonView.hidden = !on;
    cards.hidden = on;
    jsonBtn.setAttribute("aria-pressed", String(on));
    jsonBtn.textContent = on ? "Back to editor" : "View JSON";
  });
  addBtn.addEventListener("click", () => { rules.push(blank()); render(); cards.lastElementChild.querySelector("input")?.focus(); });
  presetSel.addEventListener("change", () => {
    const p = PRESETS[Number(presetSel.value)];
    presetSel.value = "";
    if (!p) return;
    rules.push(p.rule());
    render();
    cards.lastElementChild.scrollIntoView({ block: "nearest" });
  });
  revert.addEventListener("click", () => { rules = copy(state.rules); conflict.hidden = true; render(); });

  const form = h("form", { class: "pol-form cors-form", novalidate: true },
    conflict,
    h("p", { class: "lcd-tip", role: "note" }, icon("shield", "icon"),
      h("span", { text: "CORS does not make anything public. It only lets browser scripts on the websites you list read answers the bucket already gives, such as public files or presigned links." })),
    h("div", { class: "pol-bar" }, h("div", { class: "pol-tools" }, addBtn, presetSel), h("div", { class: "pol-tools" }, jsonBtn)),
    empty, cards, jsonView, error,
    h("div", { class: "cd-actions pol-actions" }, revert, h("span", { class: "pol-spacer" }), close, save));

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (busy) return;
    for (const f of flushers) if (!f()) return;
    if (save.disabled) return;
    busy = true;
    showError("");
    cards.querySelectorAll(".cors-rule-err").forEach((n) => { n.hidden = true; n.replaceChildren(); });
    save.replaceChildren(h("span", { class: "spinner", "aria-hidden": "true" }), "Saving…");
    update();
    try {
      const res = await putBucketCors(bucket, { rules: rules.map(clean), etag: state.etag });
      state = res;
      rules = copy(res.rules);
      changed = changed || res.changed;
      render();
      toast.show(res.changed ? (res.rules.length ? `CORS rules saved for ${bucket}.` : `CORS rules of ${bucket} removed.`) : "Nothing to change.");
      for (const w of res.warnings) toast.show(w.message, { timeout: 9000 });
    } catch (err) {
      if (err.status === 401) { dlg.close(); return; }
      if (err.status === 409 && err.body?.etagMismatch) {
        draft = rules.map(clean);
        mine.value = JSON.stringify(draft, null, 2);
        state = err.body.current;
        rules = copy(state.rules);
        conflict.hidden = false;
        render();
      } else if (err.status === 422 && err.body?.errors) {
        showFieldErrors(err.body.errors);
        if (!err.body.errors.some((x) => x.rule)) return;
        showError("Fix the marked rules and save again.");
      } else showError(err.message || "Could not save the rules.");
    } finally {
      busy = false;
      save.replaceChildren(icon("check"), "Save rules");
      update();
    }
  });

  const dlg = h("dialog", { class: "cd qd pol cors", "aria-labelledby": `${id}-t`, "aria-describedby": `${id}-m` },
    h("div", { class: "pol-body" },
      h("div", { class: "qd-head" },
        h("h2", { id: `${id}-t` }, "CORS rules of ", nameSpan(bucket)),
        h("p", { class: "cd-msg", id: `${id}-m`, text: "Decide which websites may use this bucket from a visitor's browser. This is an Advanced tool." })),
      form));
  render();
  close.addEventListener("click", () => dlg.close());
  dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });

  return new Promise((resolve) => {
    dlg.addEventListener("close", () => { dlg.remove(); resolve({ changed }); });
    document.body.append(dlg);
    dlg.showModal();
    close.focus();
  });
}
