import { strict as assert } from 'node:assert';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { after, before, test } from 'node:test';

import { PACKAGE, api, fcmEnv, pairedPhone, startGoogle, startRelay, stopRelay, waitFor } from '../testing/harness.mjs';

// Live turn progress through a real relay process: the phone registers an
// activity id it made, the host reports progress, and Google is asked to
// deliver it to the phone's own token as data naming that activity.

const dir = mkdtempSync(join(tmpdir(), 'talaria-relay-live-'));
const ACTIVITY = '3f2b1c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d';
let google;
let relay;
let base;
let phone;

before(async () => {
  google = await startGoogle();
  relay = await startRelay({ DATA_PATH: join(dir, 'relay-data.json'), ...fcmEnv(dir, google) });
  base = relay.base;
  phone = await pairedPhone(base, google);
});

after(async () => {
  await stopRelay(relay);
  await google.close();
  rmSync(dir, { recursive: true, force: true });
});

const report = (body) => api(base, '/v1/live-activity', { method: 'POST', credential: phone.gateway, body });
const follow = (sessionIds, activity = ACTIVITY) => api(base, `/v1/installations/${phone.installation.id}/live-activities`, {
  method: 'PUT', credential: phone.credential, body: { push_token: activity, session_ids: sessionIds },
});

test('a watched turn reaches the phone as live turn data, and its end ends it', async () => {
  assert.deepEqual((await report({ session_id: 'stored-1', state: { phase: 'thinking' } })).json, { tracked: false },
    'nothing registered yet');
  const before = google.sentTo(phone.token).length;

  const registered = await follow(['runtime-1', 'stored-1']);
  assert.equal(registered.status, 200);
  assert.deepEqual(registered.json, { registered: true });

  assert.deepEqual((await report({ session_id: 'stored-1', state: { phase: 'tool', tool: 'terminal', tools: 1, context: 20 } })).json,
    { tracked: true });
  await waitFor(() => google.sentTo(phone.token).length === before + 1, 'the update was not sent');
  const update = google.sentTo(phone.token).at(-1);
  assert.equal(update.data.category, 'HERMES_LIVE_TURN');
  assert.equal(update.data.activity, ACTIVITY);
  assert.equal(update.data.event, 'update');
  assert.equal(update.data.phase, 'tool');
  assert.equal(update.data.tool, 'terminal');
  assert.equal(update.data.tools, '1');
  assert.equal(update.data.context, '20');
  assert.equal(update.android.priority, 'NORMAL');
  assert.equal(update.android.restrictedPackageName, PACKAGE);

  assert.deepEqual((await report({ session_id: 'stored-1', state: { phase: 'thinking', tools: 1 }, end: 'done' })).json,
    { tracked: true });
  await waitFor(() => google.sentTo(phone.token).length === before + 2, 'the end was not sent');
  const end = google.sentTo(phone.token).at(-1);
  assert.equal(end.data.event, 'end');
  assert.equal(end.data.phase, 'done');
  assert.equal(end.android.priority, 'HIGH');
  assert.deepEqual((await report({ session_id: 'stored-1', state: { phase: 'thinking' } })).json, { tracked: false },
    'the ended activity is forgotten');
});

test('a phone that is no longer registered stops being followed', async () => {
  const other = await pairedPhone(base, google);
  await api(base, `/v1/installations/${other.installation.id}/live-activities`, {
    method: 'PUT', credential: other.credential, body: { push_token: ACTIVITY, session_ids: ['stored-2'] },
  });
  const removed = await api(base, `/v1/installations/${other.installation.id}`, { method: 'DELETE', credential: other.credential });
  assert.equal(removed.status, 204);
  const answer = await api(base, '/v1/live-activity', { method: 'POST', credential: other.gateway, body: { session_id: 'stored-2', state: { phase: 'thinking' } } });
  assert.equal(answer.status, 401, 'its host is signed out with it');
});

test('malformed or unauthenticated requests are refused', async () => {
  assert.equal((await api(base, '/v1/live-activity', { method: 'POST', body: { session_id: 's', state: { phase: 'thinking' } } })).status, 401);
  assert.equal((await api(base, '/v1/live-activity', { method: 'POST', credential: phone.credential, body: { session_id: 's', state: { phase: 'thinking' } } })).status, 401,
    'a phone credential is not a host credential');
  assert.equal((await report({ session_id: 's', state: { phase: 'tool', tool: 'secret-tool-name' } })).status, 400);
  assert.equal((await api(base, `/v1/installations/${phone.installation.id}/live-activities`, { method: 'PUT', body: { push_token: ACTIVITY, session_ids: ['s'] } })).status, 401);
  assert.equal((await follow(['s'], 'nope')).status, 400);
});
