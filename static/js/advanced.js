// The global Advanced toggle: one in-memory store. The server value (/api/me
// `advancedMode`, saved per user in user_profiles) is the only persistence; no
// localStorage hint. Gated UI reads isAdvanced() and/or subscribes to changes.
import { saveProfile } from "./api.js";

let value = false;
const subs = new Set();

export const isAdvanced = () => value;

export function subscribe(fn) {
  subs.add(fn);
  return () => subs.delete(fn);
}

function set(v) {
  if (v === value) return;
  value = v;
  for (const fn of [...subs]) fn(value);
}

// Called once per login with the /api/me value.
export const initAdvanced = (v) => { value = confirmed = !!v; for (const fn of [...subs]) fn(value); };

// Optimistic and serialized: saves run one after another, so the server sees them
// in click order. The last save to settle wins: it applies the server's answer, or
// (on failure) rolls back to the last server-confirmed value. Older saves never
// touch the UI while a newer one is pending.
let confirmed = false;
let latest = 0;
let chain = Promise.resolve();

export function setAdvanced(v) {
  const seq = ++latest;
  set(v);
  const run = async () => {
    try {
      const res = await saveProfile({ advancedMode: v });
      confirmed = !!res.advancedMode;
      if (seq === latest) set(confirmed);
    } catch (err) {
      if (seq === latest) set(confirmed);
      throw err;
    }
  };
  const p = chain.then(run, run);
  chain = p.catch(() => {});
  return p;
}
