// Human-readable numbers. Binary units, matching how RGW counts quota bytes.
export const UNITS = [["B", 1], ["KiB", 1024], ["MiB", 1024 ** 2], ["GiB", 1024 ** 3], ["TiB", 1024 ** 4], ["PiB", 1024 ** 5]];

export function formatBytes(n) {
  if (!Number.isFinite(n) || n <= 0) return "0 B";
  let i = 0;
  while (i < UNITS.length - 1 && n >= UNITS[i + 1][1]) i++;
  const v = n / UNITS[i][1];
  return `${i === 0 || v >= 100 ? Math.round(v) : v.toFixed(v >= 10 ? 1 : 2).replace(/\.?0+$/, "")} ${UNITS[i][0]}`;
}

export const formatCount = (n) => new Intl.NumberFormat().format(n ?? 0);

// "5 GiB · 1,000 objects", or "No quota" when it is not enforced.
export function quotaText(q) {
  if (!q?.enabled) return "No quota";
  const parts = [];
  if (q.maxSize != null) parts.push(formatBytes(q.maxSize));
  if (q.maxObjects != null) parts.push(`${formatCount(q.maxObjects)} objects`);
  return parts.join(" · ") || "No quota";
}
