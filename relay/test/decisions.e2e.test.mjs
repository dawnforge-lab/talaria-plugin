import { strict as assert } from 'node:assert';
import { createHash } from 'node:crypto';
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { after, before, test } from 'node:test';
import { FCM_MAX_DATA_BYTES, dataBytes } from '../src/fcm.mjs';
import { RelayStore } from '../src/store.mjs';
import {
  PACKAGE, UNAVAILABLE, api, fcmEnv, fcmRefusal, fcmToken, freePort, pairHost, pairedPhone, registerPhone, registration,
  routingOf, startGoogle, startRelay, stopRelay, waitFor,
} from '../testing/harness.mjs';

// Full clarify answer loop against real relay processes: register a phone,
// pair a gateway, push clarify decision events, answer from the phone, and
// poll from the gateway. Every relay pushes to a fake Google (its token and
// FCM send endpoints, testing/harness.mjs) whose answers each test sets: it
// accepts, refuses with Google's own error, or drops the connection, and it
// records what it accepted, so assertions run on the real FCM messages. One
// more relay runs the REAL FcmClient transport against a closed port — a
// connection refused outright, which the fake's dropped connection
// (accepted, then cut) does not produce — to prove that failure parks the
// decision undeliverable AND leaves the relay process alive. Nothing about
// the HTTP API is mocked.

const dir = mkdtempSync(join(tmpdir(), 'talaria-relay-e2e-'));
const dataPath = join(dir, 'relay-data.json');
const deadDataPath = join(dir, 'relay-data-dead.json');
const capacityDataPath = join(dir, 'relay-data-capacity.json');
const installationCapacityDataPath = join(dir, 'relay-data-installation-capacity.json');
// Google refuses the send: neither a token it calls gone nor a passing
// outage, so the installation stays.
const REFUSED = fcmRefusal(403, 'PERMISSION_DENIED');

const relays = [];
// The fake Google every relay pushes through unless a test brings its own.
let google;
let baseUrl;
let deadBaseUrl;

before(async () => {
  google = await startGoogle();
  const relay = await startRelay({ DATA_PATH: dataPath, ...fcmEnv(dir, google) });
  relays.push(relay);
  baseUrl = relay.base;
  // Nothing listens at the dead relay's FCM origin. Its token exchange still
  // reaches the fake (the key file's token_uri), so the send itself fails.
  // Its phone registers unproven: no challenge could reach it.
  const deadRelay = await startRelay({
    DATA_PATH: deadDataPath,
    RELAY_TOKEN_PROOF: 'optional',
    ...fcmEnv(dir, google, { googleUrl: `http://127.0.0.1:${await freePort()}` }),
  });
  relays.push(deadRelay);
  deadBaseUrl = deadRelay.base;
});

after(async () => {
  // Relays write deferred metadata on SIGTERM, so wait for every exit before
  // removing their data directory.
  await Promise.all(relays.map(stopRelay));
  await google.close();
  rmSync(dir, { recursive: true, force: true });
});

// A relay of its own on `dataFile`, pushing through `through`.
async function relayOn(dataFile, env = {}, { through = google } = {}) {
  const relay = await startRelay({ DATA_PATH: dataFile, ...fcmEnv(dir, through), ...env });
  relays.push(relay);
  return relay;
}

// The pushes `through` accepted for a phone's token, its registration
// challenge left out.
function pushesTo(through, token) {
  return through.sentTo(token).filter((message) => message.data.talaria !== undefined);
}

// A gateway's record as the relay last wrote it to `dataFile`. Plugin state
// is written within the store's save delay (5 s), or at the next full save.
function savedGateway(dataFile, installationId, name) {
  const installation = JSON.parse(readFileSync(dataFile, 'utf8')).installations[installationId];
  return Object.values(installation?.gateways ?? {}).find((gateway) => gateway.name === name);
}

test('authenticated installation updates are rate limited', async () => {
  const isolated = await relayOn(join(dir, 'relay-data-installation-update-budget.json'));
  try {
    const phone = await registerPhone(isolated.base, google);
    let last;
    for (let index = 0; index < 31; index += 1) {
      last = await api(isolated.base, `/v1/installations/${phone.installation.id}`, {
        method: 'PUT', credential: phone.credential, body: { preferences: { enabled: true } },
      });
    }
    assert.equal(last.status, 429);
    assert.equal(last.json.error, 'rate_limited');
  } finally {
    await stopRelay(isolated);
  }
});

test('registration flood stays bounded without consuming authenticated event capacity', async () => {
  const isolatedPath = join(dir, 'relay-data-global-budget.json');
  const fixture = new RelayStore(isolatedPath);
  const created = fixture.createInstallation({ bundleId: PACKAGE, deviceToken: fcmToken() });
  const pairing = fixture.createPairing(created.installation.id);
  const claim = fixture.claimPairing(pairing.code, 'control target');
  fixture.savePendingDecision({ id: 'talaria-push-globalbudget', installationId: created.installation.id, gatewayId: claim.gatewayId, question: 'answer?' });
  // Unproven registrations, as a relay in the token-proof rollout window
  // takes them: every one of the flood is a real write.
  const isolated = await relayOn(isolatedPath, { RELAY_TOKEN_PROOF: 'optional' });
  const base = isolated.base;
  const flooded = fcmToken();
  try {
    let response;
    for (let index = 0; index < 25; index += 1) {
      response = await api(base, '/v1/installations', {
        method: 'POST',
        body: registration(flooded),
        from: `10.${Math.floor(index / 250)}.${Math.floor(index / 250)}.${(index % 250) + 1}`,
      });
      if (index < 24) assert.equal(response.status, 201);
    }
    assert.equal(response.status, 429);
    assert.equal(response.json.error, 'rate_limited');
    const gatewayCredential = `${created.installation.id}.${claim.gatewayId}.${claim.gatewaySecret}`;
    const event = await api(base, '/v1/events', {
      method: 'POST', credential: gatewayCredential,
      body: { event_id: 'plugin:after-registration-flood', type: 'plugin.hello' },
    });
    assert.equal(event.status, 202, 'registration writes must not spend the authenticated event budget');
    assert.equal((await api(base, '/v1/decisions/talaria-push-globalbudget', { credential: gatewayCredential })).json.status, 'pending', 'polls remain usable after registration quota exhaustion');
    const answered = await api(base, '/v1/decisions/talaria-push-globalbudget/respond', {
      method: 'POST',
      credential: `${created.installation.id}.${created.deviceSecret}`,
      body: { answer: 'yes', gateway_id: claim.gatewayId },
    });
    assert.equal(answered.status, 200, 'decision answers use their separate authenticated control budget');
  } finally {
    await stopRelay(isolated);
  }
});

test('invalid and per-client-limited registration or claim requests cannot starve authenticated intake', async () => {
  const isolatedPath = join(dir, 'relay-data-invalid-ingress.json');
  const fixture = new RelayStore(isolatedPath);
  const created = fixture.createInstallation({ bundleId: PACKAGE, deviceToken: fcmToken() });
  const pairing = fixture.createPairing(created.installation.id);
  const claim = fixture.claimPairing(pairing.code, 'valid event gateway');
  const isolated = await relayOn(isolatedPath);
  const base = isolated.base;
  try {
    for (let index = 0; index < 60; index += 1) {
      await api(base, '/v1/installations', { method: 'POST', body: registration('invalid'), from: '10.9.0.7' });
      await api(base, '/v1/pairings/claim', { method: 'POST', body: { pairing_code: 'not-a-live-code' }, from: '10.9.0.7' });
    }
    const result = await api(base, '/v1/events', {
      method: 'POST',
      credential: `${created.installation.id}.${claim.gatewayId}.${claim.gatewaySecret}`,
      body: { event_id: 'response:after-invalid-flood', type: 'plugin.hello' },
    });
    assert.equal(result.status, 202, 'invalid or rate-limited public requests must not spend the global ingress budget');
  } finally {
    await stopRelay(isolated);
  }
});

test('authenticated invalid or locally limited writes do not spend the shared ingress budget', async () => {
  const isolatedPath = join(dir, 'relay-data-invalid-authenticated-ingress.json');
  const fixture = new RelayStore(isolatedPath);
  const first = fixture.createInstallation({ bundleId: PACKAGE, deviceToken: fcmToken() });
  const second = fixture.createInstallation({ bundleId: PACKAGE, deviceToken: fcmToken() });
  const firstPairing = fixture.createPairing(first.installation.id);
  const firstClaim = fixture.claimPairing(firstPairing.code, 'invalid request source');
  const secondPairing = fixture.createPairing(second.installation.id);
  const secondClaim = fixture.claimPairing(secondPairing.code, 'valid request source');
  const isolated = await relayOn(isolatedPath);
  const base = isolated.base;
  const deviceCredential = `${first.installation.id}.${first.deviceSecret}`;
  const gatewayCredential = `${first.installation.id}.${firstClaim.gatewayId}.${firstClaim.gatewaySecret}`;
  try {
    for (let index = 0; index < 40; index += 1) {
      await api(base, `/v1/installations/${first.installation.id}`, {
        method: 'PUT', credential: deviceCredential, body: { device_token: 'invalid' },
      });
      await api(base, '/v1/events', {
        method: 'POST', credential: gatewayCredential, body: { event_id: 'bad', type: 'plugin.hello' },
      });
      await api(base, `/v1/installations/${first.installation.id}/pairings`, {
        method: 'POST', credential: deviceCredential, body: { dashboard_id: 'not-a-uuid' },
      });
    }
    const result = await api(base, '/v1/events', {
      method: 'POST',
      credential: `${second.installation.id}.${secondClaim.gatewayId}.${secondClaim.gatewaySecret}`,
      body: { event_id: 'plugin:after-invalid-authenticated-flood', type: 'plugin.hello' },
    });
    assert.equal(result.status, 202, 'validation and local quota failures must not spend the shared ingress budget');
  } finally {
    await stopRelay(isolated);
  }
});

test('revocation budget leaves decision answer and release actions available', async () => {
  const isolatedPath = join(dir, 'relay-data-control-budget.json');
  const installations = {};
  const credentials = [];
  let gatewayCounter = 0;
  const hash = (value) => createHash('sha256').update(value).digest('hex');
  for (let installationIndex = 0; installationIndex < 8; installationIndex += 1) {
    const installationId = `00000000-0000-4000-8000-${String(installationIndex + 1).padStart(12, '0')}`;
    const deviceSecret = `device-${installationIndex}`;
    const gateways = {};
    for (let gatewayIndex = 0; gatewayIndex < 16; gatewayIndex += 1) {
      const gatewayId = `10000000-0000-4000-8000-${String(gatewayCounter + 1).padStart(12, '0')}`;
      const gatewaySecret = `gateway-${gatewayCounter++}`;
      gateways[gatewayId] = { id: gatewayId, name: 'test', secretHash: hash(gatewaySecret), createdAt: new Date().toISOString() };
      credentials.push(`${installationId}.${gatewayId}.${gatewaySecret}`);
    }
    installations[installationId] = {
      id: installationId, bundleId: PACKAGE, deviceToken: fcmToken(),
      deviceSecretHash: hash(deviceSecret), gateways, active: true, preferences: { enabled: true },
      createdAt: new Date().toISOString(), updatedAt: new Date().toISOString(),
    };
  }
  writeFileSync(isolatedPath, JSON.stringify({ version: 1, installations, pairings: {}, eventIds: {}, pendingDecisions: {} }));
  const fixture = new RelayStore(isolatedPath);
  const answerGateway = Object.keys(installations['00000000-0000-4000-8000-000000000008'].gateways)[0];
  const releaseGateway = Object.keys(installations['00000000-0000-4000-8000-000000000008'].gateways)[1];
  fixture.savePendingDecision({ id: 'talaria-push-control1', installationId: '00000000-0000-4000-8000-000000000008', gatewayId: answerGateway, question: 'answer?' });
  fixture.savePendingDecision({ id: 'talaria-push-control2', installationId: '00000000-0000-4000-8000-000000000008', gatewayId: releaseGateway, question: 'release?' });
  const isolated = await relayOn(isolatedPath);
  const base = isolated.base;
  try {
    let last;
    for (let index = 0; index < 25; index += 1) {
      last = await api(base, '/v1/gateways/current', { method: 'DELETE', credential: credentials[index] });
      if (index < 24) assert.equal(last.status, 204);
    }
    assert.equal(last.status, 429);
    assert.equal(last.json.error, 'rate_limited');
    const decisionInstallation = installations['00000000-0000-4000-8000-000000000008'];
    const deviceCredential = `00000000-0000-4000-8000-000000000008.device-7`;
    const decisionGateway = Object.keys(decisionInstallation.gateways)[0];
    const answered = await api(base, '/v1/decisions/talaria-push-control1/respond', {
      method: 'POST', credential: deviceCredential, body: { answer: 'yes', gateway_id: decisionGateway },
    });
    assert.equal(answered.status, 200, 'revocation flood must leave answers available');
    const secondGateway = Object.keys(decisionInstallation.gateways)[1];
    const released = await api(base, '/v1/decisions/talaria-push-control2', { method: 'DELETE', credential: credentials.find((credential) => credential.startsWith(`00000000-0000-4000-8000-000000000008.${secondGateway}.`)) });
    assert.equal(released.status, 200, 'revocation flood must leave decision release available');
  } finally {
    await stopRelay(isolated);
  }
});

