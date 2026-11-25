// Storage location store: "onprem" or "cloud" (Phase 6). Like advanced.js: in memory,
// the server is the only persistence (/api/me `locationChoice`, `defaultLocation`,
// `cloudAvailable`; saved with PUT /api/profile {location}). No localStorage.
//
// The effective location is the user's own choice only while Advanced is on and cloud
// is available; otherwise the admin's default (and on premises when cloud is not
// available). The server computes the same (bucketeer/location.py).
import { saveProfile } from "./api.js";
import { subscribe as onAdvanced } from "./advanced.js";

let choice = null;          // the user's saved choice, null = follow the default
let fallback = "onprem";    // the admin's default
let available = false;      // cloud configured and its connection check passed
let current = "onprem";
const subs = new Set();

const compute = () => {
  const loc = choice || fallback;  // the saved choice always applies; Advanced only unlocks the switch
  return loc === "cloud" && !available ? "onprem" : loc;
};

function emit() {
  const next = compute();
  const changed = next !== current;
  current = next;
  for (const fn of [...subs]) fn(current, { changed });
}

export const getLocation = () => current;
export const cloudAvailable = () => available;
export const locationChoice = () => choice;

// fn(location, {changed}) also runs when only availability changes (to show/hide the switch).
export function subscribe(fn) {
  subs.add(fn);
  return () => subs.delete(fn);
}

onAdvanced(() => emit());

// Called once per login with the /api/me body.
export function initLocation(me) {
  choice = me.locationChoice ?? null;
  fallback = me.defaultLocation === "cloud" ? "cloud" : "onprem";
  available = !!me.cloudAvailable;
  confirmed = choice;
  emit();
}

// Optimistic and serialized like setAdvanced: the last save to settle wins, a failure
// rolls back to the last server-confirmed choice.
let confirmed = null;
let latest = 0;
let chain = Promise.resolve();

export function setLocation(v) {
  const seq = ++latest;
  choice = v;
  emit();
  const run = async () => {
    try {
      const res = await saveProfile({ location: v });
      confirmed = res.locationChoice ?? null;
      available = !!res.cloudAvailable;
      fallback = res.defaultLocation === "cloud" ? "cloud" : "onprem";
      if (seq === latest) { choice = confirmed; emit(); }
    } catch (err) {
      if (seq === latest) { choice = confirmed; emit(); }
      throw err;
    }
  };
  const p = chain.then(run, run);
  chain = p.catch(() => {});
  return p;
}
