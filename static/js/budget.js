// Instant feedback for a budget number; the server (budget.py) is authoritative and
// may also apply an administrator-defined format. Returns "" when fine.
export const BUDGET_MAX_LEN = 64;
const ALLOWED = /^[A-Za-z0-9 +\-=._:/@]+$/;

export function checkBudget(raw) {
  const v = raw.trim();
  if (!v) return "";
  if (v.length > BUDGET_MAX_LEN) return `At most ${BUDGET_MAX_LEN} characters.`;
  if (/[\p{Cc}\p{Cf}]/u.test(v)) return "Control characters are not allowed.";
  if (!ALLOWED.test(v)) return "Use only letters (A-Z), digits, spaces and the characters + - = . _ : / @";
  return "";
}
