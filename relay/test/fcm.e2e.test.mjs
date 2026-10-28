import { strict as assert } from 'node:assert';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { after, before, test } from 'node:test';

import {
  DEBUG_PACKAGE, PACKAGE, UNAVAILABLE, UNREGISTERED, api, challenge, envelopeOf, fcmEnv, fcmToken, freePort, pairedPhone,
  registerPhone, registration, relayBootFailure, routingOf, startGoogle, startRelay, stopRelay,
} from '../testing/harness.mjs';

// Phones through a real relay process, against a fake Google (its token
// endpoint and FCM's send endpoint): registration with token proof, pairing,
// and events reaching the phone as FCM data messages.

const dir = mkdtempSync(join(tmpdir(), 'talaria-relay-fcm-'));
const dataPath = join(dir, 'relay-data.json');
let google;
let relay;
let base;

before(async () => {
  google = await startGoogle();
  relay = await startRelay({ DATA_PATH: dataPath, ...fcmEnv(dir, google) });
  base = relay.base;
});

after(async () => {
  await stopRelay(relay);
  await google.close();
  rmSync(dir, { recursive: true, force: true });
});

test('the challenge reaches the phone as an urgent data message for its package', async () => {
  const token = fcmToken();
  const before = google.sends.length;
  const asked = await api(base, '/v1/token-challenges', { method: 'POST', body: registration(token) });
  assert.equal(asked.status, 202);
  assert.deepEqual(asked.json, { challenge_sent: true, expires_in: 600 });
  const [push] = google.sends.slice(before);
  assert.match(push.authorization, /^Bearer access-\d+$/);
  assert.equal(push.message.token, token);
  assert.deepEqual(Object.keys(push.message.data), ['talaria_token_challenge']);
  assert.match(push.message.data.talaria_token_challenge, /^[A-Za-z0-9_-]{43}$/);
  assert.deepEqual(push.message.android, { priority: 'HIGH', restrictedPackageName: PACKAGE, ttl: '600s' });
});

test('a registration is refused for another app, another platform or a malformed token', async () => {
  const token = fcmToken();
  assert.deepEqual((await api(base, '/v1/token-challenges', { method: 'POST', body: registration(token, { bundle_id: 'com.other.app' }) })).json, { error: 'invalid_topic' });
  assert.deepEqual((await api(base, '/v1/token-challenges', { method: 'POST', body: registration(token, { platform: 'apns' }) })).json, { error: 'invalid_platform' });
  assert.deepEqual((await api(base, '/v1/token-challenges', { method: 'POST', body: registration('not a token') })).json, { error: 'invalid_device_token' });
  // `platform` may be left out.
  const { platform: _platform, ...unnamed } = registration(token);
  assert.equal((await api(base, '/v1/token-challenges', { method: 'POST', body: unnamed })).status, 202);
});

test('a phone registers only with the nonce pushed to it through FCM', async () => {
  const token = fcmToken();
  assert.equal((await api(base, '/v1/installations', { method: 'POST', body: registration(token) })).json.error, 'token_proof_required');
  assert.equal((await api(base, '/v1/installations', { method: 'POST', body: registration(token, { token_proof: 'x'.repeat(43) }) })).json.error, 'token_proof_invalid');

  // Another phone's nonce proves nothing for this token.
  const otherNonce = await challenge(base, google, fcmToken());
  assert.equal((await api(base, '/v1/installations', { method: 'POST', body: registration(token, { token_proof: otherNonce }) })).json.error, 'token_proof_invalid');

  const proof = await challenge(base, google, token, { bundle_id: DEBUG_PACKAGE });
  const registered = await api(base, '/v1/installations', { method: 'POST', body: registration(token, { bundle_id: DEBUG_PACKAGE, token_proof: proof }) });
  assert.equal(registered.status, 201, JSON.stringify(registered.json));
  assert.deepEqual(Object.keys(registered.json.installation).sort(), ['active', 'gateways', 'id', 'preferences', 'updated_at']);
});

