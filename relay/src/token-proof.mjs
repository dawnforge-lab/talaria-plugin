import { createHash, randomBytes, timingSafeEqual } from 'node:crypto';

// Proof that a phone holds the FCM token it registers. The phone asks for a
// challenge, the relay pushes a one-time nonce to that token, and only the
// phone holding the token can send it back with the registration (or with a
// device-token change). So a leaked token can't be bound to someone else's
// installation, and filling the relay takes real phones running Talaria,
// not a script.
//
// Challenges live in memory only: a restart just means the phone asks again.

// The challenge push's data key, which the app reads the nonce from.
export const TOKEN_CHALLENGE_KEY = 'talaria_token_challenge';
export const TOKEN_PROOF_MODES = new Set(['required', 'optional']);

const NONCE_PATTERN = /^[A-Za-z0-9_-]{43}$/;

export class TokenChallenges {
  constructor({ maxPending = 20_000, maxPerToken = 3, ttlMs = 10 * 60_000, now = Date.now } = {}) {
    this.maxPending = maxPending;
    this.maxPerToken = maxPerToken;
    this.ttlMs = ttlMs;
    this.now = now;
    // device token -> [{ hash, expiresAt }], oldest first. Map order is
    // issue order, so the first key is the oldest token to forget.
    this.pending = new Map();
    this.count = 0;
  }

  // A fresh nonce for this token. A phone that asked twice may get either
  // push first, so the last few stay valid until one is used.
  issue(deviceToken) {
    this.sweep();
    const nonce = randomBytes(32).toString('base64url');
    const earlier = this.pending.get(deviceToken) ?? [];
    const entries = this.maxPerToken > 1 ? earlier.slice(-(this.maxPerToken - 1)) : [];
    this.forget(deviceToken);
    while (this.count >= this.maxPending && this.pending.size) this.forget(this.pending.keys().next().value);
    entries.push({ hash: hashNonce(nonce), expiresAt: this.now() + this.ttlMs });
    this.pending.set(deviceToken, entries);
    this.count += entries.length;
    return nonce;
  }

  // True once, for a nonce pushed to this token that hasn't expired; using
  // one retires every challenge for the token.
  verify(deviceToken, nonce) {
    if (typeof nonce !== 'string' || !NONCE_PATTERN.test(nonce)) return false;
    const entries = this.pending.get(deviceToken);
    if (!entries) return false;
    const now = this.now();
    const hash = hashNonce(nonce);
    const matched = entries.some((entry) => entry.expiresAt > now && timingSafeEqual(entry.hash, hash));
    if (matched) this.forget(deviceToken);
    return matched;
  }

  // Drops one nonce whose push never went out.
  withdraw(deviceToken, nonce) {
    const entries = this.pending.get(deviceToken);
    if (!entries) return;
    const hash = hashNonce(nonce);
    const kept = entries.filter((entry) => !entry.hash.equals(hash));
    this.count -= entries.length - kept.length;
    if (kept.length) this.pending.set(deviceToken, kept);
    else this.pending.delete(deviceToken);
  }

  sweep() {
    const now = this.now();
    for (const [token, entries] of this.pending) {
      const live = entries.filter((entry) => entry.expiresAt > now);
      if (live.length === entries.length) continue;
      this.count -= entries.length - live.length;
      if (live.length) this.pending.set(token, live);
      else this.pending.delete(token);
    }
  }

  forget(deviceToken) {
    const entries = this.pending.get(deviceToken);
    if (!entries) return;
    this.count -= entries.length;
    this.pending.delete(deviceToken);
  }
}

// The challenge push: nothing to show, so a phone that isn't waiting for it
// ignores it. Urgent, because setup waits for it.
export function tokenChallengePush(nonce, { ttlSeconds, nowSeconds = Math.floor(Date.now() / 1000) }) {
  return { data: { [TOKEN_CHALLENGE_KEY]: nonce }, urgent: true, expiration: nowSeconds + ttlSeconds };
}

export function tokenProofModeFromEnv(env = process.env) {
  const raw = (env.RELAY_TOKEN_PROOF ?? '').trim();
  if (!raw) return 'required';
  if (!TOKEN_PROOF_MODES.has(raw)) throw new Error('RELAY_TOKEN_PROOF must be required or optional.');
  return raw;
}

function hashNonce(nonce) {
  return createHash('sha256').update(nonce).digest();
}