test('no-op updates and metadata-free duplicates leave event capacity available', async () => {
  const isolatedPath = join(dir, 'relay-data-noop-ingress.json');
  const fixture = new RelayStore(isolatedPath);
  const targets = [];
  for (let index = 0; index < 4; index += 1) {
    const created = fixture.createInstallation({ bundleId: PACKAGE, deviceToken: fcmToken() });
    const pairing = fixture.createPairing(created.installation.id);
    const claim = fixture.claimPairing(pairing.code, `quota gateway ${index}`);
    targets.push({ created, claim, deviceCredential: `${created.installation.id}.${created.deviceSecret}`, gatewayCredential: `${created.installation.id}.${claim.gatewayId}.${claim.gatewaySecret}` });
  }
  const isolated = await relayOn(isolatedPath, { RELAY_EVENTS_PER_MINUTE: '96' });
  const base = isolated.base;
  try {
    const original = await api(base, '/v1/events', {
      method: 'POST', credential: targets[0].gatewayCredential,
      body: { event_id: 'plugin:duplicate-no-meta', type: 'plugin.hello' },
    });
    assert.equal(original.status, 202);
    for (let index = 0; index < 93; index += 1) {
      const target = targets[index % targets.length];
      const result = await api(base, '/v1/events', {
        method: 'POST', credential: target.gatewayCredential,
        body: { event_id: `plugin:ingress-${index}`, type: 'plugin.hello' },
      });
      assert.equal(result.status, 202);
    }

    const noOpUpdate = await api(base, `/v1/installations/${targets[0].created.installation.id}`, {
      method: 'PUT', credential: targets[0].deviceCredential,
      body: { preferences: { show_previews: false, enabled: true } },
    });
    assert.equal(noOpUpdate.status, 200, 'a semantic no-op still returns the installation DTO');
    const noMetadataDuplicate = await api(base, '/v1/events', {
      method: 'POST', credential: targets[0].gatewayCredential,
      body: { event_id: 'plugin:duplicate-no-meta', type: 'plugin.hello' },
    });
    assert.deepEqual(noMetadataDuplicate, { status: 200, json: { accepted: true, duplicate: true } });

    const metadataDuplicate = await api(base, '/v1/events', {
      method: 'POST', credential: targets[0].gatewayCredential,
      body: { event_id: 'plugin:duplicate-no-meta', type: 'plugin.hello', plugin_version: '9.9.9' },
    });
    assert.deepEqual(metadataDuplicate, { status: 200, json: { accepted: true, duplicate: true } });

    const event96 = await api(base, '/v1/events', {
      method: 'POST', credential: targets[3].gatewayCredential,
      body: { event_id: 'plugin:ingress-96', type: 'plugin.hello' },
    });
    assert.equal(event96.status, 202, 'no-op update and metadata-free duplicate must not consume the event budget');
    const pluginVersion = () => JSON.parse(readFileSync(isolatedPath, 'utf8'))
      .installations[targets[0].created.installation.id].gateways[targets[0].claim.gatewayId].pluginVersion;
    await waitFor(() => pluginVersion() === '9.9.9', 'metadata-bearing duplicate still records plugin state');
    const event97 = await api(base, '/v1/events', {
      method: 'POST', credential: targets[3].gatewayCredential,
      body: { event_id: 'plugin:ingress-97', type: 'plugin.hello' },
    });
    assert.equal(event97.status, 429);
    assert.equal(event97.json.error, 'rate_limited');
  } finally {
    await stopRelay(isolated);
  }
});

test('non-mutating decision responses do not consume the shared decision budget', async () => {
  const isolatedPath = join(dir, 'relay-data-noop-decision-budget.json');
  const fixture = new RelayStore(isolatedPath);
  const targets = [];
  for (let index = 0; index < 7; index += 1) {
    const created = fixture.createInstallation({ bundleId: PACKAGE, deviceToken: fcmToken() });
    const pairing = fixture.createPairing(created.installation.id);
    const claim = fixture.claimPairing(pairing.code, `response quota ${index}`);
    targets.push({ created, claim, deviceCredential: `${created.installation.id}.${created.deviceSecret}`, gatewayCredential: `${created.installation.id}.${claim.gatewayId}.${claim.gatewaySecret}` });
  }
  const answeredId = 'talaria-push-already-answered';
  fixture.savePendingDecision({ id: answeredId, installationId: targets[1].created.installation.id, gatewayId: targets[1].claim.gatewayId, question: 'Done?' });
  fixture.respondPendingDecision(targets[1].created.installation.id, targets[1].claim.gatewayId, answeredId, 'yes');
  const batchId = 'talaria-push-duplicate-first';
  fixture.savePendingDecision({ id: batchId, installationId: targets[2].created.installation.id, gatewayId: targets[2].claim.gatewayId, question: 'First?', questions: [
    { qid: 'q0', question: 'First?', choices: ['a'] }, { qid: 'q1', question: 'Second?', choices: ['b'] },
  ] });
  fixture.respondPendingDecision(targets[2].created.installation.id, targets[2].claim.gatewayId, batchId, 'a', 'q0');
  const invalidQidId = 'talaria-push-invalid-qid';
  fixture.savePendingDecision({ id: invalidQidId, installationId: targets[3].created.installation.id, gatewayId: targets[3].claim.gatewayId, question: 'Batch?', questions: [
    { qid: 'q0', question: 'First?', choices: ['a'] }, { qid: 'q1', question: 'Second?', choices: ['b'] },
  ] });
  const releasedId = 'talaria-push-released-response';
  fixture.savePendingDecision({ id: releasedId, installationId: targets[4].created.installation.id, gatewayId: targets[4].claim.gatewayId, question: 'Released?' });
  fixture.cancelPendingDecision(targets[4].created.installation.id, targets[4].claim.gatewayId, releasedId);
  const answerId = 'talaria-push-valid-after-noops';
  fixture.savePendingDecision({ id: answerId, installationId: targets[5].created.installation.id, gatewayId: targets[5].claim.gatewayId, question: 'Still answerable?' });
  const releaseId = 'talaria-push-release-after-noops';
  fixture.savePendingDecision({ id: releaseId, installationId: targets[6].created.installation.id, gatewayId: targets[6].claim.gatewayId, question: 'Still releasable?' });

  const isolated = await relayOn(isolatedPath);
  const base = isolated.base;
  try {
    for (let index = 0; index < 100; index += 1) {
      const targetIndex = index % 5;
      const target = targets[targetIndex];
      let id;
      let body;
      let expectedStatus;
      if (targetIndex === 0) {
        // An unknown gateway is an unknown decision; no gateway at all is a
        // malformed answer. Neither changes anything.
        id = 'talaria-push-no-such-decision';
        body = { answer: 'yes', ...(index % 2 === 0 ? { gateway_id: 'missing-gateway' } : {}) };
        expectedStatus = index % 2 === 0 ? 404 : 400;
      } else if (targetIndex === 1) {
        id = answeredId;
        body = { answer: 'again', gateway_id: target.claim.gatewayId };
        expectedStatus = 409;
      } else if (targetIndex === 2) {
        id = batchId;
        body = { answer: 'again', gateway_id: target.claim.gatewayId, ...(index % 2 === 0 ? { question_id: 'q0' } : {}) };
        expectedStatus = 409;
      } else if (targetIndex === 3) {
        id = invalidQidId;
        body = { answer: 'bad-qid', gateway_id: target.claim.gatewayId, question_id: 'q9' };
        expectedStatus = 400;
      } else {
        id = releasedId;
        body = { answer: 'late', gateway_id: target.claim.gatewayId };
        expectedStatus = 410;
      }
      const result = await api(base, `/v1/decisions/${id}/respond`, {
        method: 'POST', credential: target.deviceCredential, body, from: `10.12.0.${targetIndex + 1}`,
      });
      assert.equal(result.status, expectedStatus);
    }

    const answered = await api(base, `/v1/decisions/${answerId}/respond`, {
      method: 'POST', credential: targets[5].deviceCredential,
      body: { answer: 'yes', gateway_id: targets[5].claim.gatewayId },
      from: '10.12.0.6',
    });
    assert.equal(answered.status, 200, '100 non-mutating responses leave the budget for a real answer');
    const released = await api(base, `/v1/decisions/${releaseId}`, {
      method: 'DELETE', credential: targets[6].gatewayCredential, from: '10.12.0.7',
    });
    assert.equal(released.status, 200, 'a valid release also remains available');
  } finally {
    await stopRelay(isolated);
  }
});

test('a full installation store rejects a valid registration before charging its public budget', async () => {
  const isolatedPath = join(dir, 'relay-data-full-registration-capacity.json');
  const installations = {};
  for (let index = 0; index < 1024; index += 1) {
    const id = `20000000-0000-4000-8000-${String(index + 1).padStart(12, '0')}`;
    installations[id] = { id, active: true, gateways: {}, deviceSecretHash: 'synthetic', preferences: {} };
  }
  writeFileSync(isolatedPath, JSON.stringify({ version: 1, installations, pairings: {}, eventIds: {}, pendingDecisions: {} }));
  const before = readFileSync(isolatedPath, 'utf8');
  const isolated = await relayOn(isolatedPath, { RELAY_MAX_INSTALLATIONS: '1024' });
  const token = fcmToken();
  try {
    for (let index = 0; index < 125; index += 1) {
      const result = await api(isolated.base, '/v1/installations', {
        method: 'POST', body: registration(token), from: `10.11.0.${index + 1}`,
      });
      assert.equal(result.status, 429);
      assert.equal(result.json.error, 'installation_limit_reached', 'capacity rejection must stay actionable across rotated client IPs');
    }
    assert.equal(readFileSync(isolatedPath, 'utf8'), before, 'rejected registrations do not rewrite the full store');
  } finally {
    await stopRelay(isolated);
  }
});

