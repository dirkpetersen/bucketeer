import { h } from "../dom.js";
import { icon } from "../icons.js";

// Bucket quota in whole TiB with the monthly cost shown live (PLAN 0 and 2).
// quotaPicker({ id, value, info, product, current }) -> { el, input, value(), validate(), focus() }
//   info:    the `quota` block of GET /api/buckets/options
//            { defaultTiB, maxTiB, weeklyLimitTiB, remainingTiB, nextReleaseAt, exempt }
//   product: the catalog product { name, pricePerTiBMonth, currency, billedOn }
//   current: the bucket's quota now (TiB or null), so only an increase counts against the week.
// The server is authoritative for the limit; this only explains it before the user saves.
let seq = 0;

export function formatMoney(amount, currency = "USD") {
  const whole = Number.isInteger(amount);
  try {
    return new Intl.NumberFormat(undefined, {
      style: "currency", currency, minimumFractionDigits: whole ? 0 : 2, maximumFractionDigits: 2,
    }).format(amount);
  } catch {
    return `${currency} ${whole ? amount : amount.toFixed(2)}`;
  }
}

export function formatTiB(n) {
  return `${new Intl.NumberFormat().format(n)} TiB`;
}

export function shortDate(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

// "10 TiB × $5 = $50 per month" as nodes (the total in bold).
export function costLine(tib, product) {
  if (!product || tib == null) return h("span", { text: "No quota set" });
  const price = Number(product.pricePerTiBMonth) || 0;
  const total = Math.round(tib * price * 100) / 100;
  return h("span", { class: "qp-cost-text" },
    `${formatTiB(tib)} × ${formatMoney(price, product.currency)} = `,
    h("b", { text: `${formatMoney(total, product.currency)} per month` }));
}

export function quotaPicker({ id = `qp${++seq}`, value, info, product, current = null }) {
  const max = info?.maxTiB ?? 100000;
  const input = h("input", {
    class: "input mono-input qp-input", type: "number", id: `${id}-q`, min: "1", max: String(max), step: "1",
    inputmode: "numeric", value: String(value ?? info?.defaultTiB ?? 1), "aria-describedby": `${id}-c ${id}-l ${id}-e`,
  });
  const dec = h("button", { class: "btn btn-sm qp-step", type: "button", "aria-label": "1 TiB less" }, icon("minus"));
  const inc = h("button", { class: "btn btn-sm qp-step", type: "button", "aria-label": "1 TiB more" }, icon("plus"));
  const cost = h("p", { class: "qp-cost", id: `${id}-c`, "aria-live": "polite" });
  const lim = h("p", { class: "set-hint qp-limit", id: `${id}-l` });
  const err = h("p", { class: "set-err", id: `${id}-e`, role: "alert", hidden: true });

  const read = () => {
    const v = input.value.trim();
    return /^\d+$/.test(v) ? Number(v) : NaN;
  };
  const message = (n) => {
    if (!Number.isInteger(n) || n < 1 || n > max) return `Enter a whole number of TiB from 1 to ${new Intl.NumberFormat().format(max)}.`;
    return "";
  };
  const paint = () => {
    const n = read();
    const bad = message(n);
    cost.replaceChildren(bad ? h("span", { class: "qp-cost-text", text: "—" }) : costLine(n, product));
    if (product?.billedOn === "usage") cost.append(h("small", { text: " Estimate: the real cost is billed." }));
    dec.disabled = !Number.isInteger(n) || n <= 1;
    inc.disabled = Number.isInteger(n) && n >= max;
    lim.replaceChildren();
    lim.dataset.over = "false";
    if (!info || info.exempt || info.remainingTiB == null) {
      if (info?.exempt) lim.textContent = "As an administrator you are not limited by the weekly quota limit.";
      return;
    }
    const added = Number.isInteger(n) ? n - (current ?? 0) : 0;
    const left = info.remainingTiB;
    if (added > left) {
      lim.dataset.over = "true";
      lim.append(icon("alert", "icon"), h("span", {
        text: `This adds ${formatTiB(added)}, but you can add only ${formatTiB(left)} more this week (limit ${formatTiB(info.weeklyLimitTiB)} in 7 days).${info.nextReleaseAt ? ` More frees up after ${shortDate(info.nextReleaseAt)}.` : ""}`,
      }));
    } else if (added > 0) {
      lim.textContent = `You can add ${formatTiB(left)} of quota this week (limit ${formatTiB(info.weeklyLimitTiB)} in 7 days). Lowering a quota is always possible.`;
    } else if (current != null && Number.isInteger(n) && n < current) {
      lim.textContent = "Lowering a quota is always possible. It does not give back this week's allowance.";
    } else {
      lim.textContent = `You can add ${formatTiB(left)} of quota this week.`;
    }
  };
  const step = (d) => {
    const n = read();
    input.value = String(Math.min(max, Math.max(1, (Number.isInteger(n) ? n : 1) + d)));
    err.hidden = true;
    input.setAttribute("aria-invalid", "false");
    paint();
  };
  dec.addEventListener("click", () => step(-1));
  inc.addEventListener("click", () => step(1));
  input.addEventListener("input", () => { err.hidden = true; input.setAttribute("aria-invalid", "false"); paint(); });
  paint();

  const el = h("div", { class: "field qp" },
    h("label", { for: input.id }, "Quota ", h("span", { class: "cb-opt", text: "(whole TiB, at least 1)" })),
    h("div", { class: "qp-row" }, dec, h("div", { class: "qp-box" }, input, h("span", { class: "qp-unit", "aria-hidden": "true", text: "TiB" })), inc),
    cost, lim, err);

  return {
    el,
    input,
    value: read,
    validate() {
      const m = message(read());
      err.textContent = m;
      err.hidden = !m;
      input.setAttribute("aria-invalid", String(!!m));
      return m;
    },
    error(m) { err.textContent = m; err.hidden = !m; },
    // After a 409 from the server: show its numbers.
    update(newInfo) { info = { ...info, ...newInfo }; paint(); },
  };
}
