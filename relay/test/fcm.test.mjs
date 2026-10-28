import { strict as assert } from 'node:assert';
import { createVerify, generateKeyPairSync } from 'node:crypto';
import { test } from 'node:test';

import { FCM_MAX_DATA_BYTES, FcmClient, dataBytes, fcmConfigFromEnv, fcmMessage, validateFcmToken } from '../src/fcm.mjs';
import { notificationFor, phoneBackgroundPushFor } from '../src/server.mjs';
import { tokenChallengePush } from '../src/token-proof.mjs';

const { privateKey, publicKey } = generateKeyPairSync('rsa', { modulusLength: 2048 });
const serviceAccount = {
  type: 'service_account',
  project_id: 'hermes-test',
  private_key_id: 'key-1',
  private_key: privateKey.export({ type: 'pkcs8', format: 'pem' }),
  client_email: 'relay@hermes-test.iam.gserviceaccount.com',
  token_uri: 'https://oauth2.example/token',
};
const TOKEN = 'cXyZ_1-2:APA91b' + 'Q'.repeat(140);
const PACKAGE = 'lab.dawnforge.hermes';

// A fake Google: the token endpoint and FCM's send endpoint, answering with
// `sends` in turn (the last one repeats).
function fakeGoogle({ sends = [{ status: 200, body: { name: 'projects/hermes-test/messages/1' } }], token = {} } = {}) {
  const calls = { token: [], send: [] };
  let sent = 0;
  const fetch = async (url, init) => {
    if (url === serviceAccount.token_uri) {
      calls.token.push(Object.fromEntries(new URLSearchParams(init.body)));
      const answer = { status: 200, body: { access_token: `access-${calls.token.length}`, expires_in: 3599, token_type: 'Bearer' }, ...token };
      return new Response(JSON.stringify(answer.body), { status: answer.status });
    }
    calls.send.push({ url, authorization: init.headers.authorization, body: JSON.parse(init.body) });
    const answer = sends[Math.min(sent, sends.length - 1)];
    sent += 1;
    return new Response(JSON.stringify(answer.body ?? {}), { status: answer.status });
  };
  return { fetch, calls };
}

function fcmError(status, code, { errorCode, field } = {}) {
  return {
    status,
    body: {
      error: {
        code: status,
        message: 'refused',
        status: code,
        details: [
          ...(errorCode ? [{ '@type': 'type.googleapis.com/google.firebase.fcm.v1.FcmError', errorCode }] : []),
          ...(field ? [{ '@type': 'type.googleapis.com/google.rpc.BadRequest', fieldViolations: [{ field, description: 'Invalid registration token' }] }] : []),
        ],
      },
    },
  };
}

test('an FCM token is URL-safe text, kept as written', () => {
  assert.equal(validateFcmToken(TOKEN), TOKEN);
  assert.equal(validateFcmToken('cAbcDefGhijKlmnOpqrStu'), 'cAbcDefGhijKlmnOpqrStu', 'an installation id is accepted too');
  for (const bad of [undefined, '', 'short', 'has space ' + 'a'.repeat(40), 'a'.repeat(513), '<' + 'a'.repeat(40) + '>']) {
    assert.throws(() => validateFcmToken(bad), (error) => error.status === 400 && error.message === 'invalid_device_token');
  }
});

test('both FCM settings are required; their test seams need the test switch', () => {
  assert.throws(() => fcmConfigFromEnv({}), /FCM_SERVICE_ACCOUNT_PATH is required/);
  assert.throws(() => fcmConfigFromEnv({ FCM_PACKAGES: PACKAGE }), /FCM_SERVICE_ACCOUNT_PATH is required/);
  assert.throws(() => fcmConfigFromEnv({ FCM_SERVICE_ACCOUNT_PATH: '/k.json' }), /FCM_PACKAGES is required/);
  assert.throws(() => fcmConfigFromEnv({ FCM_SERVICE_ACCOUNT_PATH: '/k.json', FCM_PACKAGES: 'not a package' }), /Android package names/);
  assert.throws(() => fcmConfigFromEnv({ FCM_SERVICE_ACCOUNT_PATH: '/k.json', FCM_PACKAGES: PACKAGE, FCM_ORIGIN: 'http://127.0.0.1:1' }), /test setting/);
  const config = fcmConfigFromEnv({ FCM_SERVICE_ACCOUNT_PATH: '/k.json', FCM_PACKAGES: `${PACKAGE}, ${PACKAGE}.debug` });
  assert.deepEqual([...config.packages], [PACKAGE, `${PACKAGE}.debug`]);
  assert.equal(config.allowInsecure, false);
  assert.equal(config.origin, undefined);
});