test('event IDs stay out of the data file and a full event ledger keeps accepting events', async () => {
  const isolatedPath = join(dir, 'relay-data-event-ledger.json');
  const fixture = new RelayStore(isolatedPath);
  const created = fixture.createInstallation({ bundleId: PACKAGE, deviceToken: fcmToken() });
  const pairing = fixture.createPairing(created.installation.id);
  const claim = fixture.claimPairing(pairing.code, 'ledger target');
  const isolated = await relayOn(isolatedPath, {
    RELAY_MAX_EVENT_IDS_PER_INSTALLATION: '2', RELAY_MAX_EVENT_IDS: '2',
  });
  const send = (eventId, extra = {}) => api(isolated.base, '/v1/events', {
    method: 'POST',
    credential: `${created.installation.id}.${claim.gatewayId}.${claim.gatewaySecret}`,
    body: { event_id: eventId, type: 'plugin.hello', ...extra },
  });
  try {
    for (const eventId of ['plugin:ledger-1', 'plugin:ledger-2', 'plugin:ledger-3']) {
      assert.equal((await send(eventId)).status, 202, 'a full ledger never rejects an event');
    }
    assert.deepEqual(await send('plugin:ledger-3'), { status: 200, json: { accepted: true, duplicate: true } }, 'recent IDs still dedupe');
    assert.equal((await send('plugin:ledger-1')).status, 202, 'the oldest ID was forgotten to make room');
    assert.equal((await send('plugin:ledger-4', { plugin_version: '9.9.9' })).status, 202);
    // Windows ends a process without a SIGTERM for it to save on: there the
    // save timer writes the metadata instead.
    if (process.platform === 'win32') {
      await waitFor(() => JSON.parse(readFileSync(isolatedPath, 'utf8')).installations[created.installation.id].gateways[claim.gatewayId].pluginVersion === '9.9.9',
        'deferred plugin metadata is written within the save delay');
    }
  } finally {
    await stopRelay(isolated);
  }
  const persisted = JSON.parse(readFileSync(isolatedPath, 'utf8'));
  assert.equal(persisted.eventIds, undefined, 'event IDs never reach the data file');
  assert.equal(persisted.installations[created.installation.id].gateways[claim.gatewayId].pluginVersion, '9.9.9',
    'deferred plugin metadata is written on shutdown');
});

test('a push that fails after intake gives its event ID back, so the retry is sent', async () => {
  const isolatedPath = join(dir, 'relay-data-retry.json');
  const fixture = new RelayStore(isolatedPath);
  const token = fcmToken();
  const created = fixture.createInstallation({ bundleId: PACKAGE, deviceToken: token });
  const pairing = fixture.createPairing(created.installation.id);
  const claim = fixture.claimPairing(pairing.code, 'retry target');
  // Google drops the first send and refuses the second, then takes them.
  const own = await startGoogle();
  own.answers.push('drop', UNAVAILABLE);
  const isolated = await relayOn(isolatedPath, {}, { through: own });
  // The plugin's retry: the same event, same id.
  const send = () => api(isolated.base, '/v1/events', {
    method: 'POST',
    credential: `${created.installation.id}.${claim.gatewayId}.${claim.gatewaySecret}`,
    body: { type: 'input.needed', event_id: 'input:retry0000001', session_id: 'sess-retry', profile: 'default' },
  });
  try {
    assert.deepEqual(await send(), { status: 502, json: { error: 'push_unreachable' } });
    assert.deepEqual(await send(), { status: 502, json: { error: 'push_rejected', reason: 'UNAVAILABLE', status: 503 } },
      'a retry after a failed send is sent again, not called a duplicate');
    assert.deepEqual(await send(), { status: 202, json: { accepted: true, delivered: true } });
    assert.deepEqual(await send(), { status: 200, json: { accepted: true, duplicate: true } }, 'a delivered event still dedupes');
    assert.equal(pushesTo(own, token).length, 1, 'the phone got the notification once');
  } finally {
    await stopRelay(isolated);
    await own.close();
  }
});

test('an hourly limit refusal gives the event id back', async () => {
  const isolatedPath = join(dir, 'relay-data-hourly.json');
  const fixture = new RelayStore(isolatedPath);
  const created = fixture.createInstallation({ bundleId: PACKAGE, deviceToken: fcmToken() });
  const pairing = fixture.createPairing(created.installation.id);
  const claim = fixture.claimPairing(pairing.code, 'hourly target');
  const isolated = await relayOn(isolatedPath, { RELAY_TEST_LOCATION_PUSHES_PER_HOUR: '1' });
  const send = (eventId) => api(isolated.base, '/v1/events', {
    method: 'POST',
    credential: `${created.installation.id}.${claim.gatewayId}.${claim.gatewaySecret}`,
    body: { type: 'location.requested', event_id: eventId, profile: 'work', locate: { id: 'l_0123456789abcdefXYZ' } },
  });
  try {
    assert.deepEqual(await send('locate:hourly00000000000000001'), { status: 202, json: { accepted: true, delivered: true } });
    const refused = await send('locate:hourly00000000000000002');
    assert.equal(refused.status, 429);
    assert.deepEqual(await send('locate:hourly00000000000000002'), refused, 'the retry is refused again, not called a duplicate');
  } finally {
    await stopRelay(isolated);
  }
});

test('a copy that arrives mid-send waits for it, and is told to retry when it fails', async () => {
  const isolatedPath = join(dir, 'relay-data-in-flight.json');
  const fixture = new RelayStore(isolatedPath);
  const token = fcmToken();
  const created = fixture.createInstallation({ bundleId: PACKAGE, deviceToken: token });
  const pairing = fixture.createPairing(created.installation.id);
  const claim = fixture.claimPairing(pairing.code, 'in-flight target');
  // Every send takes 1.5 s, and Google drops the first.
  const own = await startGoogle();
  own.answers.push('drop');
  own.delayMs = 1500;
  const isolated = await relayOn(isolatedPath, {}, { through: own });
  const send = (eventId) => api(isolated.base, '/v1/events', {
    method: 'POST',
    credential: `${created.installation.id}.${claim.gatewayId}.${claim.gatewaySecret}`,
    body: { type: 'input.needed', event_id: eventId, session_id: 'sess-in-flight', profile: 'default' },
  });
  try {
    const first = send('input:inflight00001');
    await new Promise((resolve) => setTimeout(resolve, 500));
    const copy = await send('input:inflight00001');
    assert.deepEqual(await first, { status: 502, json: { error: 'push_unreachable' } });
    assert.deepEqual(copy, { status: 503, json: { error: 'delivery_failed' } }, 'the copy is not told the failed send was a duplicate');
    assert.deepEqual(await send('input:inflight00001'), { status: 202, json: { accepted: true, delivered: true } });

    const second = send('input:inflight00002');
    await new Promise((resolve) => setTimeout(resolve, 500));
    assert.deepEqual(await send('input:inflight00002'), { status: 200, json: { accepted: true, duplicate: true } },
      'a copy of a send that worked is a duplicate');
    assert.equal((await second).status, 202);
    assert.equal(pushesTo(own, token).length, 2, 'one push per event');
  } finally {
    await stopRelay(isolated);
    await own.close();
  }
});

test('call requests reach the phone with the call category and stop at the per-installation ceiling', async () => {
  const isolatedPath = join(dir, 'relay-data-calls.json');
  const fixture = new RelayStore(isolatedPath);
  const token = fcmToken();
  const created = fixture.createInstallation({ bundleId: PACKAGE, deviceToken: token });
  const pairing = fixture.createPairing(created.installation.id);
  const claim = fixture.claimPairing(pairing.code, 'calls target');
  const isolated = await relayOn(isolatedPath, { RELAY_CALLS_PER_INSTALLATION_PER_DAY: '2' });
  const send = (eventId, type = 'call.requested') => api(isolated.base, '/v1/events', {
    method: 'POST',
    credential: `${created.installation.id}.${claim.gatewayId}.${claim.gatewaySecret}`,
    body: {
      type,
      event_id: eventId,
      session_id: 'st-1',
      plugin_version: '0.13.0',
      title: 'Hermes wants to talk',
      body: 'Your job finished.',
      call: { id: eventId.slice(5), kind: 'done', session_ids: ['rt-1', 'st-1'] },
    },
  });
  try {
    assert.equal((await send('call:000000000000000000000001')).status, 202);
    assert.deepEqual(await send('call:000000000000000000000001'), { status: 200, json: { accepted: true, duplicate: true } },
      'a resent call is a duplicate, not a second call');
    assert.equal((await send('call:000000000000000000000002')).status, 202);
    assert.deepEqual(await send('call:000000000000000000000003'), { status: 429, json: { error: 'call_limit' } });
    assert.equal((await send('response:000000000000000000000004', 'response.ready')).status, 202,
      'ordinary pushes are not held to the call ceiling');
  } finally {
    await stopRelay(isolated);
  }
  const sent = pushesTo(google, token);
  assert.equal(sent.length, 3);
  // A call is an ordinary notification the app shows with its Talk action.
  assert.equal(sent[0].data.category, 'HERMES_CALL');
  assert.equal(sent[0].data.title, 'Hermes wants to talk');
  assert.deepEqual(routingOf(sent[0]).call, { id: '000000000000000000000001', kind: 'done', session_ids: ['rt-1', 'st-1'] });
  assert.equal(sent[2].data.category, undefined);
});

test('an invalid capacity setting stops the relay at boot', async () => {
  const { code, stderr } = await bootFailure('invalid-limit', { RELAY_MAX_INSTALLATIONS: '10k' });
  assert.notEqual(code, 0);
  assert.match(stderr, /RELAY_MAX_INSTALLATIONS must be a positive integer\./);
});

// A relay that should refuse to start: `{ code, stderr }`. It is a test
// relay pointed at the fake Google unless `extraEnv` says otherwise.
async function bootFailure(name, extraEnv) {
  const child = spawn(process.execPath, ['src/server.mjs'], {
    cwd: fileURLToPath(new URL('..', import.meta.url)),
    env: {
      ...process.env,
      HOST: '127.0.0.1',
      PORT: String(await freePort()),
      PUBLIC_URL: `https://relay-${name}.example`,
      DATA_PATH: join(dir, `relay-data-${name}.json`),
      RELAY_TEST_SEAMS: '1',
      ...fcmEnv(dir, google),
      ...extraEnv,
    },
    stdio: ['ignore', 'ignore', 'pipe'],
  });
  let stderr = '';
  child.stderr.on('data', (chunk) => { stderr += chunk; });
  // A relay that starts anyway fails the test instead of hanging it.
  const guard = setTimeout(() => child.kill('SIGKILL'), 10_000);
  const code = await new Promise((resolve) => child.once('exit', resolve));
  clearTimeout(guard);
  return { code, stderr };
}

test('the Google test seams stop a relay that is not a test at boot', async () => {
  // Another FCM origin, and a key file whose token endpoint isn't HTTPS (the
  // fake Google's): both only for a test relay.
  for (const [env, message] of [
    [{}, /FCM_ORIGIN is a test setting; unset it for a real relay\./],
    [{ FCM_ORIGIN: undefined }, /token_uri must use HTTPS\./],
  ]) {
    const { code, stderr } = await bootFailure('seams', { ...env, RELAY_TEST_SEAMS: '' });
    assert.notEqual(code, 0);
    assert.match(stderr, message);
  }
});

test('an invalid TRUST_PROXY stops the relay at boot', async () => {
  const { code, stderr } = await bootFailure('proxy-hops', { TRUST_PROXY: 'yes' });
  assert.notEqual(code, 0);
  assert.match(stderr, /TRUST_PROXY must be the number of reverse proxies/);
});

// A Compose bind mount Docker created is root's, and the relay runs as node:
// it would otherwise answer /healthz and fail its first save. Windows keeps
// no write permission on a directory for chmod to take away.
test('a data directory the relay cannot write stops it at boot', {
  skip: (process.getuid?.() === 0 && 'root can write anywhere') || (process.platform === 'win32' && 'Windows ignores directory permission bits'),
}, async () => {
  const locked = join(dir, 'locked-data');
  mkdirSync(locked);
  chmodSync(locked, 0o555);
  try {
    const { code, stderr } = await bootFailure('locked-data', { DATA_PATH: join(locked, 'relay.json') });
    assert.equal(code, 1);
    assert.match(stderr, /DATA_PATH directory .*locked-data is not writable/);
  } finally {
    chmodSync(locked, 0o755);
  }
});

