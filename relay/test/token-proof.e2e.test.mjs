import { strict as assert } from 'node:assert';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { after, before, test } from 'node:test';
import { RelayStore } from '../src/store.mjs';

import {
  PACKAGE, UNREGISTERED, api as call, challenge, fcmEnv, fcmToken, registration, startGoogle, startRelay, stopRelay,
} from '../testing/harness.mjs';

// Registration proof (#142, #146) through a real relay process: a phone
// registers, or moves to a new device token, only with the nonce the relay
// pushed to that token.

const dir = mkdtempSync(join(tmpdir(), 'talaria-relay-proof-'));
const dataPath = join(dir, 'relay-data.json');
const VICTIM_TOKEN = fcmToken();
let google;
let relay;
let victim;

const api = (path, options) => call(relay.base, path, options);
const proofFor = (deviceToken) => challenge(relay.base, google, deviceToken);

async function registerProven(deviceToken) {
  const proof = await proofFor(deviceToken);
  return api('/v1/installations', { method: 'POST', body: registration(deviceToken, { token_proof: proof }) });
}

// Whether an installation's credential still works.
async function active(credential, id) {
  return (await api(`/v1/installations/${id}`, { method: 'PUT', credential, body: {} })).status === 200;
}

before(async () => {
  // A phone registered before token proof.
  const fixture = new RelayStore(dataPath);
  const created = fixture.createInstallation({ bundleId: PACKAGE, deviceToken: VICTIM_TOKEN });
  victim = { id: created.installation.id, credential: `${created.installation.id}.${created.deviceSecret}` };
  google = await startGoogle();
  relay = await startRelay({ DATA_PATH: dataPath, RELAY_TOKEN_PROOF: '', ...fcmEnv(dir, google) });
});

after(async () => {
  await stopRelay(relay);
  await google.close();
  rmSync(dir, { recursive: true, force: true });
});

test('registering without a proof is refused by default, so a leaked token gains nothing', async () => {
  const leaked = await api('/v1/installations', { method: 'POST', body: registration(VICTIM_TOKEN) });
  assert.equal(leaked.status, 403);
  assert.equal(leaked.json.error, 'token_proof_required');
});

test('a wrong proof is refused', async () => {
  const token = fcmToken();
  await proofFor(token);
  const wrong = await api('/v1/installations', { method: 'POST', body: registration(token, { token_proof: 'A'.repeat(43) }) });
  assert.equal(wrong.status, 403);
  assert.equal(wrong.json.error, 'token_proof_invalid');
});

test('a proven phone registers once per nonce', async () => {
  const token = fcmToken();
  const proof = await proofFor(token);
  const registered = await api('/v1/installations', { method: 'POST', body: registration(token, { token_proof: proof }) });
  assert.equal(registered.status, 201);
  assert.ok(registered.json.credential);
  const replayed = await api('/v1/installations', { method: 'POST', body: registration(token, { token_proof: proof }) });
  assert.equal(replayed.status, 403);
  assert.equal(replayed.json.error, 'token_proof_invalid');
});

test('a nonce proves only the token it was pushed to', async () => {
  const proof = await proofFor(fcmToken());
  const elsewhere = await api('/v1/installations', { method: 'POST', body: registration(VICTIM_TOKEN, { token_proof: proof }) });
  assert.equal(elsewhere.status, 403);
  assert.equal(elsewhere.json.error, 'token_proof_invalid');
});

test('an installation moves to a new device token only with its proof; keeping its token needs none', async () => {
  const own = fcmToken();
  const registered = await registerProven(own);
  const credential = registered.json.credential;
  const id = registered.json.installation.id;

  const same = await api(`/v1/installations/${id}`, { method: 'PUT', credential, body: { device_token: own, preferences: { show_previews: true } } });
  assert.equal(same.status, 200);

  const hijack = await api(`/v1/installations/${id}`, { method: 'PUT', credential, body: { device_token: VICTIM_TOKEN } });
  assert.equal(hijack.status, 403);
  assert.equal(hijack.json.error, 'token_proof_required');

  const next = fcmToken();
  const moved = await api(`/v1/installations/${id}`, { method: 'PUT', credential, body: { device_token: next, token_proof: await proofFor(next) } });
  assert.equal(moved.status, 200);
});

test('installations from before token proof keep working', async () => {
  const update = await api(`/v1/installations/${victim.id}`, { method: 'PUT', credential: victim.credential, body: { device_token: VICTIM_TOKEN, preferences: { show_previews: false } } });
  assert.equal(update.status, 200);
});

test('a proven registration retires the same phone\'s earlier installation', async () => {
  const token = fcmToken();
  const first = await registerProven(token);
  // The app lost its credential (say, a restore) and registers again.
  const second = await registerProven(token);
  assert.equal(second.status, 201);
  assert.equal(await active(first.json.credential, first.json.installation.id), false);
  assert.equal(await active(second.json.credential, second.json.installation.id), true);
});

test("moving to a proven token retires the phone's stale installation that held it", async () => {
  // The phone registered once with token T, then (say, after a restore)
  // registered again with another token, and now moves that installation
  // back to T.
  const token = fcmToken();
  const stale = await registerProven(token);
  const current = await registerProven(fcmToken());
  const moved = await api(`/v1/installations/${current.json.installation.id}`, {
    method: 'PUT', credential: current.json.credential, body: { device_token: token, token_proof: await proofFor(token) },
  });
  assert.equal(moved.status, 200);
  assert.equal(await active(stale.json.credential, stale.json.installation.id), false, 'the stale one is retired');
});

test('challenges for one token are rate limited', async () => {
  const token = fcmToken();
  for (let index = 0; index < 5; index += 1) {
    assert.equal((await api('/v1/token-challenges', { method: 'POST', body: registration(token) })).status, 202);
  }
  const flooded = await api('/v1/token-challenges', { method: 'POST', body: registration(token) });
  assert.equal(flooded.status, 429);
});

test('challenges check the app and token shape before pushing', async () => {
  const before = google.sends.length;
  assert.equal((await api('/v1/token-challenges', { method: 'POST', body: registration(fcmToken(), { bundle_id: 'com.example.other' }) })).status, 400);
  assert.equal((await api('/v1/token-challenges', { method: 'POST', body: registration('not a token') })).status, 400);
  assert.equal(google.sends.length, before);
});

test('a challenge Google says can never reach the token is refused as an invalid token', async () => {
  google.answers.push(UNREGISTERED);
  const refused = await api('/v1/token-challenges', { method: 'POST', body: registration(fcmToken()) });
  assert.equal(refused.status, 400);
  assert.equal(refused.json.error, 'invalid_device_token');
});
