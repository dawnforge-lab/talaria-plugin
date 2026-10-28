// Shared by the end-to-end tests: a fake Google (its OAuth token endpoint
// and FCM's send endpoint), real relay processes pointed at it, and the
// phone side of registering and pairing. Kept outside test/ so `node --test`
// doesn't run it as a test file.

import { strict as assert } from 'node:assert';
import { spawn } from 'node:child_process';
import { generateKeyPairSync, randomBytes } from 'node:crypto';
import { writeFileSync } from 'node:fs';
import { createServer } from 'node:http';
import { createServer as createNetServer } from 'node:net';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

export const PACKAGE = 'lab.dawnforge.hermes';
export const DEBUG_PACKAGE = 'lab.dawnforge.hermes.debug';
const PROJECT = 'talaria-test';
const privateKey = generateKeyPairSync('rsa', { modulusLength: 2048 }).privateKey.export({ type: 'pkcs8', format: 'pem' });

// A refusal from FCM, as Google words it.
export function fcmRefusal(status, code, { errorCode, field } = {}) {
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

// The token will never work again: the relay forgets it.
export const UNREGISTERED = fcmRefusal(404, 'NOT_FOUND', { errorCode: 'UNREGISTERED' });
// Google is having a moment: worth a retry.
export const UNAVAILABLE = fcmRefusal(503, 'UNAVAILABLE', { errorCode: 'UNAVAILABLE' });

// A fake Google. Every send is recorded in `sends` with its `token` (the
// message's token or fid), the `message` and how it was `answered`; `sent`
// holds the messages it accepted. It answers each send with the next of
// `answers`, else `mode`: 'accept', 'drop' (the connection dies, as a
// transport failure) or a refusal ({ status, body }). `delayMs` holds every
// send that long first.
export async function startGoogle() {
  const google = {
    url: '',
    mode: 'accept',
    answers: [],
    delayMs: 0,
    sends: [],
    tokenExchanges: 0,
    get sent() {
      return this.sends.filter((send) => send.answered === 'accept').map((send) => send.message);
    },
    // Messages accepted for one device token.
    sentTo(token) {
      return this.sends.filter((send) => send.answered === 'accept' && send.token === token).map((send) => send.message);
    },
    reset() {
      this.mode = 'accept';
      this.answers = [];
      this.delayMs = 0;
    },
    // The service account key file for a relay to use with this Google.
    keyFile(dir) {
      const path = join(dir, `service-account-${randomBytes(4).toString('hex')}.json`);
      writeFileSync(path, JSON.stringify({
        type: 'service_account',
        project_id: PROJECT,
        private_key_id: 'key-1',
        private_key: privateKey,
        client_email: `relay@${PROJECT}.iam.gserviceaccount.com`,
        token_uri: `${this.url}/token`,
      }));
      return path;
    },
  };
  const server = createServer(async (request, response) => {
    const body = await readBody(request);
    if (request.url === '/token') {
      google.tokenExchanges += 1;
      response.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify({ access_token: `access-${google.tokenExchanges}`, expires_in: 3599 }));
      return;
    }
    if (request.url === `/v1/projects/${PROJECT}/messages:send`) {
      const { message } = JSON.parse(body);
      const answer = google.answers.length ? google.answers.shift() : google.mode;
      const send = { token: message.token ?? message.fid, message, authorization: request.headers.authorization, answered: answer === 'accept' || answer === 'drop' ? answer : 'refused' };
      google.sends.push(send);
      if (google.delayMs) await new Promise((resolve) => setTimeout(resolve, google.delayMs));
      if (answer === 'drop') {
        request.socket.destroy();
        return;
      }
      if (answer === 'accept') {
        response.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify({ name: `projects/${PROJECT}/messages/${google.sends.length}` }));
        return;
      }
      response.writeHead(answer.status, { 'content-type': 'application/json' }).end(JSON.stringify(answer.body));
      return;
    }
    response.writeHead(404).end();
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  google.url = `http://127.0.0.1:${server.address().port}`;
  google.close = () => new Promise((resolve) => {
    server.closeAllConnections?.();
    server.close(resolve);
  });
  return google;
}

function readBody(request) {
  return new Promise((resolve) => {
    let body = '';
    request.setEncoding('utf8');
    request.on('data', (chunk) => { body += chunk; });
    request.on('end', () => resolve(body));
  });
}

// A loopback port nothing listens on (until someone takes it).
export function freePort() {
  return new Promise((resolve, reject) => {
    const probe = createNetServer();
    probe.listen(0, '127.0.0.1', () => {
      const { port } = probe.address();
      probe.close(() => resolve(port));
    });
    probe.on('error', reject);
  });
}

// The settings a relay needs to push through `google` (or, with
// `googleUrl`, through whatever answers there).
export function fcmEnv(dir, google, { googleUrl = google.url, packages = [PACKAGE, DEBUG_PACKAGE] } = {}) {
  return { FCM_SERVICE_ACCOUNT_PATH: google.keyFile(dir), FCM_PACKAGES: packages.join(','), FCM_ORIGIN: googleUrl };
}

// A relay process. Its JSON log lines are collected in `relay.logs` with
// `logs`. Resolves once /healthz answers; rejects if it exits first.
export async function startRelay(env, { logs = false, port } = {}) {
  port ??= await freePort();
  const base = `http://127.0.0.1:${port}`;
  const child = spawn(process.execPath, ['src/server.mjs'], {
    // fileURLToPath: URL.pathname yields "/C:/…" on Windows, which spawn
    // rejects; the file-path form works on every platform.
    cwd: fileURLToPath(new URL('..', import.meta.url)),
    env: {
      ...process.env,
      HOST: '127.0.0.1',
      PORT: String(port),
      PUBLIC_URL: `https://relay-${port}.example`,
      RELAY_TEST_SEAMS: '1',
      // api() names its own address in X-Forwarded-For.
      TRUST_PROXY: '1',
      ...env,
    },
    stdio: ['ignore', logs ? 'pipe' : 'ignore', 'pipe'],
  });
  const relay = { child, base, port, logs: [], stderr: '' };
  if (logs) {
    let pending = '';
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', (chunk) => {
      pending += chunk;
      const lines = pending.split('\n');
      pending = lines.pop();
      for (const line of lines) {
        try { relay.logs.push(JSON.parse(line)); } catch { /* Not a log line. */ }
      }
    });
  }
  child.stderr.setEncoding('utf8');
  child.stderr.on('data', (chunk) => {
    relay.stderr += chunk;
    if (logs) {
      for (const line of chunk.split('\n').filter(Boolean)) {
        try { relay.logs.push(JSON.parse(line)); } catch { /* Not a log line. */ }
      }
    }
  });
  const deadline = Date.now() + 10_000;
  for (;;) {
    if (child.exitCode !== null) throw new Error(`relay exited with ${child.exitCode}: ${relay.stderr}`);
    try {
      if ((await fetch(`${base}/healthz`)).ok) return relay;
    } catch { /* Not listening yet. */ }
    if (Date.now() > deadline) throw new Error(`relay did not start: ${relay.stderr}`);
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
}