test('a client-sent X-Forwarded-For entry cannot pick its own rate-limit bucket', async () => {
  const isolated = await relayOn(join(dir, 'relay-data-xff.json'));
  try {
    let response;
    // The proxy appends the address it saw (10.20.0.1); everything before
    // it is the client's own header, rotated on every request.
    for (let index = 0; index < 13; index += 1) {
      response = await api(isolated.base, '/v1/installations', {
        method: 'POST',
        body: registration('invalid'),
        headers: { 'x-forwarded-for': `203.0.113.${index + 1}, 10.20.0.1` },
      });
    }
    assert.equal(response.status, 429, 'all 13 share the proxy-seen bucket of 12 a minute');
  } finally {
    await stopRelay(isolated);
  }
});

test('batch clarify end to end: questions[] survive intake, per-qid answers, release, and only the respond route answers', async () => {
  // Notifications ENABLED so intake builds the real FCM message and
  // deliverability is computed from what actually survived into it.
  const phone = await pairedPhone(baseUrl, google, { preferences: { enabled: true, decision_cards: true }, gatewayName: 'batch gateway' });
  const deviceCredential = phone.credential;
  const gatewayCredential = phone.gateway;
  const gatewayId = phone.gatewayId;

  // Push a two-question batch through the real event endpoint.
  const event = await api(baseUrl, '/v1/events', {
    method: 'POST',
    credential: gatewayCredential,
    body: {
      type: 'input.needed',
      event_id: 'input:batch0000001',
      session_id: 'sess-batch',
      profile: 'default',
      decision: {
        kind: 'clarify',
        request_id: 'talaria-push-batche2e1',
        question: 'Which environment?',
        choices: ['staging', 'prod'],
        questions: [
          { qid: 'q0', question: 'Which environment?', choices: ['staging', 'prod'], multi_select: false },
          { qid: 'q1', question: 'Which tests?', choices: ['unit', 'ui'], multi_select: true },
        ],
      },
    },
  });
  // A decision card that survives the size guard is delivered for real
  // (Google accepts): deliverable stays true and the plugin keeps polling.
  assert.equal(event.status, 202);
  assert.deepEqual(event.json, { accepted: true, delivered: true });

  // The stored decision kept the FULL batch, deliverable, and the open qids.
  const poll = await api(baseUrl, '/v1/decisions/talaria-push-batche2e1', { credential: gatewayCredential });
  assert.equal(poll.status, 200);
  assert.equal(poll.json.status, 'pending');
  assert.equal(poll.json.deliverable, true, 'the card survived into the FCM message');
  assert.deepEqual(poll.json.remaining, ['q0', 'q1']);

  // Device answers q0 through the /respond route.
  const q0 = await api(baseUrl, '/v1/decisions/talaria-push-batche2e1/respond', {
    method: 'POST',
    credential: deviceCredential,
    body: { question_id: 'q0', answer: 'staging', gateway_id: gatewayId },
  });
  assert.equal(q0.status, 200);
  assert.deepEqual(q0.json, { status: 'answered', remaining: ['q1'] });

  // A duplicate q0 is question-locked (409), NOT released: q1 stays open.
  const duplicate = await api(baseUrl, '/v1/decisions/talaria-push-batche2e1/respond', {
    method: 'POST',
    credential: deviceCredential,
    body: { question_id: 'q0', answer: 'prod', gateway_id: gatewayId },
  });
  assert.equal(duplicate.status, 409);
  assert.equal(duplicate.json.error, 'already_answered');

  // The bare path no longer answers anything: q1 stays open.
  const bare = await api(baseUrl, '/v1/decisions/talaria-push-batche2e1', {
    method: 'POST',
    credential: deviceCredential,
    body: { question_id: 'q1', answer: '["unit"]', gateway_id: gatewayId },
  });
  assert.equal(bare.status, 404);
  assert.deepEqual((await api(baseUrl, '/v1/decisions/talaria-push-batche2e1', { credential: gatewayCredential })).json.remaining, ['q1']);

  const q1 = await api(baseUrl, '/v1/decisions/talaria-push-batche2e1/respond', {
    method: 'POST',
    credential: deviceCredential,
    body: { question_id: 'q1', answer: '["unit"]', gateway_id: gatewayId },
  });
  assert.equal(q1.status, 200);
  assert.deepEqual(q1.json, { status: 'answered', remaining: [] });

  const done = await api(baseUrl, '/v1/decisions/talaria-push-batche2e1', { credential: gatewayCredential });
  assert.equal(done.status, 200);
  assert.equal(done.json.status, 'answered');
  assert.deepEqual(done.json.remaining, []);
  assert.deepEqual({ ...done.json.answers }, { q0: 'staging', q1: '["unit"]' });

  // ── Release semantics: the native path won, the plugin deletes. ──
  await api(baseUrl, '/v1/events', {
    method: 'POST',
    credential: gatewayCredential,
    body: {
      type: 'input.needed',
      event_id: 'input:batch0000002',
      session_id: 'sess-batch',
      profile: 'default',
      decision: {
        kind: 'clarify',
        request_id: 'talaria-push-batche2e2',
        question: 'Second?',
        questions: [{ qid: 'q0', question: 'Second?', choices: ['a'], multi_select: false }],
      },
    },
  });
  const released = await api(baseUrl, '/v1/decisions/talaria-push-batche2e2', { method: 'DELETE', credential: gatewayCredential });
  assert.equal(released.status, 200);
  assert.deepEqual(released.json, { status: 'cancelled' }, 'a LIVE decision release reports cancelled');
  // A late device answer reports decision_released (410), NOT qid-locked.
  const late = await api(baseUrl, '/v1/decisions/talaria-push-batche2e2/respond', {
    method: 'POST',
    credential: deviceCredential,
    body: { question_id: 'q0', answer: 'a', gateway_id: gatewayId },
  });
  assert.equal(late.status, 410);
  assert.equal(late.json.error, 'decision_released');
  // The poller sees the release and falls back to the original path.
  const pollReleased = await api(baseUrl, '/v1/decisions/talaria-push-batche2e2', { credential: gatewayCredential });
  assert.deepEqual(pollReleased.json, { status: 'unknown' });

  // ── An oversized batch is parked undeliverable: the size guard stripped
  // the card from the FCM message, so the plugin must stop polling. ──
  const oversized = await api(baseUrl, '/v1/events', {
    method: 'POST',
    credential: gatewayCredential,
    body: {
      type: 'input.needed',
      event_id: 'input:batch0000003',
      session_id: 'sess-batch',
      profile: 'default',
      decision: {
        kind: 'clarify',
        request_id: 'talaria-push-batche2e3',
        question: 'Huge?',
        questions: Array.from({ length: 8 }, (_, i) => ({
          qid: `q${i}`,
          question: 'x'.repeat(500),
          choices: Array.from({ length: 8 }, (_, j) => 'y'.repeat(80)),
          multi_select: false,
        })),
      },
    },
  });
  assert.equal(oversized.status, 202);
  assert.deepEqual(
    oversized.json,
    { accepted: true, delivered: true },
    'the plain input.needed fallback banner is still delivered when the size guard strips the card',
  );
  const banner = pushesTo(google, phone.token).at(-1);
  assert.equal(routingOf(banner).decision, undefined, 'the banner went without its card');
  assert.ok(dataBytes(banner.data) <= FCM_MAX_DATA_BYTES);
  const oversizedPoll = await api(baseUrl, '/v1/decisions/talaria-push-batche2e3', { credential: gatewayCredential });
  assert.equal(oversizedPoll.json.status, 'pending');
  assert.equal(oversizedPoll.json.deliverable, false, 'a size-guard-stripped card must be undeliverable');
});

test('clarify decision: push → device answer → gateway poll', async () => {
  // Device registers with notifications disabled so event delivery skips Google.
  const phone = await registerPhone(baseUrl, google, { preferences: { enabled: false, decision_cards: true } });
  const deviceCredential = phone.credential;
  const installationId = phone.installation.id;

  // Pair a gateway (device creates the code, gateway claims it).
  const { gateway: gatewayCredential, gatewayId } = await pairHost(baseUrl, phone, { gatewayName: 'test gateway' });

  // Gateway pushes a clarify decision.
  const event = await api(baseUrl, '/v1/events', {
    method: 'POST',
    credential: gatewayCredential,
    body: {
      type: 'input.needed',
      event_id: 'input:abcdef123456',
      session_id: 'sess-1',
      profile: 'default',
      decision: {
        kind: 'clarify',
        request_id: 'talaria-push-abc123',
        question: 'Which color?',
        choices: ['Red', 'Blue'],
      },
    },
  });
  assert.equal(event.status, 202);
  assert.deepEqual(event.json, { accepted: true, delivered: false });

  // Gateway polls: pending. This installation registered with enabled:false,
  // so no card was delivered and the decision reports deliverable:false —
  // the plugin's poll loop stops instead of waiting out the full budget.
  const poll = await api(baseUrl, '/v1/decisions/talaria-push-abc123', { credential: gatewayCredential });
  assert.equal(poll.status, 200);
  assert.deepEqual(poll.json, { status: 'pending', deliverable: false });

  // Another installation's gateway must not see it.
  const stranger = await pairedPhone(baseUrl, google, { gatewayName: 'stranger' });
  const strangerPoll = await api(baseUrl, '/v1/decisions/talaria-push-abc123', {
    credential: stranger.gateway,
  });
  assert.equal(strangerPoll.status, 200);
  assert.equal(strangerPoll.json.status, 'unknown');

  // The stranger device cannot answer either, even naming the right gateway.
  const strangerAnswer = await api(baseUrl, '/v1/decisions/talaria-push-abc123/respond', {
    method: 'POST',
    credential: stranger.credential,
    body: { answer: 'Blue', gateway_id: gatewayId },
  });
  assert.equal(strangerAnswer.status, 404);

  // An answer that doesn't name its gateway is refused, never matched to one.
  const unnamed = await api(baseUrl, '/v1/decisions/talaria-push-abc123/respond', {
    method: 'POST',
    credential: deviceCredential,
    body: { answer: 'Blue' },
  });
  assert.deepEqual(unnamed, { status: 400, json: { error: 'invalid_gateway_id' } });
  assert.deepEqual((await api(baseUrl, '/v1/decisions/talaria-push-abc123', { credential: gatewayCredential })).json,
    { status: 'pending', deliverable: false }, 'the refused answer answered nothing');

  // The paired device answers; the gateway observes the answer.
  const answer = await api(baseUrl, '/v1/decisions/talaria-push-abc123/respond', {
    method: 'POST',
    credential: deviceCredential,
    body: { answer: 'Red', gateway_id: gatewayId },
  });
  assert.equal(answer.status, 200);
  assert.equal(answer.json.status, 'answered');

  const answered = await api(baseUrl, '/v1/decisions/talaria-push-abc123', { credential: gatewayCredential });
  assert.equal(answered.status, 200);
  assert.deepEqual(answered.json, { status: 'answered', answer: 'Red' });

  // Releasing an ALREADY-COMPLETED decision is a diagnostic, not an error:
  // same 200, distinct status string so gateway logs can tell the two apart.
  const completed = await api(baseUrl, '/v1/decisions/talaria-push-abc123', { method: 'DELETE', credential: gatewayCredential });
  assert.equal(completed.status, 200);
  assert.deepEqual(completed.json, { status: 'already_completed' });

  // Second answer attempts are rejected.
  const reanswer = await api(baseUrl, '/v1/decisions/talaria-push-abc123/respond', {
    method: 'POST',
    credential: deviceCredential,
    body: { answer: 'Blue', gateway_id: gatewayId },
  });
  assert.equal(reanswer.status, 409);

  // A duplicate delivery of the same event (same event_id) must not wipe the
  // parked decision's answer: dedupe happens before the pending-decision save.
  const duplicate = await api(baseUrl, '/v1/events', {
    method: 'POST',
    credential: gatewayCredential,
    body: {
      type: 'input.needed',
      event_id: 'input:abcdef123456',
      session_id: 'sess-1',
      profile: 'default',
      decision: {
        kind: 'clarify',
        request_id: 'talaria-push-abc123',
        question: 'Which color?',
        choices: ['Red', 'Blue'],
      },
    },
  });
  assert.equal(duplicate.status, 200);
  assert.equal(duplicate.json.duplicate, true);
  const reusedRequestId = await api(baseUrl, '/v1/events', {
    method: 'POST',
    credential: gatewayCredential,
    body: {
      type: 'input.needed',
      event_id: 'input:abcdef654321',
      session_id: 'sess-1',
      profile: 'default',
      decision: { kind: 'clarify', request_id: 'talaria-push-abc123', question: 'Replacement?', choices: ['Wrong'] },
    },
  });
  assert.equal(reusedRequestId.status, 200);
  assert.equal(reusedRequestId.json.duplicate, true, 'a repeated logical request id does not send a second answerable push');
  const afterDuplicate = await api(baseUrl, '/v1/decisions/talaria-push-abc123', { credential: gatewayCredential });
  assert.deepEqual(afterDuplicate.json, { status: 'answered', answer: 'Red' });

  // Empty answers are rejected outright.
  const empty = await api(baseUrl, '/v1/decisions/talaria-push-abc123/respond', {
    method: 'POST',
    credential: deviceCredential,
    body: { answer: '   ', gateway_id: gatewayId },
  });
  assert.equal(empty.status, 400);

  // Unknown ids are 404 for devices.
  const unknown = await api(baseUrl, '/v1/decisions/talaria-push-nope/respond', {
    method: 'POST',
    credential: deviceCredential,
    body: { answer: 'x', gateway_id: gatewayId },
  });
  assert.equal(unknown.status, 404);

  // Credentials are enforced on both endpoints.
  const unauth = await api(baseUrl, '/v1/decisions/talaria-push-abc123');
  assert.equal(unauth.status, 401);
  const unauthAnswer = await api(baseUrl, '/v1/decisions/talaria-push-abc123/respond', {
    method: 'POST',
    body: { answer: 'x', gateway_id: gatewayId },
  });
  assert.equal(unauthAnswer.status, 401);

  // Plugin version announcement: a control event that never notifies but
  // records the gateway's plugin state for an operator reading the data file.
  const hello = await api(baseUrl, '/v1/events', {
    method: 'POST',
    credential: gatewayCredential,
    body: {
      type: 'plugin.hello',
      // The plugin's event_id hashes the version (hex, no dots); keep the
      // fixture within the id charset the relay accepts.
      event_id: 'hello:020abcdef12',
      plugin_version: '0.3.0',
      plugin_capabilities: ['approval-decisions', 'clarify-loop', 'version-reporting'],
    },
  });
  assert.equal(hello.status, 202);
  assert.deepEqual(hello.json, { accepted: true, delivered: false });
  await waitFor(() => {
    const recorded = savedGateway(dataPath, installationId, 'test gateway');
    return recorded?.pluginVersion === '0.3.0' && recorded.pluginCapabilities.includes('clarify-loop') && Boolean(recorded.lastEventAt);
  }, 'the hello records the gateway\'s plugin state');

  // A later real event refreshes the recorded plugin version.
  await api(baseUrl, '/v1/events', {
    method: 'POST',
    credential: gatewayCredential,
    body: {
      type: 'response.ready',
      event_id: 'response:abcdef999999',
      session_id: 'sess-1',
      plugin_version: '0.2.1',
      plugin_capabilities: ['approval-decisions', 'clarify-loop'],
    },
  });

  // A second gateway on the same installation running the same plugin version
  // sends the same deterministic hello id; it must still be recorded even
  // though the event itself dedupes.
  const { gateway: secondGateway } = await pairHost(baseUrl, phone, { gatewayName: 'second gateway' });
  await api(baseUrl, '/v1/events', {
    method: 'POST',
    credential: secondGateway,
    body: {
      type: 'plugin.hello',
      event_id: 'hello:020abcdef12',
      plugin_version: '0.3.0',
      plugin_capabilities: ['approval-decisions', 'clarify-loop', 'version-reporting'],
    },
  });

  // A pre-0.2 notifier sends events with no plugin_version: last-seen is
  // stamped anyway, so a gateway that sends shows apart from one that only
  // paired.
  await api(baseUrl, '/v1/events', {
    method: 'POST',
    credential: secondGateway,
    body: {
      type: 'response.ready',
      event_id: 'response:legacy111111',
      session_id: 'sess-2',
    },
  });
  await waitFor(() => {
    const refreshed = savedGateway(dataPath, installationId, 'test gateway');
    const second = savedGateway(dataPath, installationId, 'second gateway');
    return refreshed?.pluginVersion === '0.2.1' && second?.pluginVersion === '0.3.0' && Boolean(second.lastEventAt);
  }, 'a later event refreshes the version, and a duplicate hello id still records the new gateway');
});

