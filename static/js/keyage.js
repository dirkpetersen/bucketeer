import { h } from "./dom.js";

// How old an access key is, for the key lists (own keys, subusers, cloud keys). The server
// sends per key `ageDays`, `ageKnownSince`, `ageExact` (false: Bucketeer first saw the key
// then, so the key is at least that old) and `rotateSoon`, and `maxKeyAgeDays` (null = the
// administrators switched key age reminders off).

const fmtDate = (iso) => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleDateString(undefined, { dateStyle: "medium" });
};
const days = (n) => `${n.toLocaleString()} ${n === 1 ? "day" : "days"}`;

/** "12 days old", "at least 12 days old (first seen Nov 1, 2026)" or "" when unknown. */
export function ageText(key) {
  if (key?.ageDays == null) return "";
  if (key.ageExact) return `${days(key.ageDays)} old`;
  const seen = fmtDate(key.ageKnownSince);
  return `at least ${days(key.ageDays)} old${seen ? ` (first seen ${seen})` : ""}`;
}

/** The "Rotate soon" chip for a key past the limit, else null. */
export function rotateSoonChip(key, maxDays) {
  if (!key?.rotateSoon) return null;
  return h("span", {
    class: "chip chip-warn key-age-chip",
    title: maxDays ? `Older than ${maxDays} days. Rotate it to replace it with a new key.` : null,
  }, h("span", { class: "dot" }), "Rotate soon");
}

/** Fill `el` (a `.key-age` line) for `key`; hidden while the age is unknown. */
export function paintKeyAge(el, key, maxDays) {
  const text = ageText(key);
  el.hidden = !text;
  el.replaceChildren(h("span", { class: "acct-sub", text: text ? `Age: ${text}` : "" }));
  const chip = rotateSoonChip(key, maxDays);
  if (chip) el.append(chip);
  return el;
}

/** A new "Age: …" line with the chip (repaint it with `paintKeyAge` when the key changes). */
export function keyAge(key, maxDays) {
  return paintKeyAge(h("p", { class: "key-age" }), key, maxDays);
}
