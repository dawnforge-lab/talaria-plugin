import { createPrivateKey, createSign } from 'node:crypto';
import { readFileSync } from 'node:fs';

// Android phones, through Firebase Cloud Messaging's HTTP v1 API. Every push
// is a data message, never an FCM `notification`: the app decrypts the
// envelope and posts the notification itself.

const DEFAULT_FCM_ORIGIN = 'https://fcm.googleapis.com';
const DEFAULT_TOKEN_URI = 'https://oauth2.googleapis.com/token';
const FCM_SCOPE = 'https://www.googleapis.com/auth/firebase.messaging';
// Bound for one call to Google: a peer that accepts the connection and then
// stalls must not hold the event request open.
const SEND_TIMEOUT_MS = 10_000;
// An access token lasts an hour; a fresh one is fetched this long before.
const TOKEN_REFRESH_MARGIN_MS = 5 * 60_000;
// FCM caps a message's data at 4096 bytes, keys and values together.
export const FCM_MAX_DATA_BYTES = 4096;
// What an Android phone registers: a Firebase installation id (FID), which
// firebase-messaging 25.1+ registers with in place of the deprecated token,
// or an older SDK's registration token. Both are URL-safe text.
const FCM_TOKEN_PATTERN = /^[A-Za-z0-9_:-]{20,512}$/;
// A FID is 22 base64url characters; a registration token is far longer and
// has a colon. Each goes in its own field of the message.
const FID_PATTERN = /^[A-Za-z0-9_-]{22}$/;
const PACKAGE_PATTERN = /^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$/;
// Send errors after which the token will never work for this relay's
// Firebase project: the app was uninstalled or its token rotated, or the
// token belongs to another project. INVALID_ARGUMENT also covers a message
// the relay built wrong, so it drops the token only when Google names the
// token as the bad field.
const TOKEN_GONE = new Set(['UNREGISTERED', 'NOT_FOUND', 'SENDER_ID_MISMATCH']);
const TOKEN_FIELDS = new Set(['message.token', 'message.fid']);

export function validateFcmToken(value) {
  const token = String(value ?? '');
  if (!FCM_TOKEN_PATTERN.test(token)) {
    const error = new Error('invalid_device_token');
    error.status = 400;
    throw error;
  }
  return token;
}

// The relay's Google settings. FCM_SERVICE_ACCOUNT_PATH is the Firebase
// service account's key file and FCM_PACKAGES the app ids allowed to
// register. FCM_ORIGIN, and a key file whose token_uri isn't HTTPS, are test
// seams for a fake Google.
export function fcmConfigFromEnv(env = process.env) {
  const path = env.FCM_SERVICE_ACCOUNT_PATH?.trim();
  const packages = env.FCM_PACKAGES?.trim();
  if (!path) throw new Error('FCM_SERVICE_ACCOUNT_PATH is required.');
  if (!packages) throw new Error('FCM_PACKAGES is required.');
  const names = packages.split(',').map((name) => name.trim()).filter(Boolean);
  if (!names.length || names.some((name) => !PACKAGE_PATTERN.test(name))) {
    throw new Error('FCM_PACKAGES must be a comma-separated list of Android package names.');
  }
  const testSeams = env.RELAY_TEST_SEAMS === '1';
  let origin;
  if (env.FCM_ORIGIN !== undefined) {
    if (!testSeams) throw new Error('FCM_ORIGIN is a test setting; unset it for a real relay.');
    origin = new URL(env.FCM_ORIGIN).toString().replace(/\/$/, '');
  }
  return { serviceAccountPath: path, packages: new Set(names), origin, allowInsecure: testSeams };
}

export class FcmClient {
  constructor({ serviceAccount, origin, allowInsecure = false, fetch = globalThis.fetch, now = Date.now }) {
    if (serviceAccount?.type !== 'service_account') throw new Error('The FCM key file is not a service account key.');
    for (const field of ['project_id', 'client_email', 'private_key']) {
      if (typeof serviceAccount[field] !== 'string' || !serviceAccount[field]) throw new Error(`The FCM key file has no ${field}.`);
    }
    const tokenUri = new URL(serviceAccount.token_uri || DEFAULT_TOKEN_URI);
    if (tokenUri.protocol !== 'https:' && !allowInsecure) throw new Error('The FCM key file\'s token_uri must use HTTPS.');
    this.projectId = serviceAccount.project_id;
    this.clientEmail = serviceAccount.client_email;
    this.keyId = serviceAccount.private_key_id;
    this.privateKey = createPrivateKey(serviceAccount.private_key);
    this.tokenUri = tokenUri.toString();
    this.origin = origin || DEFAULT_FCM_ORIGIN;
    this.fetch = fetch;
    this.now = now;
    this.cached = null;
    this.pending = null;
  }

  static fromFile(path, options = {}) {
    return new FcmClient({ ...options, serviceAccount: JSON.parse(readFileSync(path, 'utf8')) });
  }