test('full decision capacity rejects before event persistence and leaves another installation answerable', async () => {
  const seed = new RelayStore(capacityDataPath);
  let victim;
  let attacker;
  for (let i = 0; i < 8; i += 1) {
    const registered = seed.createInstallation({
      bundleId: PACKAGE,
      deviceToken: fcmToken(),
      preferences: i === 7 ? { enabled: false } : {},
    });
    const pairing = seed.createPairing(registered.installation.id);
    const gateway = seed.claimPairing(pairing.code, `gateway ${i}`);
    const entry = {
      deviceCredential: `${registered.installation.id}.${registered.deviceSecret}`,
      gatewayCredential: `${registered.installation.id}.${gateway.gatewayId}.${gateway.gatewaySecret}`,
      installationId: registered.installation.id,
      gatewayId: gateway.gatewayId,
    };
    if (i === 0) victim = entry;
    if (i === 7) attacker = entry;
    for (let j = 0; j < 32; j += 1) {
      const id = `talaria-push-seed-${i}-${j}`;
      seed.savePendingDecision({
        id,
        installationId: entry.installationId,
        gatewayId: entry.gatewayId,
        question: 'Seed decision?',
        choices: ['Yes', 'No'],
        deliverable: i !== 7,
      });
    }
  }
  assert.equal(Object.keys(seed.data.pendingDecisions).length, 256);
  const capacity = await relayOn(capacityDataPath, { RELAY_MAX_ACTIVE_DECISIONS: '256' });
  const rejected = await api(capacity.base, '/v1/events', {
    method: 'POST',
    credential: attacker.gatewayCredential,
    body: {
      type: 'input.needed',
      event_id: 'input:capacity-over',
      session_id: 'sess-1',
      profile: 'default',
      decision: { kind: 'clarify', request_id: 'talaria-push-capacity-over', question: 'Overflow?', choices: ['Yes'] },
    },
  });
  assert.equal(rejected.status, 429);
  assert.deepEqual(rejected.json, { error: 'decision_capacity_exceeded' });
  const persisted = JSON.parse(readFileSync(capacityDataPath, 'utf8'));
  assert.equal(persisted.eventIds, undefined, 'event IDs never reach the data file');
  assert.equal(persisted.pendingDecisions[RelayStore.decisionKey(attacker.installationId, attacker.gatewayId, 'talaria-push-capacity-over')], undefined,
    'no unstored answerable decision is created');

  const victimId = 'talaria-push-seed-0-0';
  const answer = await api(capacity.base, `/v1/decisions/${victimId}/respond`, {
    method: 'POST', credential: victim.deviceCredential, body: { answer: 'Yes', gateway_id: victim.gatewayId },
  });
  assert.equal(answer.status, 200);
  assert.deepEqual((await api(capacity.base, `/v1/decisions/${victimId}`, {
    credential: victim.gatewayCredential,
  })).json, { status: 'answered', answer: 'Yes' });
});

test('per-installation active capacity returns 429 and retry succeeds after an answer releases a slot', async () => {
  const seed = new RelayStore(installationCapacityDataPath);
  const registered = seed.createInstallation({ bundleId: PACKAGE, deviceToken: fcmToken() });
  const pairing = seed.createPairing(registered.installation.id);
  const gateway = seed.claimPairing(pairing.code, 'capacity gateway');
  for (let i = 0; i < 32; i += 1) {
    seed.savePendingDecision({
      id: `talaria-push-install-${i}`, installationId: registered.installation.id,
      gatewayId: gateway.gatewayId, question: 'Seed?',
    });
  }
  const capacity = await relayOn(installationCapacityDataPath);
  const base = capacity.base;
  const event = {
    type: 'input.needed', event_id: 'input:install-capacity', session_id: 'sess-1', profile: 'default',
    decision: { kind: 'clarify', request_id: 'talaria-push-install-over', question: 'Overflow?', choices: ['Yes'] },
  };
  const rejected = await api(base, '/v1/events', { method: 'POST', credential: `${registered.installation.id}.${gateway.gatewayId}.${gateway.gatewaySecret}`, body: event });
  assert.equal(rejected.status, 429);
  assert.deepEqual(rejected.json, { error: 'decision_capacity_exceeded' });
  const answer = await api(base, '/v1/decisions/talaria-push-install-0/respond', {
    method: 'POST', credential: `${registered.installation.id}.${registered.deviceSecret}`, body: { answer: 'Done', gateway_id: gateway.gatewayId },
  });
  assert.equal(answer.status, 200);
  const retried = await api(base, '/v1/events', { method: 'POST', credential: `${registered.installation.id}.${gateway.gatewayId}.${gateway.gatewaySecret}`, body: event });
  assert.equal(retried.status, 202);
  assert.equal(retried.json.accepted, true);
});

test('decision_cards=false still delivers the plain banner and parks the decision undeliverable', async () => {
  const phone = await pairedPhone(baseUrl, google, {
    preferences: { enabled: true, input_needed: true, decision_cards: false },
    gatewayName: 'no-cards gateway',
  });
  const gatewayCredential = phone.gateway;

  const event = await api(baseUrl, '/v1/events', {
    method: 'POST',
    credential: gatewayCredential,
    body: {
      type: 'input.needed',
      event_id: 'input:nocards00001',
      session_id: 'sess-nocards',
      profile: 'default',
      decision: {
        kind: 'clarify',
        request_id: 'talaria-push-nocards1',
        question: 'Which environment?',
        questions: [{ qid: 'q0', question: 'Which environment?', choices: ['staging'], multi_select: false }],
      },
    },
  });
  // The user disabled answerable cards, NOT notifications: the ordinary
  // input.needed banner is still delivered.
  assert.equal(event.status, 202);
  assert.deepEqual(event.json, { accepted: true, delivered: true });

  // The parked decision is undeliverable, so the plugin immediately falls
  // back to the native clarify path instead of waiting out its budget.
  const poll = await api(baseUrl, '/v1/decisions/talaria-push-nocards1', { credential: gatewayCredential });
  assert.equal(poll.status, 200);
  assert.equal(poll.json.status, 'pending');
  assert.equal(poll.json.deliverable, false);
  assert.deepEqual(poll.json.remaining, ['q0'], 'the qids survive even when the card does not');
});

test('a Google refusal after parking flips a deliverable decision to undeliverable', async () => {
  const phone = await pairedPhone(baseUrl, google, { preferences: { enabled: true, decision_cards: true }, gatewayName: 'reject gateway' });
  const gatewayCredential = phone.gateway;

  // Google refuses the send AFTER intake parked the decision as deliverable
  // (the structured card survived the size guard).
  google.mode = REFUSED;
  try {
    const event = await api(baseUrl, '/v1/events', {
      method: 'POST',
      credential: gatewayCredential,
      body: {
        type: 'input.needed',
        event_id: 'input:reject000001',
        session_id: 'sess-reject',
        profile: 'default',
        decision: {
          kind: 'clarify',
          request_id: 'talaria-push-reject1',
          question: 'Which environment?',
          questions: [{ qid: 'q0', question: 'Which environment?', choices: ['staging'], multi_select: false }],
        },
      },
    });
    // Google refused the send: the relay reports the failure upstream…
    assert.equal(event.status, 502);
    assert.equal(event.json.error, 'push_rejected');
  } finally {
    google.reset();
  }

  // …but the parked decision is no longer deliverable — the plugin's next
  // poll sees it and promptly falls back to the native clarify path instead
  // of polling a decision no device ever received.
  const poll = await api(baseUrl, '/v1/decisions/talaria-push-reject1', { credential: gatewayCredential });
  assert.equal(poll.status, 200);
  assert.equal(poll.json.status, 'pending');
  assert.equal(poll.json.deliverable, false, 'a Google refusal must flip the parked deliverable flag');
  assert.deepEqual(poll.json.remaining, ['q0']);
});

