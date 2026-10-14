// localStorage for per-viewer conveniences only. Never throws.
export function load(key, fallback = null) {
  try {
    const v = localStorage.getItem(`bucketeer.${key}`);
    return v === null ? fallback : v;
  } catch {
    return fallback;
  }
}
export function save(key, value) {
  try {
    if (value === null) localStorage.removeItem(`bucketeer.${key}`);
    else localStorage.setItem(`bucketeer.${key}`, String(value));
  } catch {
    /* ignore */
  }
}