test('a key file that is not a service account, or names an HTTP token endpoint, is refused', () => {
  assert.throws(() => new FcmClient({ serviceAccount: { ...serviceAccount, type: 'authorized_user' } }), /not a service account/);
  assert.throws(() => new FcmClient({ serviceAccount: { ...serviceAccount, client_email: '' } }), /no client_email/);
  assert.throws(() => new FcmClient({ serviceAccount: { ...serviceAccount, token_uri: 'http://oauth2.example/token' } }), /must use HTTPS/);
});

test('a send signs a service-account JWT, exchanges it once, and posts a data message for the token', async () => {
  const google = fakeGoogle();
  const client = new FcmClient({ serviceAccount, fetch: google.fetch });
  const message = { data: { title: 'Response ready' }, android: { priority: 'HIGH' } };
  assert.deepEqual(await client.send(TOKEN, message), { ok: true, status: 200, reason: null, tokenGone: false });
  await client.send(TOKEN, message);

  assert.equal(google.calls.token.length, 1, 'the access token is cached');
  const { grant_type: grant, assertion } = google.calls.token[0];
  assert.equal(grant, 'urn:ietf:params:oauth:grant-type:jwt-bearer');
  const [header, claims, signature] = assertion.split('.');
  const verifier = createVerify('RSA-SHA256');
  verifier.update(`${header}.${claims}`);
  assert.ok(verifier.verify(publicKey, Buffer.from(signature, 'base64url')), 'signed with the service account key');
  assert.deepEqual(JSON.parse(Buffer.from(header, 'base64url')), { alg: 'RS256', typ: 'JWT', kid: 'key-1' });
  const decoded = JSON.parse(Buffer.from(claims, 'base64url'));
  assert.equal(decoded.iss, serviceAccount.client_email);
  assert.equal(decoded.aud, serviceAccount.token_uri);
  assert.equal(decoded.scope, 'https://www.googleapis.com/auth/firebase.messaging');
  assert.equal(decoded.exp - decoded.iat, 3600);

  const [sent] = google.calls.send;
  assert.equal(sent.url, 'https://fcm.googleapis.com/v1/projects/hermes-test/messages:send');
  assert.equal(sent.authorization, 'Bearer access-1');
  assert.deepEqual(sent.body, { message: { token: TOKEN, ...message } });
});

test("a Firebase installation id goes in the message's fid field, a registration token in token", async () => {
  const google = fakeGoogle();
  const client = new FcmClient({ serviceAccount, fetch: google.fetch });
  const fid = 'cAbcDefGhijKlmnOpqrStu';
  await client.send(fid, { data: {} });
  await client.send(TOKEN, { data: {} });
  assert.deepEqual(google.calls.send.map((call) => Object.keys(call.body.message)), [['fid', 'data'], ['token', 'data']]);
  assert.equal(google.calls.send[0].body.message.fid, fid);
});

test('sends that arrive together share one token exchange, and an expiring token is renewed', async () => {
  let now = 1_000_000;
  const google = fakeGoogle();
  const client = new FcmClient({ serviceAccount, fetch: google.fetch, now: () => now });
  await Promise.all([client.send(TOKEN, {}), client.send(TOKEN, {}), client.send(TOKEN, {})]);
  assert.equal(google.calls.token.length, 1);
  now += 54 * 60_000;
  await client.send(TOKEN, {});
  assert.equal(google.calls.token.length, 1, 'still fresh 54 minutes in');
  now += 2 * 60_000;
  await client.send(TOKEN, {});
  assert.equal(google.calls.token.length, 2, 'renewed five minutes before it runs out');
  assert.equal(google.calls.send.at(-1).authorization, 'Bearer access-2');
});