test('events reach the phone as data messages, sealed content untouched and no readable text stored', async () => {
  const phone = await pairedPhone(base, google);
  const before = google.sends.length;
  const e2e = { v: 1, kid: 'c'.repeat(32), msg: 'response:fcm-sealed-1', iat: Math.floor(Date.now() / 1000), tok: '0123456789abcdef', z: 0, req: '', n: 'AAAAAAAAAAAAAAAA', ct: 'S'.repeat(300) };
  const sealed = await api(base, '/v1/events', { method: 'POST', credential: phone.gateway, body: { event_id: 'response:fcm-sealed-1', type: 'response.ready', e2e } });
  assert.equal(sealed.status, 202, JSON.stringify(sealed.json));
  assert.deepEqual(sealed.json, { accepted: true, delivered: true });

  const plain = await api(base, '/v1/events', {
    method: 'POST',
    credential: phone.gateway,
    body: { event_id: 'approval:fcm-plain-1', type: 'approval.needed', session_id: 'sess-9', profile: 'default', title: 'Delete the drafts folder?', body: 'Hermes wants to run rm -rf drafts/' },
  });
  assert.equal(plain.status, 202);

  const [first, second] = google.sends.slice(before).map((send) => send.message);
  assert.equal(first.token, phone.token);
  assert.deepEqual(envelopeOf(first), e2e);
  assert.equal(first.data.title, 'Response ready');
  assert.deepEqual(routingOf(first), { type: 'response.ready', e2e: 1, gateway_id: phone.gatewayId });
  assert.deepEqual(first.android, { priority: 'HIGH', restrictedPackageName: PACKAGE });

  // Previews are off by default: the plaintext event's own text stays home.
  assert.equal(second.data.title, 'Approval needed');
  assert.equal(JSON.stringify(second).includes('drafts'), false);
  assert.equal(routingOf(second).session_id, 'sess-9');

  // The relay saves on a timer; its data file must never hold message text.
  await new Promise((resolve) => setTimeout(resolve, 5_500));
  const stored = readFileSync(dataPath, 'utf8');
  assert.equal(stored.includes('drafts'), false);
  assert.equal(stored.includes('S'.repeat(300)), false);
});

test('Hermes asking for the location now reaches the phone as an urgent, short-lived data message', async () => {
  const phone = await pairedPhone(base, google);
  const before = google.sends.length;
  const asked = await api(base, '/v1/events', {
    method: 'POST',
    credential: phone.gateway,
    body: { event_id: 'locate:l_fcmLocate0123456789', type: 'location.requested', profile: 'default', locate: { id: 'l_fcmLocate0123456789' } },
  });
  assert.equal(asked.status, 202, JSON.stringify(asked.json));
  assert.deepEqual(asked.json, { accepted: true, delivered: true });
  const [{ message: push }] = google.sends.slice(before);
  assert.equal(push.token, phone.token);
  // No alert: the app answers without showing anything.
  assert.equal(push.data.title, undefined);
  assert.deepEqual(routingOf(push), {
    type: 'location.requested', profile: 'default', locate: { id: 'l_fcmLocate0123456789' }, gateway_id: phone.gatewayId,
  });
  assert.equal(push.android.priority, 'HIGH');
  assert.ok(Number.parseInt(push.android.ttl, 10) <= 60, push.android.ttl);

  // Sealed, the request goes untouched with only the routing beside it.
  const e2e = { v: 1, kid: 'd'.repeat(32), msg: 'locate:l_fcmSealed0123456789', iat: Math.floor(Date.now() / 1000), tok: '0123456789abcdef', z: 0, req: '', n: 'AAAAAAAAAAAAAAAA', ct: 'L'.repeat(120) };
  const sealed = await api(base, '/v1/events', { method: 'POST', credential: phone.gateway, body: { event_id: 'locate:l_fcmSealed0123456789', type: 'location.requested', e2e } });
  assert.equal(sealed.status, 202, JSON.stringify(sealed.json));
  const last = google.sends.at(-1).message;
  assert.deepEqual(envelopeOf(last), e2e);
  assert.deepEqual(routingOf(last), { type: 'location.requested', e2e: 1, gateway_id: phone.gatewayId });
});

test('a send Google fails gives the event id back, so the plugin\'s retry is delivered', async () => {
  const phone = await pairedPhone(base, google);
  google.answers.push(UNAVAILABLE);
  const event = { event_id: 'response:fcm-retry-1', type: 'response.ready', session_id: 'sess-r' };
  const failed = await api(base, '/v1/events', { method: 'POST', credential: phone.gateway, body: event });
  assert.equal(failed.status, 502);
  assert.deepEqual(failed.json, { error: 'push_rejected', reason: 'UNAVAILABLE', status: 503 });
  const retried = await api(base, '/v1/events', { method: 'POST', credential: phone.gateway, body: event });
  assert.equal(retried.status, 202);
  assert.equal(retried.json.delivered, true);
  // A passing failure keeps the phone.
  const update = await api(base, `/v1/installations/${phone.installation.id}`, { method: 'PUT', credential: phone.credential, body: {} });
  assert.equal(update.status, 200);
});