// Runs a relay that should refuse to start, and resolves with its stderr.
export function relayBootFailure(env) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, ['src/server.mjs'], {
      cwd: fileURLToPath(new URL('..', import.meta.url)),
      env: { ...process.env, HOST: '127.0.0.1', PORT: '1', PUBLIC_URL: 'https://relay.example', RELAY_TEST_SEAMS: '1', ...env },
      stdio: ['ignore', 'ignore', 'pipe'],
    });
    let stderr = '';
    child.stderr.setEncoding('utf8');
    child.stderr.on('data', (chunk) => { stderr += chunk; });
    const timer = setTimeout(() => { child.kill('SIGKILL'); reject(new Error('relay started')); }, 10_000);
    child.on('exit', (code) => {
      clearTimeout(timer);
      if (code === 0) reject(new Error('relay exited cleanly'));
      else resolve(stderr);
    });
  });
}

export async function stopRelay(relay) {
  const { child } = relay.child ? relay : { child: relay };
  if (child.exitCode !== null || child.signalCode !== null) return;
  const exited = (timeoutMs) => new Promise((resolve) => {
    if (child.exitCode !== null || child.signalCode !== null) { resolve(true); return; }
    const onExit = () => { clearTimeout(timer); resolve(true); };
    const timer = setTimeout(() => { child.off('exit', onExit); resolve(false); }, timeoutMs);
    timer.unref?.();
    child.once('exit', onExit);
  });
  child.kill('SIGTERM');
  if (await exited(5_000)) return;
  child.kill('SIGKILL');
  if (!await exited(2_000)) throw new Error('relay did not stop after SIGKILL');
}

