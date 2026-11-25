import { h } from "../dom.js";
import { icon } from "../icons.js";
import { bucketOptions, setOwnBucketQuota } from "../api.js";
import { nameSpan } from "./confirm-dialog.js";
import { formatMoney, quotaPicker } from "./quota-picker.js";

// bucketQuotaDialog({ bucket, current }) -> Promise<{ error? } | { saved }>
// "Change quota…": the bucket's quota in whole TiB with its monthly cost shown live. An increase
// counts against the weekly limit (the server answers 409 with what is left); lowering always works.
let seq = 0;

export async function bucketQuotaDialog({ bucket, current }) {
  const id = `bq${++seq}`;
  let opts;
  try { opts = await bucketOptions(); } catch (err) { return { error: err }; }
  let saved = null;
  let busy = false;

  const picker = quotaPicker({ id, value: current ?? opts.quota.defaultTiB, info: opts.quota, product: opts.product, current });
  const error = h("p", { class: "qd-error", role: "alert", hidden: true });
  const show = (msg) => { error.textContent = msg; error.hidden = !msg; };
  const cancel = h("button", { class: "btn", type: "button", text: "Cancel" });
  const submit = h("button", { class: "btn btn-primary", type: "submit" }, icon("check"), "Save quota");
  const price = opts.product
    ? `${opts.product.name} costs ${formatMoney(Number(opts.product.pricePerTiBMonth) || 0, opts.product.currency)} per TiB and month, billed on the quota you reserve.`
    : "";

  const form = h("form", { novalidate: true },
    h("div", { class: "qd-head" },
      h("h2", { id: `${id}-t` }, "Quota of ", nameSpan(bucket)),
      h("p", { class: "cd-msg", id: `${id}-m`, text: `The most this bucket may hold. ${price}` })),
    picker.el,
    error,
    h("div", { class: "cd-actions" }, cancel, submit));
  const dlg = h("dialog", { class: "cd qd cb", "aria-labelledby": `${id}-t`, "aria-describedby": `${id}-m` }, form);

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (busy) return;
    if (picker.validate()) { picker.input.focus(); return; }
    const value = picker.value();
    if (value === current) { dlg.close(); return; }
    show("");
    busy = true;
    submit.disabled = true;
    submit.replaceChildren(h("span", { class: "spinner", "aria-hidden": "true" }), "Saving…");
    try {
      saved = await setOwnBucketQuota(bucket, value);
      dlg.close();
    } catch (err) {
      if (err.status === 401) { dlg.close(); return; }
      if (err.status === 409 && err.body?.remainingTiB != null) {
        picker.update({ remainingTiB: err.body.remainingTiB, nextReleaseAt: err.body.freesUpAt ?? null });
      }
      show(err.message || "Could not change the quota.");
      submit.disabled = false;
      submit.replaceChildren(icon("check"), "Save quota");
    } finally {
      busy = false;
    }
  });
  cancel.addEventListener("click", () => dlg.close());
  dlg.addEventListener("cancel", (e) => { if (busy) e.preventDefault(); });
  dlg.addEventListener("click", (e) => { if (e.target === dlg && !busy) dlg.close(); });

  return new Promise((resolve) => {
    dlg.addEventListener("close", () => { dlg.remove(); resolve({ saved }); });
    document.body.append(dlg);
    dlg.showModal();
    picker.input.focus();
    picker.input.select();
  });
}