  // `{ ok, status, reason, tokenGone }`, where `reason` is FCM's error code
  // and `tokenGone` says the token will never work again. A transport
  // failure throws.
  async send(token, message) {
    let result = await this.post(token, message, await this.accessToken());
    // A token Google revoked early: fetch a new one and try once more.
    if (result.status === 401) {
      this.cached = null;
      result = await this.post(token, message, await this.accessToken());
    }
    return result;
  }

  async post(token, message, accessToken) {
    const response = await this.fetch(`${this.origin}/v1/projects/${encodeURIComponent(this.projectId)}/messages:send`, {
      method: 'POST',
      headers: { authorization: `Bearer ${accessToken}`, 'content-type': 'application/json; charset=utf-8' },
      body: JSON.stringify({ message: { [FID_PATTERN.test(token) ? 'fid' : 'token']: token, ...message } }),
      signal: AbortSignal.timeout(SEND_TIMEOUT_MS),
    });
    const text = await response.text();
    if (response.ok) return { ok: true, status: response.status, reason: null, tokenGone: false };
    let error = null;
    try { error = JSON.parse(text)?.error ?? null; } catch { error = null; }
    const details = Array.isArray(error?.details) ? error.details : [];
    const fcmError = details.find((detail) => detail?.['@type'] === 'type.googleapis.com/google.firebase.fcm.v1.FcmError');
    const reason = fcmError?.errorCode ?? error?.status ?? null;
    const badToken = details.some((detail) => detail?.['@type'] === 'type.googleapis.com/google.rpc.BadRequest'
      && Array.isArray(detail.fieldViolations)
      && detail.fieldViolations.some((violation) => TOKEN_FIELDS.has(violation?.field)));
    return {
      ok: false,
      status: response.status,
      reason,
      tokenGone: TOKEN_GONE.has(reason) || (reason === 'INVALID_ARGUMENT' && badToken),
    };
  }

  // One exchange at a time: sends that arrive meanwhile wait for its token.
  async accessToken() {
    if (this.cached && this.now() < this.cached.refreshAt) return this.cached.token;
    this.pending ??= this.exchange().finally(() => { this.pending = null; });
    return this.pending;
  }

  async exchange() {
    const issuedAt = Math.floor(this.now() / 1000);
    const header = base64url(JSON.stringify({ alg: 'RS256', typ: 'JWT', ...(this.keyId ? { kid: this.keyId } : {}) }));
    const claims = base64url(JSON.stringify({ iss: this.clientEmail, scope: FCM_SCOPE, aud: this.tokenUri, iat: issuedAt, exp: issuedAt + 3600 }));
    const signer = createSign('RSA-SHA256');
    signer.update(`${header}.${claims}`);
    signer.end();
    const assertion = `${header}.${claims}.${signer.sign(this.privateKey).toString('base64url')}`;
    const response = await this.fetch(this.tokenUri, {
      method: 'POST',
      headers: { 'content-type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ grant_type: 'urn:ietf:params:oauth:grant-type:jwt-bearer', assertion }).toString(),
      signal: AbortSignal.timeout(SEND_TIMEOUT_MS),
    });
    let body = null;
    try { body = await response.json(); } catch { body = null; }
    // Google's own error text, never the assertion: it names the key's problem.
    if (!response.ok || typeof body?.access_token !== 'string') {
      throw new Error(`fcm access token refused (${response.status}${body?.error ? `: ${body.error}` : ''})`);
    }
    const lifetimeMs = Math.max(Number(body.expires_in) || 3600, 60) * 1000;
    this.cached = {
      token: body.access_token,
      refreshAt: this.now() + Math.max(lifetimeMs - TOKEN_REFRESH_MARGIN_MS, lifetimeMs / 2),
    };
    return this.cached.token;
  }
}

// A push the relay built, `{ data, urgent, expiration }`, as the FCM message
// for the app with this package name. Every data value is already a string,
// as FCM requires. Urgent pushes are what the user acts on or what setup
// waits for: they wake the phone. The rest may wait for its next wake-up.
// No collapse key: FCM keeps only four per device, and the app replaces a
// chat's older notification itself.
export function fcmMessage({ data, urgent = true, expiration }, packageName, nowSeconds = Math.floor(Date.now() / 1000)) {
  const ttl = expiration === undefined ? undefined : Math.max(0, expiration - nowSeconds);
  return {
    data,
    android: {
      priority: urgent ? 'HIGH' : 'NORMAL',
      restrictedPackageName: packageName,
      ...(ttl === undefined ? {} : { ttl: `${ttl}s` }),
    },
  };
}

// What FCM counts against FCM_MAX_DATA_BYTES: keys and values, in UTF-8.
export function dataBytes(data) {
  let total = 0;
  for (const [key, value] of Object.entries(data)) total += Buffer.byteLength(key) + Buffer.byteLength(value);
  return total;
}

function base64url(value) {
  return Buffer.from(value).toString('base64url');
}