test('a dropped connection to Google is reported as unreachable, and the retry is delivered', async () => {
  const phone = await pairedPhone(base, google);
  google.answers.push('drop');
  const event = { event_id: 'response:fcm-drop-1', type: 'response.ready', session_id: 'sess-d' };
  const failed = await api(base, '/v1/events', { method: 'POST', credential: phone.gateway, body: event });
  assert.equal(failed.status, 502);
  assert.deepEqual(failed.json, { error: 'push_unreachable' });
  assert.equal((await api(base, '/v1/events', { method: 'POST', credential: phone.gateway, body: event })).status, 202);
});

test('a token FCM says is unregistered ends the installation', async () => {
  const phone = await pairedPhone(base, google);
  google.answers.push(UNREGISTERED);
  const gone = await api(base, '/v1/events', { method: 'POST', credential: phone.gateway, body: { event_id: 'response:fcm-gone-1', type: 'response.ready' } });
  assert.equal(gone.status, 502);
  assert.equal(gone.json.reason, 'UNREGISTERED');
  assert.equal((await api(base, `/v1/installations/${phone.installation.id}`, { method: 'PUT', credential: phone.credential, body: {} })).status, 401);
  assert.equal((await api(base, '/v1/events', { method: 'POST', credential: phone.gateway, body: { event_id: 'response:fcm-gone-2', type: 'response.ready' } })).status, 401);
});

test('a phone moves to a rotated FCM token only with its proof', async () => {
  const phone = await registerPhone(base, google);
  const rotated = fcmToken();
  const path = `/v1/installations/${phone.installation.id}`;
  assert.equal((await api(base, path, { method: 'PUT', credential: phone.credential, body: { device_token: rotated } })).json.error, 'token_proof_required');
  assert.equal((await api(base, path, { method: 'PUT', credential: phone.credential, body: { device_token: 'a'.repeat(10) } })).json.error, 'invalid_device_token');
  const moved = await api(base, path, { method: 'PUT', credential: phone.credential, body: { device_token: rotated, token_proof: await challenge(base, google, rotated) } });
  assert.equal(moved.status, 200);
  // A live turn is registered by an activity id the phone made.
  const malformed = await api(base, `${path}/live-activities`, { method: 'PUT', credential: phone.credential, body: { push_token: 'a'.repeat(64), session_ids: ['s'] } });
  assert.equal(malformed.status, 400);
  assert.equal(malformed.json.error, 'invalid_live_activity');
  const activity = await api(base, `${path}/live-activities`, {
    method: 'PUT',
    credential: phone.credential,
    body: { push_token: '3f2b1c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d', session_ids: ['s'] },
  });
  assert.equal(activity.status, 200);
  assert.deepEqual(activity.json, { registered: true });
});

test('the routes only an iPhone used are gone', async () => {
  const phone = await pairedPhone(base, google);
  const gone = [
    ['GET', '/v1/meta', phone.credential],
    ['POST', '/v1/maps/token', phone.gateway],
    ['POST', `/v1/decisions/${'r'.repeat(12)}`, phone.credential],
    ['DELETE', `/v1/installations/${phone.installation.id}/live-activities`, phone.credential],
    ['POST', '/v1/rings/abcdefabcdefabcdef/settled', undefined],
    ['POST', '/v1/watch/grants', phone.gateway],
  ];
  for (const [method, path, credential] of gone) {
    const answer = await api(base, path, { method, credential, body: method === 'GET' ? undefined : {} });
    assert.equal(answer.status, 404, `${method} ${path}`);
  }
});

test('the relay needs its Google settings, and its test seams need the test switch', async () => {
  const key = google.keyFile(dir);
  for (const [env, message] of [
    [{ FCM_PACKAGES: PACKAGE }, /FCM_SERVICE_ACCOUNT_PATH is required\./],
    [{ FCM_SERVICE_ACCOUNT_PATH: key }, /FCM_PACKAGES is required\./],
    [{ ...fcmEnv(dir, google), RELAY_TEST_SEAMS: '' }, /FCM_ORIGIN is a test setting/],
  ]) {
    assert.match(await relayBootFailure({ PORT: String(await freePort()), DATA_PATH: join(dir, 'relay-data-boot.json'), ...env }), message);
  }
});
