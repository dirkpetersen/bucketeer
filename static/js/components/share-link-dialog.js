import { h } from "../dom.js";
import { icon } from "../icons.js";
import { createShareLink } from "../api.js";
import { keyPicker } from "./key-picker.js";
import { copyText } from "../clipboard.js";
import { toast } from "./toast.js";
import { nameSpan } from "./confirm-dialog.js";

// shareLinkDialog({ bucket }) -> Promise<void>
// Pick one file by typing its key (prefix autocomplete from a read-only, one-page listing:
// this is not a file browser), pick how long the link lasts (7 days at most), generate a
// presigned download link. The link is kept in memory only, never in storage or attributes,
// and is dropped when the key or the duration changes or the dialog closes.
let seq = 0;
const EXPIRIES = [
  { label: "1 hour", seconds: 3600 },
  { label: "1 day", seconds: 86400 },
  { label: "7 days", seconds: 604800 },
];

export function shareLinkDialog({ bucket }) {
  const id = `sl${++seq}`;
  let busy = false;
  let link = null; // { url, expiresAt } while shown

  const error = h("p", { class: "qd-error", role: "alert", hidden: true });
  const showError = (m) => { error.textContent = m; error.hidden = !m; };

  const picker = keyPicker({
    id,
    bucket,
    onChange: () => { showError(""); resetLink(); syncGo(); },
    onError: showError,
    onUnauthorized: () => dlg.close(),
  });
  const input = picker.input;
  const select = h("select", { class: "input slk-exp", id: `${id}-exp`, "aria-label": "Link lasts" },
    EXPIRIES.map((e, i) => h("option", { value: String(e.seconds), selected: i === 1, text: e.label })));
  const go = h("button", { class: "btn btn-primary", type: "submit", disabled: true }, icon("check"), "Create link");
  const close = h("button", { class: "btn", type: "button", text: "Close" });

  const resultWrap = h("div", { class: "slk-result", hidden: true });

  function syncGo() { go.disabled = busy || input.value.trim() === "" || input.value.endsWith("/"); }
  function resetLink() {
    link = null;
    resultWrap.hidden = true;
    resultWrap.replaceChildren();
  }

  select.addEventListener("change", resetLink);

  function showLink(res) {
    link = res;
    const when = new Date(res.expiresAt);
    const field = h("input", {
      class: "input slk-url", id: `${id}-url`, type: "text", readonly: true, spellcheck: "false",
      "aria-label": "Share link",
    });
    field.value = res.url; // a property, never an attribute
    field.addEventListener("click", () => field.select());
    const copy = h("button", { class: "btn btn-sm", type: "button" }, icon("copy"), h("span", { text: "Copy" }));
    copy.addEventListener("click", async () => {
      const ok = await copyText(link.url);
      toast[ok ? "show" : "error"](ok ? "Link copied." : "Could not copy. Select the link and copy it by hand.");
    });
    resultWrap.replaceChildren(
      h("label", { class: "slk-lbl", for: field.id, text: "Your link" }),
      h("div", { class: "slk-url-row" }, field, copy),
      h("p", { class: "slk-expiry" }, "Works until ",
        h("time", { datetime: res.expiresAt, text: Number.isNaN(when.getTime()) ? res.expiresAt : when.toLocaleString(undefined, { dateStyle: "full", timeStyle: "short" }) }),
        ". Anyone who has the link can download this file until then."));
    resultWrap.hidden = false;
  }

  const form = h("form", { class: "slk-form", novalidate: true },
    h("div", { class: "field" },
      h("label", { for: input.id, text: "File name (key)" }),
      ...picker.nodes),
    h("div", { class: "field" },
      h("label", { for: select.id, text: "Link works for" }), select),
    h("p", { class: "lcd-tip", role: "note" }, icon("alert", "icon"),
      "The link is signed with your access key. It stops working when that key is rotated or deleted, or when your account is suspended."),
    error, resultWrap,
    h("div", { class: "cd-actions" }, close, go));

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (busy || go.disabled) return;
    busy = true;
    showError("");
    resetLink();
    picker.close();
    go.disabled = true;
    go.replaceChildren(h("span", { class: "spinner", "aria-hidden": "true" }), "Creating…");
    try {
      showLink(await createShareLink(bucket, input.value, Number(select.value)));
      resultWrap.querySelector("button").focus();
    } catch (err) {
      if (err.status === 401) { dlg.close(); return; }
      showError(err.message || "Could not create the link.");
    } finally {
      busy = false;
      go.replaceChildren(icon("check"), "Create link");
      syncGo();
    }
  });

  const dlg = h("dialog", { class: "cd qd lcd slk", "aria-labelledby": `${id}-t`, "aria-describedby": `${id}-m` },
    h("div", { class: "lcd-body" },
      h("div", { class: "qd-head" },
        h("h2", { id: `${id}-t` }, "Share a file from ", nameSpan(bucket)),
        h("p", { class: "cd-msg", id: `${id}-m`, text: "Create a temporary download link for one file. Links last 7 days at most." })),
      form));
  close.addEventListener("click", () => dlg.close());
  dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });

  return new Promise((resolve) => {
    dlg.addEventListener("close", () => {
      picker.dispose();
      link = null;
      dlg.remove();
      resolve();
    });
    document.body.append(dlg);
    dlg.showModal();
    input.focus();
  });
}
