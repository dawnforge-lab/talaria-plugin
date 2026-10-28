import { strict as assert } from 'node:assert';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { RelayStore } from '../src/store.mjs';
import { TokenChallenges, tokenProofModeFromEnv } from '../src/token-proof.mjs';

const TOKEN = 'a'.repeat(64);

test('a nonce works once, for its own token, before it expires', () => {
  let now = 0;
  const challenges = new TokenChallenges({ ttlMs: 1_000, now: () => now });
  const nonce = challenges.issue(TOKEN);
  assert.equal(challenges.verify('b'.repeat(64), nonce), false);
  assert.equal(challenges.verify(TOKEN, nonce), true);
  assert.equal(challenges.verify(TOKEN, nonce), false);

  const late = challenges.issue(TOKEN);
  now = 1_000;
  assert.equal(challenges.verify(TOKEN, late), false);
  challenges.sweep();
  assert.equal(challenges.count, 0);
});

test('the last few nonces for a token stay valid, and using one retires the rest', () => {
  const challenges = new TokenChallenges({ maxPerToken: 3 });
  const nonces = [1, 2, 3, 4].map(() => challenges.issue(TOKEN));
  assert.equal(challenges.count, 3);
  assert.equal(challenges.verify(TOKEN, nonces[0]), false, 'pushed out by newer ones');
  assert.equal(challenges.verify(TOKEN, nonces[2]), true);
  assert.equal(challenges.verify(TOKEN, nonces[3]), false);
  assert.equal(challenges.count, 0);
});

test('a full table forgets the oldest token', () => {
  const challenges = new TokenChallenges({ maxPending: 2 });
  const first = challenges.issue('1'.repeat(64));
  challenges.issue('2'.repeat(64));
  challenges.issue('3'.repeat(64));
  assert.equal(challenges.count, 2);
  assert.equal(challenges.verify('1'.repeat(64), first), false);
});

test('malformed proofs are refused without a lookup', () => {
  const challenges = new TokenChallenges();
  challenges.issue(TOKEN);
  for (const proof of [undefined, null, 42, '', 'short', 'A'.repeat(44), `${'A'.repeat(42)}!`]) {
    assert.equal(challenges.verify(TOKEN, proof), false);
  }
});

test('token proof is required unless set to optional', () => {
  assert.equal(tokenProofModeFromEnv({}), 'required');
  assert.equal(tokenProofModeFromEnv({ RELAY_TOKEN_PROOF: '' }), 'required');
  assert.equal(tokenProofModeFromEnv({ RELAY_TOKEN_PROOF: 'optional' }), 'optional');
  assert.throws(() => tokenProofModeFromEnv({ RELAY_TOKEN_PROOF: 'off' }), /RELAY_TOKEN_PROOF/);
});

test('a full relay reclaims the longest-inactive installation, never an active one', () => {
  const dir = mkdtempSync(join(tmpdir(), 'talaria-relay-reclaim-'));
  try {
    const store = new RelayStore(join(dir, 'relay.json'), { maxInstallations: 3 });
    const make = (token) => store.createInstallation({ bundleId: 'lab.dawnforge.hermes', deviceToken: token.repeat(64) }).installation.id;
    const older = make('1');
    const newer = make('2');
    const active = make('3');
    store.data.installations[older].updatedAt = '2026-01-01T00:00:00.000Z';
    store.createPairing(older);
    store.deactivateInstallation(newer);
    store.data.installations[older].active = false;

    const fourth = make('4');
    assert.equal(store.data.installations[older], undefined);
    assert.ok(store.data.installations[newer]);
    assert.ok(!Object.values(store.data.pairings).some((pairing) => pairing.installationId === older));

    const reclaimed = [];
    store.onReclaim = (id) => reclaimed.push(id);
    make('5');
    assert.equal(store.data.installations[newer], undefined);
    assert.deepEqual(reclaimed, [newer]);
    assert.throws(() => make('6'), /installation_limit_reached/);
    assert.ok(store.data.installations[active] && store.data.installations[fourth]);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('withdrawing a nonce whose push failed keeps the earlier ones', () => {
  const challenges = new TokenChallenges();
  const delivered = challenges.issue(TOKEN);
  const failed = challenges.issue(TOKEN);
  challenges.withdraw(TOKEN, failed);
  assert.equal(challenges.count, 1);
  assert.equal(challenges.verify(TOKEN, failed), false);
  assert.equal(challenges.verify(TOKEN, delivered), true);
});
