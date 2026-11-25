import { h } from "../dom.js";
import { icon } from "../icons.js";
import { bucketOptions, setOwnBucketQuota } from "../api.js";
import { nameSpan } from "./confirm-dialog.js";
import { formatMoney, quotaPicker } from "./quota-picker.js";

// bucketQuotaDialog({ bucket, current, options?, save?, product? }) -> Promise<{ error? } | { saved }>
// `options`/`save` default to the on-premises calls; cloud buckets pass their own and the
// bucket's product (billed on usage: the cost shown is an estimate if the bucket is full).
// "Change quota…": the bucket's quota in whole TiB with its monthly cost shown live. An increase
// counts against the weekly limit (the server answers 409 with what is left); lowering always works.
let seq = 0;

export async function bucketQuotaDialog({ bucket, current, options = bucketOptions, save = setOwnBucketQuota, product = null }) {
  const id = `bq${++seq}`;
  let opts;
  try { opts = await options(); } catch (err) { return { error: err }; }
  let saved = null;
  let busy = false;

  const picker = quotaPicker({ id, value: current ?? opts.quota.defaultTiB, info: opts.quota, product: product || opts.product, current });
  const error = h("p", { class: "qd-error", role: "alert", hidden: true });
  const show = (msg) => { error.textContent = msg; error.hidden = !msg; };
  const cancel = h("button", { class: "btn", type: "button", text: "Cancel" });
  const submit = h("button", { class: "btn btn-primary", type: "submit" }, icon("check"), "Save quota");
  const prod = product || opts.product;
  const price = !prod ? ""
    : prod.billedOn === "usage"
      ? `${prod.name} is about ${formatMoney(Number(prod.pricePerTiBMonth) || 0, prod.currency)} per TiB and month; the real AWS cost is billed. AWS does not stop uploads at the quota: you get an email when the bucket is over it.`
      : `${prod.name} costs ${formatMoney(Number(prod.pricePerTiBMonth) || 0, prod.currency)} per TiB and month, billed on the quota you reserve.`;

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
      saved = await save(bucket, value);
      dlg.close();
    } catch (err) {
      if (err.status === 401) { dlg.close(); return; }
      if (err.status === 409 && err.body?.remainingTiB != null) {
        picker.update({ remainingTiB: err.body.remainingTiB, nextReleaseAt: err.body.freesUpAt ?? null });
      }
      // The limit note under the field already explains a weekly-limit refusal.
      show(picker.over() ? "" : err.message || "Could not change the quota.");
      if (picker.over()) picker.input.focus();
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