test('unknown qid on a live batch decision is a bad request, not a missing decision', async () => {
  const phone = await pairedPhone(baseUrl, google, { preferences: { enabled: false, decision_cards: true }, gatewayName: 'qid gateway' });
  const deviceCredential = phone.credential;
  const gatewayCredential = phone.gateway;
  await api(baseUrl, '/v1/events', {
    method: 'POST',
    credential: gatewayCredential,
    body: {
      type: 'input.needed',
      event_id: 'input:qidinvalid01',
      session_id: 'sess-qid',
      profile: 'default',
      decision: {
        kind: 'clarify',
        request_id: 'talaria-push-qidcheck',
        question: 'One?',
        questions: [{ qid: 'q0', question: 'One?', choices: ['a'], multi_select: false }],
      },
    },
  });
  const answer = await api(baseUrl, '/v1/decisions/talaria-push-qidcheck/respond', {
    method: 'POST',
    credential: deviceCredential,
    body: { question_id: 'q9', answer: 'a', gateway_id: phone.gatewayId },
  });
  assert.equal(answer.status, 400);
  assert.equal(answer.json.error, 'invalid_question_id');
  // The established meanings are untouched: the live decision still polls.
  const poll = await api(baseUrl, '/v1/decisions/talaria-push-qidcheck', { credential: gatewayCredential });
  assert.equal(poll.json.status, 'pending');
});

test('an ordinary multi-question batch is delivered with the full qid set (single structured copy)', async () => {
  // Representative 3-question batch (~420-char texts, 3 choices each) —
  // large enough that two copies of the card would not fit FCM's 4 KB of
  // data, as two copies once rode in every push.
  const phone = await pairedPhone(baseUrl, google, { preferences: { enabled: true, decision_cards: true }, gatewayName: 'midsize gateway' });
  const gatewayCredential = phone.gateway;

  const longText = ('Describe this rollout step in detail: which services are affected, the expected downtime window, '
    + 'the rollback procedure if validation fails, and who signs off on completion for the platform team to proceed. ').repeat(2);
  const event = await api(baseUrl, '/v1/events', {
    method: 'POST',
    credential: gatewayCredential,
    body: {
      type: 'input.needed',
      event_id: 'input:midsizee2e01',
      session_id: 'sess-midsize-e2e',
      profile: 'default',
      decision: {
        kind: 'clarify',
        request_id: 'talaria-push-midsize1',
        question: longText,
        questions: [0, 1, 2].map((index) => ({
          qid: `q${index}`,
          question: longText + `Variant ${index}.`,
          choices: [
            `Proceed with option ${index} now`,
            `Delay option ${index} to the next window`,
            `Escalate option ${index} for review`,
          ],
          multi_select: index === 2,
        })),
      },
    },
  });
  assert.equal(event.status, 202);
  assert.deepEqual(event.json, { accepted: true, delivered: true });

  const poll = await api(baseUrl, '/v1/decisions/talaria-push-midsize1', { credential: gatewayCredential });
  assert.equal(poll.json.status, 'pending');
  assert.equal(poll.json.deliverable, true, 'a representative batch must keep its answerable card');
  assert.deepEqual(poll.json.remaining, ['q0', 'q1', 'q2'], 'all qids preserved');

  // One routing copy carries the card, the rest of the data is the alert.
  const [push] = pushesTo(google, phone.token);
  assert.deepEqual(Object.keys(push.data).sort(), ['body', 'sound', 'talaria', 'thread', 'title']);
  assert.deepEqual(routingOf(push).decision.questions.map((question) => question.qid), ['q0', 'q1', 'q2']);
  assert.ok(dataBytes(push.data) <= FCM_MAX_DATA_BYTES);
  assert.ok(dataBytes(push.data) > FCM_MAX_DATA_BYTES / 2, 'two copies of this card would not fit');
});

test('a dropped connection to Google after parking flips the decision undeliverable', async () => {
  const phone = await pairedPhone(baseUrl, google, { preferences: { enabled: true, decision_cards: true }, gatewayName: 'throw gateway' });
  const gatewayCredential = phone.gateway;

  google.mode = 'drop';
  try {
    const event = await api(baseUrl, '/v1/events', {
      method: 'POST',
      credential: gatewayCredential,
      body: {
        type: 'input.needed',
        event_id: 'input:throw000001',
        session_id: 'sess-throw',
        profile: 'default',
        decision: {
          kind: 'clarify',
          request_id: 'talaria-push-throw1',
          question: 'Which environment?',
          questions: [{ qid: 'q0', question: 'Which environment?', choices: ['staging'], multi_select: false }],
        },
      },
    });
    // A thrown send is reported as a transport failure…
    assert.equal(event.status, 502);
    assert.equal(event.json.error, 'push_unreachable');
  } finally {
    google.reset();
  }

  // …and the parked decision is undeliverable, exactly like a returned
  // rejection, so the plugin promptly falls back to the native path.
  const poll = await api(baseUrl, '/v1/decisions/talaria-push-throw1', { credential: gatewayCredential });
  assert.equal(poll.json.status, 'pending');
  assert.equal(poll.json.deliverable, false);
});

test('release succeeds even after the poll quota is exhausted', async () => {
  const phone = await pairedPhone(baseUrl, google, { preferences: { enabled: false, decision_cards: true }, gatewayName: 'quota gateway' });
  const deviceCredential = phone.credential;
  const gatewayCredential = phone.gateway;
  await api(baseUrl, '/v1/events', {
    method: 'POST',
    credential: gatewayCredential,
    body: {
      type: 'input.needed',
      event_id: 'input:quota000001',
      session_id: 'sess-quota',
      profile: 'default',
      decision: {
        kind: 'clarify',
        request_id: 'talaria-push-quota1',
        question: 'One?',
        questions: [{ qid: 'q0', question: 'One?', choices: ['a'], multi_select: false }],
      },
    },
  });

  // Burn the poll bucket (120/60s) plus margin — many polls will now 429.
  for (let i = 0; i < 125; i += 1) {
    await api(baseUrl, '/v1/decisions/talaria-push-quota1', { credential: gatewayCredential });
  }

  // Release must NOT be starved by the exhausted poll quota…
  const released = await api(baseUrl, '/v1/decisions/talaria-push-quota1', { method: 'DELETE', credential: gatewayCredential });
  assert.equal(released.status, 200);
  // …and the late device answer still reports decision_released.
  const late = await api(baseUrl, '/v1/decisions/talaria-push-quota1/respond', {
    method: 'POST',
    credential: deviceCredential,
    body: { question_id: 'q0', answer: 'a', gateway_id: phone.gatewayId },
  });
  assert.equal(late.status, 410);
  assert.equal(late.json.error, 'decision_released');
});

test('a Google that cannot be reached parks the decision undeliverable and the relay survives', async () => {
  // The dead-origin relay runs the real FcmClient transport pointed at a
  // closed loopback port, so the connection is refused outright — the
  // failure shape the fake Google's dropped connection cannot produce.
  // Notifications are ENABLED (default preferences) so intake actually calls
  // the transport instead of skipping it.
  const registered = await api(deadBaseUrl, '/v1/installations', {
    method: 'POST',
    body: registration(fcmToken()),
  });
  assert.equal(registered.status, 201);
  const phone = { ...registered.json };
  const { gateway: gatewayCredential } = await pairHost(deadBaseUrl, phone, { gatewayName: 'dead-origin gateway' });

  const event = await api(deadBaseUrl, '/v1/events', {
    method: 'POST',
    credential: gatewayCredential,
    body: {
      type: 'input.needed',
      event_id: 'input:deadfcm00001',
      session_id: 'sess-dead',
      profile: 'default',
      decision: {
        kind: 'clarify',
        request_id: 'talaria-push-dead1',
        question: 'Which environment?',
        choices: ['staging', 'prod'],
      },
    },
  });
  // The thrown transport failure is converted to the same outcome as a
  // returned rejection: reported upstream, decision flipped undeliverable.
  assert.equal(event.status, 502);
  assert.equal(event.json.error, 'push_unreachable');

  // The decision WAS parked before the send, and the plugin's poller must
  // see deliverable:false so it stops waiting and falls back to the native
  // clarify path.
  const poll = await api(deadBaseUrl, '/v1/decisions/talaria-push-dead1', { credential: gatewayCredential });
  assert.equal(poll.status, 200);
  assert.deepEqual(poll.json, { status: 'pending', deliverable: false });

  // Still alive: another poll works too, and a plain notification (no
  // decision) takes the same thrown-transport path to 502.
  const health = await api(deadBaseUrl, '/healthz');
  assert.equal(health.status, 200);
  assert.deepEqual(health.json, { ok: true });
  const plain = await api(deadBaseUrl, '/v1/events', {
    method: 'POST',
    credential: gatewayCredential,
    body: {
      type: 'response.ready',
      event_id: 'response:deadfcm001',
      session_id: 'sess-dead',
      profile: 'default',
    },
  });
  assert.equal(plain.status, 502);
  assert.equal(plain.json.error, 'push_unreachable');
  const alivePoll = await api(deadBaseUrl, '/v1/decisions/talaria-push-dead1', { credential: gatewayCredential });
  assert.equal(alivePoll.status, 200);
});

test('pairing dashboard binding end to end: bound at creation, carried on every push, event body never rebinds', async () => {
  const phone = await registerPhone(baseUrl, google);
  const installationId = phone.installation.id;
  const deviceCredential = phone.credential;

  // A malformed dashboard_id is rejected at pairing creation: the device
  // must learn its binding did not land, never silently pair unscoped.
  const malformed = await api(baseUrl, `/v1/installations/${installationId}/pairings`, {
    method: 'POST',
    credential: deviceCredential,
    body: { dashboard_id: 'not-a-uuid' },
  });
  assert.equal(malformed.status, 400);
  assert.equal(malformed.json.error, 'invalid_dashboard_id');

  const dashboardId = '0f5c8a34-1b2d-4e5f-8a9b-0c1d2e3f4a5b';
  const { gateway: gatewayCredential } = await pairHost(baseUrl, phone, { dashboardId, gatewayName: 'bound gateway' });
  assert.equal(savedGateway(dataPath, installationId, 'bound gateway').dashboardId, dashboardId, 'the claim binds the gateway');

  // A hello records the gateway and pushes nothing.
  const hello = await api(baseUrl, '/v1/events', {
    method: 'POST',
    credential: gatewayCredential,
    body: {
      type: 'plugin.hello',
      event_id: 'hello:020abcdef12',
      plugin_version: '0.3.0',
      plugin_capabilities: ['approval-decisions', 'clarify-loop', 'version-reporting'],
    },
  });
  assert.equal(hello.status, 202);
  assert.equal(pushesTo(google, phone.token).length, 0);

  // A plugin-supplied dashboard_id in the event body is dropped at the
  // trust boundary and can never rebind the gateway: the push carries the
  // pairing's binding, and the stored binding survives unchanged.
  const rebinding = await api(baseUrl, '/v1/events', {
    method: 'POST',
    credential: gatewayCredential,
    body: {
      type: 'response.ready',
      event_id: 'response:rebind000001',
      session_id: 'sess-1',
      dashboard_id: '99999999-9999-4999-8999-999999999999',
    },
  });
  assert.equal(rebinding.status, 202);
  const [push] = pushesTo(google, phone.token);
  assert.equal(routingOf(push).dashboard_id, dashboardId, 'event-body dashboard_id never rebinds the gateway');
  assert.equal(savedGateway(dataPath, installationId, 'bound gateway').dashboardId, dashboardId);
});