// A fresh, valid FCM registration token.
export function fcmToken() {
  return `${randomBytes(8).toString('hex')}:APA91b${randomBytes(96).toString('base64url')}`;
}

let address = 0;

// A JSON call to the relay. Each call comes from its own address unless
// `from` says otherwise, so the per-address limits stay out of the way of
// tests that aren't about them (the relay must trust one proxy hop).
export async function api(base, path, { method = 'GET', body, credential, headers = {}, from } = {}) {
  address += 1;
  const response = await fetch(`${base}${path}`, {
    method,
    headers: {
      'x-forwarded-for': from ?? `10.${(address >> 16) & 255}.${(address >> 8) & 255}.${address & 255}`,
      ...(body !== undefined ? { 'content-type': 'application/json' } : {}),
      ...(credential ? { authorization: `Bearer ${credential}` } : {}),
      ...headers,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return { status: response.status, json: await response.json().catch(() => null) };
}

export const registration = (deviceToken, extra = {}) => ({ platform: 'fcm', bundle_id: PACKAGE, device_token: deviceToken, ...extra });

// Asks for a challenge and returns the nonce Google was asked to deliver.
export async function challenge(base, google, deviceToken, extra = {}) {
  const before = google.sends.length;
  const asked = await api(base, '/v1/token-challenges', { method: 'POST', body: registration(deviceToken, extra) });
  assert.equal(asked.status, 202, JSON.stringify(asked.json));
  const pushes = google.sends.slice(before).filter((send) => send.token === deviceToken);
  assert.equal(pushes.length, 1);
  return pushes[0].message.data.talaria_token_challenge;
}

// A phone registered with its proof: `{ installation, credential, token }`.
export async function registerPhone(base, google, { token = fcmToken(), preferences } = {}) {
  const proof = await challenge(base, google, token);
  const registered = await api(base, '/v1/installations', {
    method: 'POST',
    body: registration(token, { token_proof: proof, ...(preferences ? { preferences } : {}) }),
  });
  assert.equal(registered.status, 201, JSON.stringify(registered.json));
  return { ...registered.json, token };
}

// Pairs a registered phone with a host: `{ gateway, gatewayId }`, where
// `gateway` is the host's credential.
export async function pairHost(base, phone, { dashboardId, gatewayName = 'hermes-pc' } = {}) {
  const pairing = await api(base, `/v1/installations/${phone.installation.id}/pairings`, {
    method: 'POST',
    credential: phone.credential,
    body: dashboardId ? { dashboard_id: dashboardId } : {},
  });
  assert.equal(pairing.status, 201, JSON.stringify(pairing.json));
  const claimed = await api(base, '/v1/pairings/claim', { method: 'POST', body: { pairing_code: pairing.json.pairing_code, gateway_name: gatewayName } });
  assert.equal(claimed.status, 200, JSON.stringify(claimed.json));
  return { gateway: claimed.json.credential, gatewayId: claimed.json.gateway_id };
}

// A phone registered and paired with one host.
export async function pairedPhone(base, google, options = {}) {
  const phone = await registerPhone(base, google, options);
  return { ...phone, ...await pairHost(base, phone, options) };
}

// A push's routing and envelope, parsed from its data.
export function routingOf(message) {
  return JSON.parse(message.data.talaria);
}

export function envelopeOf(message) {
  return message.data.talaria_e2e === undefined ? undefined : JSON.parse(message.data.talaria_e2e);
}

export async function waitFor(check, message, timeoutMs = 15_000) {
  const deadline = Date.now() + timeoutMs;
  let lastError;
  for (;;) {
    try {
      if (await check()) return;
    } catch (error) {
      lastError = error;
    }
    if (Date.now() > deadline) {
      const detail = lastError === undefined ? '' : ` (last error: ${String(lastError?.message ?? lastError).replace(/\s+/g, ' ').slice(0, 300)})`;
      throw new Error(`${message}${detail}`);
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
}
