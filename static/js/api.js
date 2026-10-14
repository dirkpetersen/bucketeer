// The ONLY way the frontend talks to /api. It always sends the CSRF header
// (`X-Requested-With: bucketeer`, required on state-changing requests) and the
// session cookie, and turns failures into ApiError. A 401 on any call except
// login announces "bucketeer:unauthorized" so app.js can show the login view.

export class ApiError extends Error {
  constructor(status, message, { retryAfter = null } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.retryAfter = retryAfter; // seconds, from Retry-After on 429
  }
}

export async function api(path, { method = "GET", json, signal, announce401 = true } = {}) {
  const headers = { "X-Requested-With": "bucketeer", Accept: "application/json" };
  const init = { method, headers, credentials: "same-origin", signal };
  if (json !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(json);
  }
  let res;
  try {
    res = await fetch(`/api${path}`, init);
  } catch (err) {
    if (err.name === "AbortError") throw err;
    throw new ApiError(0, "Cannot reach the server. Check your connection.");
  }
  let body = null;
  try {
    body = await res.json();
  } catch {
    /* empty or non-JSON body */
  }
  if (res.ok) return body;
  if (res.status === 401 && announce401) {
    window.dispatchEvent(new CustomEvent("bucketeer:unauthorized"));
  }
  const detail = typeof body?.detail === "string" ? body.detail : `Request failed (${res.status}).`;
  const ra = Number.parseInt(res.headers.get("Retry-After") ?? "", 10);
  throw new ApiError(res.status, detail, { retryAfter: Number.isFinite(ra) ? ra : null });
}

// {demo: bool}; true only when mock/demo.py wrapped the app (never in production).
export const getDemo = () => api("/demo", { announce401: false });
export const getMe = () => api("/me", { announce401: false });
export const login = (loginName, password) =>
  api("/login", { method: "POST", json: { login: loginName, password }, announce401: false });
export const logout = () => api("/logout", { method: "POST", announce401: false });

// The signed-in user's RGW account: {uid, status, reason, message, displayName, quota, created}.
// status: ready | suspended | creation_failed | unavailable. Never carries keys.
export const getAccount = () => api("/account");

// The signed-in user's S3 keys and snippet values: {profile, region, endpointUrl,
// addressingStyle, keys: [{accessKey}]}. No secrets.
export const getCredentials = () => api("/credentials");
// The secret of one of the user's own access keys: {accessKey, secretKey}. Audited
// server-side. Keep the result in memory only, for as short a time as possible.
export const revealSecret = (accessKey) => api("/credentials/reveal", { method: "POST", json: { accessKey } });

// Key management (the server holds the secrets; none of these returns one).
// create -> {accessKey}; rotate -> {accessKey, oldKeyRemoved, message?}; delete -> {accessKey}.
export const createKey = () => api("/credentials/keys", { method: "POST" });
export const rotateKey = (accessKey) => api(`/credentials/keys/${enc(accessKey)}/rotate`, { method: "POST" });
export const deleteKey = (accessKey) => api(`/credentials/keys/${enc(accessKey)}`, { method: "DELETE" });

// The signed-in user's profile: {username, displayName, email, defaultBudgetNumber|null}.
// Saving "" or null clears the default budget number; a 422 carries the message to show.
export const getProfile = () => api("/profile");
export const saveProfile = (defaultBudgetNumber) => api("/profile", { method: "PUT", json: { defaultBudgetNumber } });

// Administrator endpoints (the server enforces the role on each of them).
const enc = encodeURIComponent;
export const adminUsers = () => api("/admin/users");
export const adminBuckets = () => api("/admin/buckets");
export const setUserQuota = (uid, quota) =>
  api(`/admin/users/${enc(uid)}/quota`, { method: "PUT", json: quota });
export const setBucketQuota = (bucket, quota) =>
  api(`/admin/buckets/${enc(bucket)}/quota`, { method: "PUT", json: quota });
export const suspendUser = (uid) => api(`/admin/users/${enc(uid)}/suspend`, { method: "POST" });
export const unsuspendUser = (uid) => api(`/admin/users/${enc(uid)}/unsuspend`, { method: "POST" });

// Admin settings (naming rules, placement, default quota, uid mappings, notification endpoints).
export const getSettings = () => api("/admin/settings");
export const saveNaming = (rules) => api("/admin/settings/naming", { method: "PUT", json: rules });
export const testNaming = (body) => api("/admin/settings/naming/test", { method: "POST", json: body });
export const savePlacement = (body) => api("/admin/settings/placement", { method: "PUT", json: body });
export const saveDefaultQuota = (quota) => api("/admin/settings/default-quota", { method: "PUT", json: quota });
export const clearDefaultQuota = () => api("/admin/settings/default-quota", { method: "DELETE" });
export const saveUidMapping = (ad, rgwUid) =>
  api(`/admin/settings/uid-mappings/${enc(ad)}`, { method: "PUT", json: { rgwUid } });
export const deleteUidMapping = (ad) => api(`/admin/settings/uid-mappings/${enc(ad)}`, { method: "DELETE" });
export const saveEndpoint = (name, body) =>
  api(`/admin/settings/notification-endpoints/${enc(name)}`, { method: "PUT", json: body });
export const deleteEndpoint = (name) =>
  api(`/admin/settings/notification-endpoints/${enc(name)}`, { method: "DELETE" });