test('a token Google refuses as revoked is renewed and the send tried once more', async () => {
  const google = fakeGoogle({ sends: [{ status: 401, body: { error: { status: 'UNAUTHENTICATED' } } }, { status: 200, body: {} }] });
  const client = new FcmClient({ serviceAccount, fetch: google.fetch });
  assert.equal((await client.send(TOKEN, {})).ok, true);
  assert.equal(google.calls.token.length, 2);
  assert.deepEqual(google.calls.send.map((call) => call.authorization), ['Bearer access-1', 'Bearer access-2']);
});

test('a refused key exchange throws with Google\'s reason, never the assertion', async () => {
  const google = fakeGoogle({ token: { status: 400, body: { error: 'invalid_grant', error_description: 'Invalid JWT Signature.' } } });
  const client = new FcmClient({ serviceAccount, fetch: google.fetch });
  await assert.rejects(client.send(TOKEN, {}), (error) => {
    assert.match(error.message, /fcm access token refused \(400: invalid_grant\)/);
    assert.equal(error.message.includes('eyJ'), false);
    return true;
  });
  assert.equal(google.calls.send.length, 0);
});

test('only errors that mean the token is dead say so', async () => {
  const cases = [
    [fcmError(404, 'NOT_FOUND', { errorCode: 'UNREGISTERED' }), 'UNREGISTERED', true],
    [fcmError(403, 'PERMISSION_DENIED', { errorCode: 'SENDER_ID_MISMATCH' }), 'SENDER_ID_MISMATCH', true],
    [fcmError(400, 'INVALID_ARGUMENT', { errorCode: 'INVALID_ARGUMENT', field: 'message.token' }), 'INVALID_ARGUMENT', true],
    // The relay built the message wrong: the phone's token is fine.
    [fcmError(400, 'INVALID_ARGUMENT', { errorCode: 'INVALID_ARGUMENT', field: 'message.data' }), 'INVALID_ARGUMENT', false],
    [fcmError(400, 'INVALID_ARGUMENT', { errorCode: 'INVALID_ARGUMENT' }), 'INVALID_ARGUMENT', false],
    [fcmError(429, 'RESOURCE_EXHAUSTED', { errorCode: 'QUOTA_EXCEEDED' }), 'QUOTA_EXCEEDED', false],
    [fcmError(503, 'UNAVAILABLE', { errorCode: 'UNAVAILABLE' }), 'UNAVAILABLE', false],
    [fcmError(500, 'INTERNAL'), 'INTERNAL', false],
    [{ status: 502, body: null }, null, false],
  ];
  for (const [answer, reason, tokenGone] of cases) {
    const client = new FcmClient({ serviceAccount, fetch: fakeGoogle({ sends: [answer] }).fetch });
    const result = await client.send(TOKEN, {});
    assert.deepEqual(result, { ok: false, status: answer.status, reason, tokenGone }, JSON.stringify(answer.body));
  }
});

test('a transport failure throws', async () => {
  const client = new FcmClient({
    serviceAccount,
    fetch: async (url) => {
      if (url === serviceAccount.token_uri) return new Response(JSON.stringify({ access_token: 'a', expires_in: 3600 }));
      throw new TypeError('fetch failed');
    },
  });
  await assert.rejects(client.send(TOKEN, {}), /fetch failed/);
});