test('a literal null or non-object JSON body never 500s the pairing route', async () => {
  const phone = await registerPhone(baseUrl, google);
  const installationId = phone.installation.id;
  const deviceCredential = phone.credential;

  // Deliberate contract: ONLY a JSON object is a valid body. A top-level
  // null/array/scalar parses cleanly but is a client mistake — rejected as
  // 400 invalid_json (the pre-hardening bug was a 500 TypeError). Never
  // normalized to {} (which would turn a malformed request into a
  // successful no-field operation).
  for (const rawBody of ['null', '[1,2]', '"text"', '42']) {
    const response = await fetch(`${baseUrl}/v1/installations/${installationId}/pairings`, {
      method: 'POST',
      headers: {
        authorization: `Bearer ${deviceCredential}`,
        'content-type': 'application/json',
      },
      body: rawBody,
    });
    assert.equal(response.status, 400, `body ${rawBody} must be invalid_json`);
    assert.equal((await response.json()).error, 'invalid_json');
  }

  // Same strictness on the unauthenticated registration route.
  const nullRegistration = await fetch(`${baseUrl}/v1/installations`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: 'null',
  });
  assert.equal(nullRegistration.status, 400);

  // An EMPTY body stays legal where routes tolerate it (reads as {}).
  const emptyBody = await fetch(`${baseUrl}/v1/installations/${installationId}/pairings`, {
    method: 'POST',
    headers: {
      authorization: `Bearer ${deviceCredential}`,
      'content-type': 'application/json',
    },
  });
  assert.equal(emptyBody.status, 201, 'empty body pairs unbound');
});

test('pairing rejects a nil dashboard UUID and treats explicit null as absent', async () => {
  const phone = await registerPhone(baseUrl, google);
  const installationId = phone.installation.id;
  const deviceCredential = phone.credential;

  const nilUuid = await api(baseUrl, `/v1/installations/${installationId}/pairings`, {
    method: 'POST',
    credential: deviceCredential,
    body: { dashboard_id: '00000000-0000-0000-0000-000000000000' },
  });
  assert.equal(nilUuid.status, 400);
  assert.equal(nilUuid.json.error, 'invalid_dashboard_id');

  const explicitNull = await api(baseUrl, `/v1/installations/${installationId}/pairings`, {
    method: 'POST',
    credential: deviceCredential,
    body: { dashboard_id: null },
  });
  assert.equal(explicitNull.status, 201, 'explicit null means no binding, matching the absent shape');
});

test('plugin-supplied dashboard_id can never affect the real FCM message; null pairing stays unbound', async () => {
  // Assertions run against the messages the fake Google accepted, not the
  // event ack.
  const phone = await registerPhone(baseUrl, google);
  const installationId = phone.installation.id;
  const deviceCredential = phone.credential;

  // Gateway 1: bound to dashboard A. Gateway 2: explicit null (unbound).
  const dashboardId = '0f5c8a34-1b2d-4e5f-8a9b-0c1d2e3f4a5b';
  // One active pairing per installation at a time: create AND claim the
  // bound pairing before rotating to the unbound one.
  const { gateway: boundCredential } = await pairHost(baseUrl, phone, { dashboardId, gatewayName: 'bound' });
  const unboundPairing = await api(baseUrl, `/v1/installations/${installationId}/pairings`, {
    method: 'POST',
    credential: deviceCredential,
    body: { dashboard_id: null },
  });
  assert.equal(unboundPairing.status, 201, 'explicit null pairs with no binding');
  const unboundClaim = await api(baseUrl, '/v1/pairings/claim', {
    method: 'POST',
    body: { pairing_code: unboundPairing.json.pairing_code, gateway_name: 'unbound' },
  });
  assert.equal(unboundClaim.status, 200);

  // Gateway 1 sends an event whose body tries to REBIND itself to
  // dashboard B (and gateway 2 sends an ordinary one).
  const attackerEvent = await api(baseUrl, '/v1/events', {
    method: 'POST',
    credential: boundCredential,
    body: {
      type: 'response.ready',
      event_id: 'response:attack000001',
      session_id: 'sess-attack',
      dashboard_id: '99999999-9999-4999-8999-999999999999',
    },
  });
  assert.equal(attackerEvent.status, 202);
  const plainEvent = await api(baseUrl, '/v1/events', {
    method: 'POST',
    credential: unboundClaim.json.credential,
    body: {
      type: 'response.ready',
      event_id: 'response:plain000001',
      session_id: 'sess-plain',
    },
  });
  assert.equal(plainEvent.status, 202);

  // Assert on the FCM messages Google accepted.
  const messages = pushesTo(google, phone.token);
  const attacker = messages.find((message) => routingOf(message).session_id === 'sess-attack');
  const plain = messages.find((message) => routingOf(message).session_id === 'sess-plain');
  assert.ok(attacker, 'attacker notification sent');
  assert.ok(plain, 'plain notification sent');
  assert.equal(
    routingOf(attacker).dashboard_id,
    dashboardId,
    'the outgoing push carries the AUTHENTICATED gateway binding, not the event body'
  );
  assert.equal(
    'dashboard_id' in routingOf(plain),
    false,
    'an unbound gateway keeps the exact unbound wire shape'
  );
  assert.ok(
    !JSON.stringify(attacker).includes('99999999'),
    'the attacker-chosen identity never reaches the wire'
  );
});

test('full collision matrix: one installation, two gateways, identical dashboards', async () => {
  // GA -> dashboard A, GB -> dashboard B. Both dashboards use
  // profile=default, session=default, the SAME event_id and the SAME
  // plugin-minted request_id. Nothing may swallow, answer, cancel, or
  // rebind anything across the gateway boundary.
  const phone = await registerPhone(baseUrl, google);
  const deviceCredential = phone.credential;

  const dashboardA = '0f5c8a34-1b2d-4e5f-8a9b-0c1d2e3f4a5b';
  const dashboardB = '11111111-2222-4333-8444-555555555555';
  const { gateway: credA, gatewayId: gaId } = await pairHost(baseUrl, phone, { dashboardId: dashboardA, gatewayName: 'GA' });
  const { gateway: credB, gatewayId: gbId } = await pairHost(baseUrl, phone, { dashboardId: dashboardB, gatewayName: 'GB' });

  // Identical plugin.hello ids: both gateways recorded (scoped dedupe).
  const helloBody = {
    type: 'plugin.hello', event_id: 'hello:collisio001', plugin_version: '0.3.0',
    plugin_capabilities: ['approval-decisions', 'clarify-loop', 'version-reporting'],
  };
  assert.equal((await api(baseUrl, '/v1/events', { method: 'POST', credential: credA, body: helloBody })).status, 202);
  assert.equal((await api(baseUrl, '/v1/events', { method: 'POST', credential: credB, body: helloBody })).status, 202);

  // Identical event_id values: BOTH accepted, neither dedupes the other.
  const evt = { type: 'response.ready', event_id: 'response:sameevent1', session_id: 'default', profile: 'default' };
  assert.deepEqual(
    (await api(baseUrl, '/v1/events', { method: 'POST', credential: credA, body: evt })).json,
    { accepted: true, delivered: true },
  );
  assert.deepEqual(
    (await api(baseUrl, '/v1/events', { method: 'POST', credential: credB, body: evt })).json,
    { accepted: true, delivered: true },
    "GB's identical event_id must NOT dedupe against GA's",
  );

  // Identical plugin-minted request ids: BOTH clarify decisions park.
  const clarifyBody = {
    type: 'input.needed',
    event_id: 'input:sameevent1',
    session_id: 'default',
    profile: 'default',
    decision: { kind: 'clarify', request_id: 'talaria-push-same-request', question: 'Which?', choices: ['Red', 'Blue'] },
  };
  assert.equal((await api(baseUrl, '/v1/events', { method: 'POST', credential: credA, body: clarifyBody })).status, 202);
  assert.equal((await api(baseUrl, '/v1/events', { method: 'POST', credential: credB, body: clarifyBody })).status, 202);

  const respond = (gatewayId, body) => api(baseUrl, '/v1/decisions/talaria-push-same-request/respond', {
    method: 'POST', credential: deviceCredential, body: { gateway_id: gatewayId, ...body },
  });

  // A answers with the discriminator: only A's decision locks.
  const answerA = await respond(gaId, { answer: 'Red' });
  assert.equal(answerA.status, 200);
  assert.deepEqual(answerA.json, { status: 'answered' });

  // B polls with its OWN credential: still pending — A's answer never leaked.
  const pollB = await api(baseUrl, '/v1/decisions/talaria-push-same-request', { credential: credB });
  assert.equal(pollB.json.status, 'pending', "A's answer must not resolve B's same-id decision");
  // A's poller sees its own answered state.
  const pollA = await api(baseUrl, '/v1/decisions/talaria-push-same-request', { credential: credA });
  assert.equal(pollA.json.status, 'answered');

  // B answers with the discriminator: only B locks.
  assert.equal((await respond(gbId, { answer: 'Blue' })).status, 200);
  assert.equal((await api(baseUrl, '/v1/decisions/talaria-push-same-request', { credential: credB })).json.status, 'answered');

  // A plugin-supplied dashboard_id/gateway_id can never rebind routing:
  // GA sends an event LYING about both. The routing discriminators must
  // still be GA's authenticated identities (asserted on the messages below).
  const lyingEvent = {
    type: 'input.needed',
    event_id: 'input:sameevent3',
    session_id: 'default',
    profile: 'default',
    dashboard_id: dashboardB,
    gateway_id: gbId,
    decision: { kind: 'clarify', request_id: 'talaria-push-same-three', question: 'Lie?', choices: ['x'] },
  };
  assert.equal((await api(baseUrl, '/v1/events', { method: 'POST', credential: credA, body: lyingEvent })).status, 202);

  // An answer WITHOUT a discriminator is refused, never guessed: not while
  // two gateways hold the same live id, and not once only one does.
  const freshClarify = {
    ...clarifyBody,
    event_id: 'input:sameevent2',
    decision: { ...clarifyBody.decision, request_id: 'talaria-push-same-two' },
  };
  await api(baseUrl, '/v1/events', { method: 'POST', credential: credA, body: freshClarify });
  await api(baseUrl, '/v1/events', { method: 'POST', credential: credB, body: freshClarify });
  const unnamedAmbiguous = await api(baseUrl, '/v1/decisions/talaria-push-same-two/respond', {
    method: 'POST', credential: deviceCredential, body: { answer: 'Guess' },
  });
  assert.equal(unnamedAmbiguous.status, 400);
  assert.equal(unnamedAmbiguous.json.error, 'invalid_gateway_id');

  // Cancel A's fresh decision via GA's OWN credential; B's stays pending.
  assert.equal((await api(baseUrl, '/v1/decisions/talaria-push-same-two', { method: 'DELETE', credential: credA })).status, 200);
  assert.equal(
    (await api(baseUrl, '/v1/decisions/talaria-push-same-two', { credential: credB })).json.status,
    'pending',
    'cancelling A cannot mutate B',
  );

  // One live holder left: still refused without its gateway, answered with it.
  const unnamedUnambiguous = await api(baseUrl, '/v1/decisions/talaria-push-same-two/respond', {
    method: 'POST', credential: deviceCredential, body: { answer: 'Only-B-left' },
  });
  assert.equal(unnamedUnambiguous.status, 400);
  assert.equal(unnamedUnambiguous.json.error, 'invalid_gateway_id');
  assert.equal((await api(baseUrl, '/v1/decisions/talaria-push-same-two', { credential: credB })).json.status, 'pending');
  const namedB = await api(baseUrl, '/v1/decisions/talaria-push-same-two/respond', {
    method: 'POST', credential: deviceCredential, body: { answer: 'Only-B-left', gateway_id: gbId },
  });
  assert.equal(namedB.status, 200);
  assert.equal(
    (await api(baseUrl, '/v1/decisions/talaria-push-same-two', { credential: credB })).json.status,
    'answered',
  );

  // Zero matches keeps the existing unknown behavior.
  assert.equal(
    (await api(baseUrl, '/v1/decisions/talaria-push-never-parked/respond', {
      method: 'POST', credential: deviceCredential, body: { answer: 'x', gateway_id: gaId },
    })).status,
    404,
  );

  // The FCM messages Google accepted: scope isolation + the trust boundary.
  const messages = pushesTo(google, phone.token);
  const decisionEntries = messages.filter((message) => routingOf(message).decision?.request_id === 'talaria-push-same-request');
  assert.equal(decisionEntries.length, 2, 'both same-id decisions were delivered');
  const [firstEntry, secondEntry] = decisionEntries;
  assert.notEqual(firstEntry.data.thread, secondEntry.data.thread, 'threads never merge across dashboards');
  // The discriminators come from the AUTHENTICATED gateways — GA's lying
  // event (dashboard_id=B, gateway_id=GB in the body) must NOT rebind.
  // Each entry's discriminators pair with its OWN authenticated gateway:
  // the entry carrying GA's gateway_id carries dashboard A, and GB's
  // carries dashboard B (random UUIDs — never assume sort order).
  const byGateway = Object.fromEntries(
    decisionEntries.map((message) => [routingOf(message).gateway_id, routingOf(message).dashboard_id]));
  assert.deepEqual(byGateway, { [gaId]: dashboardA, [gbId]: dashboardB });
  // GA's lying decision entry: routing carries GA's identity, never GB's.
  const lyingEntry = messages.find((message) => routingOf(message).decision?.request_id === 'talaria-push-same-three');
  assert.equal(routingOf(lyingEntry).gateway_id, gaId, 'event-body gateway_id is dropped');
  assert.equal(routingOf(lyingEntry).dashboard_id, dashboardA, 'event-body dashboard_id is dropped');
});

