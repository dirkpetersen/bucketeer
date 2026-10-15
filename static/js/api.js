// The ONLY way the frontend talks to /api. It always sends the CSRF header
// (`X-Requested-With: bucketeer`, required on state-changing requests) and the
// session cookie, and turns failures into ApiError. A 401 on any call except
// login announces "bucketeer:unauthorized" so app.js can show the login view.

export class ApiError extends Error {
  constructor(status, message, { retryAfter = null, body = null } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.retryAfter = retryAfter; // seconds, from Retry-After on 429
    this.body = body; // the parsed JSON error body (extra fields such as requiresConfirmation)
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
  throw new ApiError(res.status, detail, { retryAfter: Number.isFinite(ra) ? ra : null, body });
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
// Partial update: pass only the fields to change ({defaultBudgetNumber}, {advancedMode}).
export const saveProfile = (fields) => api("/profile", { method: "PUT", json: fields });

// The signed-in user's own buckets. list -> {buckets: [{name, tenant, createdAt, objects, bytes,
// quota, versioning, budgetNumber, budgetMissing, tagPending}]}; options -> {naming, placements,
// defaultPlacement, defaultBudgetNumber, budgetRequired, budgetExample}; create -> {name, ..., warning?}.
// A delete of a non-empty bucket without confirmName fails with 409 and err.body.requiresConfirmation.
export const listBuckets = () => api("/buckets");
export const bucketOptions = () => api("/buckets/options");
export const createBucket = (body) => api("/buckets", { method: "POST", json: body });
export const reconcileBuckets = () => api("/buckets/reconcile", { method: "POST" });
export const setBucketVersioning = (name, enabled) =>
  api(`/buckets/${encodeURIComponent(name)}/versioning`, { method: "PUT", json: { enabled } });
// Simple lifecycle rules (loaded on demand). get -> {expireCurrentDays, expireNoncurrentDays,
// abortMultipartDays (days or null), foreignRules, versioning}. put sends all three (null removes)
// plus confirm:true for enabling or shortening expireCurrentDays (else 409 requiresConfirmation).
export const getBucketLifecycle = (name) => api(`/buckets/${encodeURIComponent(name)}/lifecycle`);
export const setBucketLifecycle = (name, rules) =>
  api(`/buckets/${encodeURIComponent(name)}/lifecycle`, { method: "PUT", json: rules });
// Full lifecycle editor (Advanced). get -> {rules: [{id, status, filter: {prefix?, tags?, sizeGreaterThan?,
// sizeLessThan?}, expiration, noncurrentExpiration, abortMultipartDays, transitions (read only), managed}],
// etag, warnings, versioning}. put sends {rules, etag, confirmManaged?}; 422 carries errors
// [{rule, field, message}], 409 {etagMismatch, current} or {requiresConfirmation, managedChanges}.
export const getLifecycleRules = (name) => api(`/buckets/${encodeURIComponent(name)}/lifecycle/rules`);
export const putLifecycleRules = (name, body) =>
  api(`/buckets/${encodeURIComponent(name)}/lifecycle/rules`, { method: "PUT", json: body });
// Share links. objects (read-only autocomplete, one page, NOT a browser) -> {prefix, entries:
// [{key, size, lastModified}], prefixes: [{prefix}], truncated}. shareLink -> {url, expiresAt};
// expiresIn is 60..604800 seconds. The url is a secret-bearing link: keep it in memory only.
export const listBucketObjects = (name, prefix, limit = 20) =>
  api(`/buckets/${encodeURIComponent(name)}/objects?prefix=${encodeURIComponent(prefix)}&limit=${limit}`);
export const createShareLink = (name, key, expiresIn) =>
  api(`/buckets/${encodeURIComponent(name)}/share-link`, { method: "POST", json: { key, expiresIn } });
export const deleteBucket = (name, confirmName) =>
  api(`/buckets/${encodeURIComponent(name)}`, { method: "DELETE", json: confirmName ? { confirmName } : {} });

// Administrator endpoints (the server enforces the role on each of them).
const enc = encodeURIComponent;
export const adminUsers = () => api("/admin/users");
export const adminBuckets = () => api("/admin/buckets");
export const adminBucketKeys = (state = "all") => api(`/admin/bucket-keys?state=${enc(state)}`);
// Audit log viewer. `params` are the filter fields (actor, action, target_type, target_contains, since, until, before_id, limit).
export const auditQuery = (params = {}) => {
  const q = new URLSearchParams(Object.entries(params).filter(([, v]) => v != null && v !== ""));
  return q.toString();
};
export const adminAudit = (params) => api(`/admin/audit?${auditQuery(params)}`);
export const adminAuditFacets = () => api("/admin/audit/facets");
export const auditExportUrl = (params) => `/api/admin/audit/export.csv?${auditQuery(params)}`;
export const adminRevokeBucketKey = (id) => api(`/admin/bucket-keys/${Number(id)}`, { method: "DELETE" });
export const setUserQuota = (uid, quota) =>
  api(`/admin/users/${enc(uid)}/quota`, { method: "PUT", json: quota });
export const setBucketQuota = (bucket, quota) =>
  api(`/admin/buckets/${enc(bucket)}/quota`, { method: "PUT", json: quota });
export const getOffboarding = () => api("/admin/offboarding");
export const runOffboarding = () => api("/admin/offboarding/run", { method: "POST" });
export const saveOffboardingSettings = (body) => api("/admin/offboarding/settings", { method: "PUT", json: body });
export const suspendUser = (uid) => api(`/admin/users/${enc(uid)}/suspend`, { method: "POST" });
export const unsuspendUser = (uid) => api(`/admin/users/${enc(uid)}/unsuspend`, { method: "POST" });

// Admin settings (naming rules, placement, default quota, uid mappings, notification endpoints).
export const getSettings = () => api("/admin/settings");
export const saveNaming = (rules) => api("/admin/settings/naming", { method: "PUT", json: rules });
export const testNaming = (body) => api("/admin/settings/naming/test", { method: "POST", json: body });
export const saveBudgetNumber = (rules) => api("/admin/settings/budget-number", { method: "PUT", json: rules });
export const testBudgetNumber = (body) => api("/admin/settings/budget-number/test", { method: "POST", json: body });
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

// Bucket keys: S3 keys that work for one bucket only (own buckets; the server checks).
// list -> {keys: [{id, label, access: read|readwrite, uid, accessKey, bucket, createdAt, suspended,
// status: active|suspended|pending|failed}]}; reveal -> {accessKey, secretKey} (audited).
export const listBucketKeys = (bucket) => api(`/buckets/${encodeURIComponent(bucket)}/keys`);
export const createBucketKey = (bucket, label, access) =>
  api(`/buckets/${encodeURIComponent(bucket)}/keys`, { method: "POST", json: { label, access } });
export const revealBucketKey = (id) => api(`/bucket-keys/${Number(id)}/reveal`, { method: "POST" });
export const revokeBucketKey = (id) => api(`/bucket-keys/${Number(id)}`, { method: "DELETE" });

// Sharing a bucket with people. lookup -> {username, displayName, isYou} (exact match, rate
// limited); shares -> {shares: [{id, recipient, displayName, uid, access, createdAt, status}], checked}.
const shareBase = (bucket) => `/buckets/${encodeURIComponent(bucket)}/shares`;
export const lookupShareRecipient = (bucket, q) => api(`/buckets/${encodeURIComponent(bucket)}/share-lookup?q=${encodeURIComponent(q)}`);
export const listBucketShares = (bucket) => api(shareBase(bucket));
export const createBucketShare = (bucket, login, access) => api(shareBase(bucket), { method: "POST", json: { login, access } });
export const updateBucketShare = (bucket, id, access) => api(`${shareBase(bucket)}/${encodeURIComponent(id)}`, { method: "PUT", json: { access } });
export const revokeBucketShare = (bucket, id) => api(`${shareBase(bucket)}/${encodeURIComponent(id)}`, { method: "DELETE" });
export const sharedWithMe = () => api("/shared-with-me");
// Raw bucket policy editor (Advanced). get -> {policy|null, managedSids, etag}. put sends the
// parsed policy object + the etag it was loaded with (+ confirmManaged:true after a 409
// requiresConfirmation {managedChanges}; a 409 etagMismatch carries {current}). del removes the policy.
export const getBucketPolicy = (name) => api(`/buckets/${encodeURIComponent(name)}/policy`);
export const putBucketPolicy = (name, body) =>
  api(`/buckets/${encodeURIComponent(name)}/policy`, { method: "PUT", json: body });
export const deleteBucketPolicy = (name, body) =>
  api(`/buckets/${encodeURIComponent(name)}/policy`, { method: "DELETE", json: body });

// ACLs (Advanced). get -> {scope, key?, owner, grants: [{kind: owner|user|unknown|group, id, uri, email,
// name, permission, scope: public|signed_in|logs|null, removable}], etag, permissions}. put sends
// {etag, keep: [{id|uri|email, permission}], add: [{login, permission}]}; a 409 etagMismatch carries
// {current}; adding group (public) grants is refused with 422. Objects are chosen by key.
const aclBase = (bucket, key) => key == null
  ? `/buckets/${encodeURIComponent(bucket)}/acl`
  : `/buckets/${encodeURIComponent(bucket)}/objects/acl?key=${encodeURIComponent(key)}`;
export const getAcl = (bucket, key) => api(aclBase(bucket, key));
export const putAcl = (bucket, key, body) => api(aclBase(bucket, key), { method: "PUT", json: body });

// CORS rules (Advanced). get -> {rules: [{id?, allowedOrigins, allowedMethods, allowedHeaders,
// exposeHeaders, maxAgeSeconds|null}], etag, warnings: [{rule, message}]}. put sends {rules, etag}
// (an empty list deletes the configuration); a 422 body has errors: [{rule, field, message}], a 409
// etagMismatch carries {current}.
export const getBucketTags = (name) => api(`/buckets/${encodeURIComponent(name)}/tags`);
// body: {tags: [{key, value}] (user tags only), etag}; 422 carries errors [{tag, field, message}],
// 409 {etagMismatch, current}.
export const putBucketTags = (name, body) =>
  api(`/buckets/${encodeURIComponent(name)}/tags`, { method: "PUT", json: body });
// {budgetNumber: string|null} -> {name, budgetNumber, changed, tagSynced, warning?}
export const setBucketBudget = (name, budgetNumber) =>
  api(`/buckets/${encodeURIComponent(name)}/budget`, { method: "PUT", json: { budgetNumber } });
// Bucket notifications (Advanced). get -> {notifications: [{id, endpoint|null, managed, kind, topic,
// events, prefix|null, suffix|null}], endpoints: [{name, type}], etag}. put sends {notifications, etag}:
// managed {id, endpoint, events, prefix?, suffix?}, external kept as {id, external: true}. A 422 body has
// errors [{item, field, message}], a 409 etagMismatch carries {current}.
export const getBucketNotifications = (name) => api(`/buckets/${encodeURIComponent(name)}/notifications`);
export const putBucketNotifications = (name, body) =>
  api(`/buckets/${encodeURIComponent(name)}/notifications`, { method: "PUT", json: body });
export const getBucketCors = (name) => api(`/buckets/${encodeURIComponent(name)}/cors`);
export const putBucketCors = (name, body) =>
  api(`/buckets/${encodeURIComponent(name)}/cors`, { method: "PUT", json: body });

// Object lock (Advanced). Default retention: get -> {enabled, defaultRetention: {mode, days|years}|null,
// etag}; put sends {defaultRetention|null, etag, confirm?}; a COMPLIANCE default answers 409
// {requiresConfirmation} until confirm is true, a stale etag 409 {etagMismatch, current}.
export const getObjectLockDefault = (name) => api(`/buckets/${encodeURIComponent(name)}/object-lock`);
export const putObjectLockDefault = (name, body) =>
  api(`/buckets/${encodeURIComponent(name)}/object-lock`, { method: "PUT", json: body });
// One object, chosen by key (versionId optional = latest). get -> {key, versionId, retention:
// {mode, retainUntil}|null, legalHold: "ON"|"OFF"}. Retention put sends {mode|null, retainUntil,
// confirm?, bypassGovernance?}; 409 carries requiresConfirmation / requiresBypass / locked.
const lockQuery = (key, versionId) =>
  `key=${encodeURIComponent(key)}${versionId ? `&versionId=${encodeURIComponent(versionId)}` : ""}`;
export const getObjectLock = (name, key, versionId) =>
  api(`/buckets/${encodeURIComponent(name)}/objects/lock?${lockQuery(key, versionId)}`);
export const putObjectRetention = (name, key, versionId, body) =>
  api(`/buckets/${encodeURIComponent(name)}/objects/retention?${lockQuery(key, versionId)}`, { method: "PUT", json: body });
export const putObjectLegalHold = (name, key, versionId, status) =>
  api(`/buckets/${encodeURIComponent(name)}/objects/legal-hold?${lockQuery(key, versionId)}`, { method: "PUT", json: { status } });

// Unfinished multipart uploads (Advanced). params: {prefix, keyMarker, uploadIdMarker, limit} ->
// {uploads: [{key, uploadId, initiated, initiator?, storageClass?}], truncated, nextKeyMarker,
// nextUploadIdMarker, totalShown}. One S3 page per call; no part counts or sizes.
export const listMultipartUploads = (name, params = {}) => {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v) q.set(k, String(v));
  const qs = q.toString();
  return api(`/buckets/${encodeURIComponent(name)}/multipart${qs ? `?${qs}` : ""}`);
};
// body: {uploads: [{key, uploadId}]} or {olderThanDays, prefix?, confirm?}. Without confirm the
// older-than form answers 409 {requiresConfirmation, count, truncated} and writes nothing.
// 200 -> {aborted, failed: [{key, uploadId, error}], notFound, truncated}.
export const abortMultipartUploads = (name, body) =>
  api(`/buckets/${encodeURIComponent(name)}/multipart/abort`, { method: "POST", json: body });