test('an encrypted event reaches the phone as generic copy and the sealed envelope, nothing readable', () => {
  const e2e = { v: 1, kid: 'a'.repeat(32), msg: 'response:12345678', iat: 1_700_000_000, tok: '0123456789abcdef', z: 0, req: '', n: 'AAAAAAAAAAAAAAAA', ct: 'B'.repeat(300) };
  const event = { eventId: 'response:12345678', type: 'response.ready', e2e };
  const gateway = { id: 'gw-1', dashboardId: '3f1c3c1e-5b2a-4c1e-9f1a-0a1b2c3d4e5f' };
  const message = fcmMessage(notificationFor(event, { show_previews: true, completion_sound: true }, gateway), PACKAGE);
  assert.deepEqual(Object.keys(message.data).sort(), ['body', 'sound', 'talaria', 'talaria_e2e', 'thread', 'title']);
  assert.equal(message.data.title, 'Response ready');
  assert.equal(message.data.body, 'Hermes has finished responding.');
  assert.deepEqual(JSON.parse(message.data.talaria_e2e), e2e);
  assert.deepEqual(JSON.parse(message.data.talaria), { type: 'response.ready', e2e: 1, gateway_id: 'gw-1', dashboard_id: gateway.dashboardId });
  assert.equal(message.data.sound, '1');
  assert.ok(Object.values(message.data).every((value) => typeof value === 'string'), 'FCM data values are strings');
  assert.deepEqual(message.android, { priority: 'HIGH', restrictedPackageName: PACKAGE });
  assert.equal('notification' in message, false, 'data-only: the app shows it');
});

test('a plaintext approval keeps its card in the routing, and previews off keep the text generic', () => {
  const event = {
    eventId: 'approval:12345678',
    type: 'approval.needed',
    sessionId: 'sess-1',
    profile: 'default',
    title: 'Run rm?',
    body: 'Hermes wants to delete build/',
    decision: { kind: 'approval', session_key: 'sess-1', description: 'Run a dangerous shell command', choices: ['once', 'deny'] },
  };
  const message = fcmMessage(notificationFor(event, { show_previews: false, completion_sound: false }), PACKAGE);
  assert.equal(message.data.title, 'Approval needed');
  assert.equal(message.data.body.includes('delete build'), false);
  assert.equal(message.data.sound, '1', 'waiting on the user chimes by default');
  const routing = JSON.parse(message.data.talaria);
  assert.equal(routing.decision.session_key, 'sess-1');
  assert.equal(routing.session_id, 'sess-1');
  assert.equal(message.data.thread, 'sess-1');
});

test('a background change waits for the phone and says it is silent; a token challenge is waited for, and expires with the challenge', () => {
  const background = fcmMessage(phoneBackgroundPushFor({ type: 'phone.changes', profile: 'default' }, { id: 'gw-1' }, { nowSeconds: 100 }), PACKAGE, 100);
  assert.equal(background.android.priority, 'NORMAL');
  assert.equal(background.android.ttl, `${24 * 60 * 60}s`);
  assert.equal(background.data.title, undefined, 'nothing to show');
  assert.deepEqual(JSON.parse(background.data.talaria), { type: 'phone.changes', mode: 'background', gateway_id: 'gw-1', profile: 'default' });

  const challenge = fcmMessage(tokenChallengePush('n'.repeat(43), { ttlSeconds: 600, nowSeconds: 100 }), PACKAGE, 100);
  assert.deepEqual(challenge, {
    data: { talaria_token_challenge: 'n'.repeat(43) },
    android: { priority: 'HIGH', restrictedPackageName: PACKAGE, ttl: '600s' },
  });
});

test('a sealed event over FCM\'s data cap drops the envelope, never the push', () => {
  const e2e = { v: 1, kid: 'a'.repeat(32), msg: 'input:12345678', iat: 1_700_000_000, tok: '0123456789abcdef', z: 0, req: 'r'.repeat(12), n: 'AAAAAAAAAAAAAAAA', ct: 'B'.repeat(FCM_MAX_DATA_BYTES) };
  const { data } = notificationFor({ eventId: 'input:12345678', type: 'input.needed', e2e }, { show_previews: false }, { id: 'gw-1' });
  assert.equal(data.talaria_e2e, undefined);
  assert.equal(data.title, 'Input needed');
  assert.deepEqual(JSON.parse(data.talaria), { type: 'input.needed', e2e: 1, gateway_id: 'gw-1' });
  assert.ok(dataBytes(data) <= FCM_MAX_DATA_BYTES);
});

test('an expired push is sent with no time left rather than a negative one', () => {
  assert.equal(fcmMessage({ data: {}, expiration: 50 }, PACKAGE, 100).android.ttl, '0s');
  assert.equal('ttl' in fcmMessage({ data: {} }, PACKAGE, 100).android, false);
});