test('encrypted clarify: question ids parked, card flag drives deliverability, sealed answers kept whole', async () => {
  const phone = await pairedPhone(baseUrl, google, { gatewayName: 'e2e gateway' });
  const deviceCredential = phone.credential;
  const gatewayCredential = phone.gateway;
  const gatewayId = phone.gatewayId;
  const kid = '0123456789abcdef0123456789abcdef';
  const encrypted = (requestId, card, eventId) => ({
    type: 'input.needed',
    event_id: eventId,
    e2e: { v: 1, kid, msg: eventId, iat: Math.floor(Date.now() / 1000), tok: '0123456789abcdef', z: 0, req: requestId, n: 'AAAAAAAAAAAAAAAA', ct: 'B'.repeat(300) },
    clarify: { request_id: requestId, qids: ['q0', 'q1'], card },
  });

  const fits = await api(baseUrl, '/v1/events', { method: 'POST', credential: gatewayCredential, body: encrypted('talaria-push-e2e000000001', true, 'input:e2e00000000000000000000000000001') });
  assert.equal(fits.status, 202);
  const poll = await api(baseUrl, '/v1/decisions/talaria-push-e2e000000001', { credential: gatewayCredential });
  assert.equal(poll.json.status, 'pending');
  assert.deepEqual(poll.json.remaining, ['q0', 'q1']);
  assert.notEqual(poll.json.deliverable, false);

  // The card didn't fit the envelope: parked undeliverable, so the plugin
  // falls back to Hermes' own clarify path on its first poll.
  const tooBig = await api(baseUrl, '/v1/events', { method: 'POST', credential: gatewayCredential, body: encrypted('talaria-push-e2e000000002', false, 'input:e2e00000000000000000000000000002') });
  assert.equal(tooBig.status, 202);
  const tooBigPoll = await api(baseUrl, '/v1/decisions/talaria-push-e2e000000002', { credential: gatewayCredential });
  assert.equal(tooBigPoll.json.deliverable, false);

  // A sealed answer longer than the plaintext cap is stored and returned whole.
  const sealed = `e2e1.${kid}.AAAAAAAAAAAAAAAA.${'C'.repeat(5000)}`;
  const answered = await api(baseUrl, '/v1/decisions/talaria-push-e2e000000001/respond', {
    method: 'POST',
    credential: deviceCredential,
    body: { answer: sealed, question_id: 'q0', gateway_id: gatewayId },
  });
  assert.equal(answered.status, 200);
  const afterAnswer = await api(baseUrl, '/v1/decisions/talaria-push-e2e000000001', { credential: gatewayCredential });
  assert.equal(afterAnswer.json.answers.q0, sealed);

  // Text that only looks like a sealed answer is an ordinary answer (so a
  // keyless pairing can still answer "e2e1.…"); the plugin of an encrypted
  // pairing rejects it because it doesn't open.
  const lookalike = `e2e1.${kid}.short.payload`;
  const plain = await api(baseUrl, '/v1/decisions/talaria-push-e2e000000001/respond', {
    method: 'POST',
    credential: deviceCredential,
    body: { answer: lookalike, question_id: 'q1', gateway_id: gatewayId },
  });
  assert.equal(plain.status, 200);
  const afterPlain = await api(baseUrl, '/v1/decisions/talaria-push-e2e000000001', { credential: gatewayCredential });
  assert.equal(afterPlain.json.answers.q1, lookalike);

  // An answer the relay can't keep whole is refused, never cut: a cut sealed
  // answer no longer opens (the plugin would drop the whole poll after the
  // phone was told "answered"), and a cut plaintext answer isn't what the
  // user wrote. The question stays open for another try or surface.
  const parked = await api(baseUrl, '/v1/events', { method: 'POST', credential: gatewayCredential, body: encrypted('talaria-push-e2e000000003', true, 'input:e2e00000000000000000000000000003') });
  assert.equal(parked.status, 202);
  const respond = (answer, questionId) => api(baseUrl, '/v1/decisions/talaria-push-e2e000000003/respond', {
    method: 'POST',
    credential: deviceCredential,
    body: { answer, question_id: questionId, gateway_id: gatewayId },
  });
  const oversizedSealed = await respond(`e2e1.${kid}.AAAAAAAAAAAAAAAA.${'D'.repeat(11_200)}`, 'q0');
  assert.equal(oversizedSealed.status, 413);
  assert.equal(oversizedSealed.json.error, 'answer_too_large');
  const malformedSealed = await respond(`e2e1.${kid}.AAAAAAAAAAAAAAAA.short`, 'q0');
  assert.equal(malformedSealed.status, 400);
  assert.equal(malformedSealed.json.error, 'invalid_answer');
  const oversizedPlain = await respond(`${'x'.repeat(2000)}y`, 'q1');
  assert.equal(oversizedPlain.status, 413);
  assert.equal(oversizedPlain.json.error, 'answer_too_large');
  const stillOpen = await api(baseUrl, '/v1/decisions/talaria-push-e2e000000003', { credential: gatewayCredential });
  assert.deepEqual(stillOpen.json.remaining, ['q0', 'q1']);
  assert.equal(stillOpen.json.answers?.q0, undefined);
  // At the bound, and with surrounding whitespace trimmed first, it fits.
  const atBound = await respond(`  ${'x'.repeat(2000)}\n`, 'q1');
  assert.equal(atBound.status, 200);
  const afterBound = await api(baseUrl, '/v1/decisions/talaria-push-e2e000000003', { credential: gatewayCredential });
  assert.equal(afterBound.json.answers.q1, 'x'.repeat(2000));
});

test('a location request is a silent, short-lived data message to the phone, never a notification', async () => {
  const isolatedPath = join(dir, 'relay-data-locate.json');
  const fixture = new RelayStore(isolatedPath);
  const token = fcmToken();
  const created = fixture.createInstallation({ bundleId: PACKAGE, deviceToken: token });
  const pairing = fixture.createPairing(created.installation.id);
  const claim = fixture.claimPairing(pairing.code, 'locate target');
  const isolated = await relayOn(isolatedPath);
  const send = (eventId, body = {}) => api(isolated.base, '/v1/events', {
    method: 'POST',
    credential: `${created.installation.id}.${claim.gatewayId}.${claim.gatewaySecret}`,
    body: { type: 'location.requested', event_id: eventId, profile: 'work', locate: { id: 'l_0123456789abcdefXYZ' }, ...body },
  });
  try {
    assert.deepEqual(await send('locate:000000000000000000000002'), { status: 202, json: { accepted: true, delivered: true } });
    const [push] = pushesTo(google, token);
    assert.deepEqual(Object.keys(push.data), ['talaria'], 'nothing for the phone to show');
    assert.equal(push.android.priority, 'HIGH');
    const ttl = Number.parseInt(push.android.ttl, 10);
    assert.ok(ttl > 0 && ttl <= 60, push.android.ttl);
    const routing = routingOf(push);
    assert.equal(routing.type, 'location.requested');
    assert.equal(routing.profile, 'work');
    assert.deepEqual(routing.locate, { id: 'l_0123456789abcdefXYZ' });
    assert.equal(routing.gateway_id, claim.gatewayId);

    assert.deepEqual(await send('locate:000000000000000000000003', { locate: { id: 'bad' } }),
      { status: 400, json: { error: 'invalid_location_request' } }, 'an unsealed request needs its id');
    assert.deepEqual(await send('locate:000000000000000000000003', { locate: { id: 'bad' } }),
      { status: 200, json: { accepted: true, duplicate: true } }, 'a malformed request keeps its id: a retry would fail the same way');
    assert.equal(pushesTo(google, token).length, 1);
  } finally {
    await stopRelay(isolated);
  }
});

test('phone changes: an alert notifies, a background change wakes silently, and the preference stops both', async () => {
  const isolatedPath = join(dir, 'relay-data-phone.json');
  const fixture = new RelayStore(isolatedPath);
  const token = fcmToken();
  const created = fixture.createInstallation({ bundleId: PACKAGE, deviceToken: token });
  const pairing = fixture.createPairing(created.installation.id);
  const claim = fixture.claimPairing(pairing.code, 'phone target');
  const isolated = await relayOn(isolatedPath);
  const base = isolated.base;
  const device = `${created.installation.id}.${created.deviceSecret}`;
  const send = (eventId, body = {}) => api(base, '/v1/events', {
    method: 'POST',
    credential: `${created.installation.id}.${claim.gatewayId}.${claim.gatewaySecret}`,
    body: { type: 'phone.changes', event_id: eventId, profile: 'work', title: 'Hermes wants to add an event', body: 'Dentist · Fri 16 Oct 15:00', ...body },
  });
  try {
    assert.deepEqual(await send('phone:ch_000000000000000001'), { status: 202, json: { accepted: true, delivered: true } });
    const [alert] = pushesTo(google, token);
    assert.equal(alert.data.category, 'PHONE_CHANGE');
    assert.equal(alert.data.title, 'Hermes has a change for you');
    assert.equal(alert.android.priority, 'HIGH', 'an ordinary, urgent notification');

    assert.deepEqual(await send('phone:ch_000000000000000002', { mode: 'background' }),
      { status: 202, json: { accepted: true, delivered: true, background: true } });
    const background = pushesTo(google, token)[1];
    assert.equal(background.android.priority, 'NORMAL');
    assert.deepEqual(Object.keys(background.data), ['talaria'], 'nothing for the phone to show');
    assert.equal(routingOf(background).mode, 'background');
    assert.equal(routingOf(background).gateway_id, claim.gatewayId);
    assert.ok(!JSON.stringify(background).includes('Dentist'));

    assert.deepEqual(await send('phone:ch_000000000000000003', { mode: 'loud' }), { status: 400, json: { error: 'invalid_mode' } });
    const off = await api(base, `/v1/installations/${created.installation.id}`, { method: 'PUT', credential: device, body: { preferences: { phone_changes: false } } });
    assert.equal(off.json.installation.preferences.phone_changes, false);
    assert.deepEqual(await send('phone:ch_000000000000000004'), { status: 202, json: { accepted: true, delivered: false } });
    assert.deepEqual(await send('phone:ch_000000000000000005', { mode: 'background' }), { status: 202, json: { accepted: true, delivered: false } });
    assert.equal(pushesTo(google, token).length, 2);
  } finally {
    await stopRelay(isolated);
  }
});
