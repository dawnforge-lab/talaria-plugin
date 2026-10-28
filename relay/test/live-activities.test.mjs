import { strict as assert } from 'node:assert';
import { test } from 'node:test';

import {
  LIVE_TURN_CATEGORY, LiveActivities, STALE_AFTER_S, liveActivityPush, validateRegistration, validateReport,
} from '../src/live-activities.mjs';

const TOKEN_A = '3f2b1c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5a';
const TOKEN_B = '3f2b1c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5b';
const TOKEN_C = '3f2b1c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5c';

function harness({ maxActivities = 100, maxPerInstallation = 4, minIntervalMs = 5_000 } = {}) {
  let clock = 1_000_000;
  const sent = [];
  const results = [];
  const activities = new LiveActivities({
    maxActivities,
    maxPerInstallation,
    minIntervalMs,
    now: () => clock,
    send: async (installationId, push) => {
      sent.push({ installationId, push });
      return results.shift() ?? { ok: true, status: 200, reason: null, tokenGone: false };
    },
  });
  return { activities, sent, results, advance: (ms) => { clock += ms; } };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

test('a report reaches the installation of the activity registered under any of the chat ids', async () => {
  const { activities, sent } = harness();
  activities.register('install-1', TOKEN_A, ['runtime-1', 'stored-1']);
  assert.equal(activities.report('install-1', 'stored-1', { phase: 'tool', tool: 'terminal', tools: 2, context: 41 }), true);
  await settle();
  assert.equal(sent.length, 1);
  assert.equal(sent[0].installationId, 'install-1');
  assert.deepEqual(sent[0].push.data, {
    category: LIVE_TURN_CATEGORY, activity: TOKEN_A, event: 'update', phase: 'tool', tools: '2', tool: 'terminal', context: '41',
    stale_at: String(1_000 + STALE_AFTER_S),
  });
  assert.equal(sent[0].push.urgent, false);
});

test('nobody watching: no push, and the host hears it', async () => {
  const { activities, sent } = harness();
  activities.register('install-1', TOKEN_A, ['session-1']);
  assert.equal(activities.report('install-1', 'session-2', { phase: 'thinking', tools: 0 }), false);
  assert.equal(activities.report('install-2', 'session-1', { phase: 'thinking', tools: 0 }), false,
    'another installation never reaches this phone');
  await settle();
  assert.equal(sent.length, 0);
});

test('updates inside the interval are merged and the latest goes out when it is up', async () => {
  const { activities, sent, advance } = harness({ minIntervalMs: 30 });
  activities.register('install-1', TOKEN_A, ['session-1']);
  activities.report('install-1', 'session-1', { phase: 'thinking', tools: 0 });
  await settle();
  activities.report('install-1', 'session-1', { phase: 'tool', tool: 'web', tools: 1 });
  activities.report('install-1', 'session-1', { phase: 'tool', tool: 'files', tools: 2 });
  await settle();
  assert.equal(sent.length, 1);
  advance(30);
  await new Promise((resolve) => setTimeout(resolve, 60));
  assert.equal(sent.length, 2);
  assert.equal(sent[1].push.data.tool, 'files');
  assert.equal(sent[1].push.data.tools, '2');
});

test('the end goes out at once, ends the activity and forgets it', async () => {
  const { activities, sent } = harness();
  activities.register('install-1', TOKEN_A, ['session-1']);
  activities.report('install-1', 'session-1', { phase: 'thinking', tools: 0 });
  await settle();
  assert.equal(activities.report('install-1', 'session-1', { phase: 'thinking', tools: 3, context: 12 }, 'done'), true);
  await settle();
  assert.equal(sent.length, 2);
  const { data, urgent } = sent[1].push;
  assert.equal(data.event, 'end');
  assert.equal(data.phase, 'done');
  assert.equal(data.tools, '3');
  assert.equal(data.ended_at, '1000');
  assert.equal(urgent, true);
  assert.equal(activities.report('install-1', 'session-1', { phase: 'thinking', tools: 0 }), false);
  assert.equal(activities.size, 0);
});

test('a token Google gave up on drops the activity; a passing refusal keeps it', async () => {
  const { activities, results } = harness();
  activities.register('install-1', TOKEN_A, ['session-1']);
  results.push({ ok: false, status: 503, reason: 'UNAVAILABLE', tokenGone: false });
  activities.report('install-1', 'session-1', { phase: 'thinking', tools: 0 });
  await settle();
  await settle();
  assert.equal(activities.size, 1);

  activities.register('install-2', TOKEN_B, ['session-2']);
  results.push({ ok: false, status: 404, reason: 'UNREGISTERED', tokenGone: true });
  activities.report('install-2', 'session-2', { phase: 'thinking', tools: 0 });
  await settle();
  await settle();
  assert.equal(activities.find('install-2', 'session-2'), undefined);
});

test('a newer activity for the chat takes it over; old registrations make room', () => {
  const { activities } = harness({ maxPerInstallation: 2 });
  activities.register('install-1', TOKEN_A, ['session-1']);
  activities.register('install-1', TOKEN_B, ['session-1']);
  assert.equal(activities.find('install-1', 'session-1').token, TOKEN_B);
  activities.register('install-1', TOKEN_C, ['session-3']);
  assert.equal(activities.size, 2, 'the oldest went past the per-phone bound');
  assert.equal(activities.find('install-1', 'session-1').token, TOKEN_B);
});

test('activities older than eight hours are swept', () => {
  const { activities, advance } = harness();
  activities.register('install-1', TOKEN_A, ['session-1']);
  advance(8 * 60 * 60 * 1000 + 1);
  assert.equal(activities.find('install-1', 'session-1'), undefined);
});

test('an update goes stale after a while; waiting on the user and the end are urgent', () => {
  const update = liveActivityPush({ phase: 'tool', tool: 'web', tools: 3, context: 40 }, undefined, TOKEN_A, { nowSeconds: 100 });
  assert.deepEqual(update, {
    data: {
      category: 'HERMES_LIVE_TURN', activity: TOKEN_A, event: 'update', phase: 'tool', tools: '3', tool: 'web', context: '40',
      stale_at: String(100 + STALE_AFTER_S),
    },
    urgent: false,
    expiration: 100 + 5 * 60,
  });
  assert.ok(Object.values(update.data).every((value) => typeof value === 'string'), 'FCM data values are strings');
  assert.equal(liveActivityPush({ phase: 'approval', tools: 1 }, undefined, TOKEN_A, { nowSeconds: 100 }).urgent, true);
  assert.equal(liveActivityPush({ phase: 'input', tools: 1 }, undefined, TOKEN_A, { nowSeconds: 100 }).urgent, true);

  const ended = liveActivityPush({ phase: 'tool', tool: 'web', tools: 3 }, 'failed', TOKEN_A, { nowSeconds: 100 });
  assert.equal(ended.data.event, 'end');
  assert.equal(ended.data.phase, 'failed');
  assert.equal(ended.data.ended_at, '100');
  assert.equal(ended.data.tool, undefined);
  assert.equal(ended.data.stale_at, undefined);
  assert.equal(ended.urgent, true);
  assert.equal(ended.expiration, 100 + 5 * 60);
});

test('reports carry only known phases, categories and bounded numbers', () => {
  assert.deepEqual(validateReport({ session_id: 's-1', state: { phase: 'tool', tool: 'browser', tools: 4, context: 99 } }),
    { sessionId: 's-1', state: { phase: 'tool', tool: 'browser', tools: 4, context: 99 }, end: undefined });
  assert.deepEqual(validateReport({ session_id: 's-1', state: { phase: 'thinking' }, end: 'stopped' }),
    { sessionId: 's-1', state: { phase: 'thinking', tools: 0 }, end: 'stopped' });
  assert.equal(validateReport({ session_id: 's-1', state: { phase: 'tool', tool: 'rm -rf /' } }), null);
  assert.equal(validateReport({ session_id: 's-1', state: { phase: 'writing a novel' } }), null);
  assert.equal(validateReport({ session_id: 's-1', state: { phase: 'thinking', context: 140 } }), null);
  assert.equal(validateReport({ session_id: 's-1', state: { phase: 'thinking' }, end: 'exploded' }), null);
  assert.equal(validateReport({ session_id: 'has spaces', state: { phase: 'thinking' } }), null);
  assert.equal(validateReport({ state: { phase: 'thinking' } }), null);
  assert.equal(validateReport({ session_id: 's-1', state: { phase: 'thinking', tools: 1.5 } }), null);
});

test('registrations need an activity id the phone made and one to six chat ids', () => {
  assert.deepEqual(validateRegistration({ push_token: TOKEN_A.toUpperCase(), session_ids: ['a', 'b'] }),
    { token: TOKEN_A, sessionIds: ['a', 'b'] });
  assert.equal(validateRegistration({ push_token: 'zz', session_ids: ['a'] }), null);
  assert.equal(validateRegistration({ push_token: 'a'.repeat(64), session_ids: ['a'] }), null, 'not a UUID');
  assert.equal(validateRegistration({ push_token: TOKEN_A, session_ids: [] }), null);
  assert.equal(validateRegistration({ push_token: TOKEN_A, session_ids: ['a', 'b c'] }), null);
  assert.equal(validateRegistration({ push_token: TOKEN_A, session_ids: Array.from({ length: 7 }, (_, i) => `s${i}`) }), null);
  assert.deepEqual(validateRegistration({ push_token: TOKEN_A, session_ids: Array(7).fill('a') }),
    { token: TOKEN_A, sessionIds: ['a'] }, 'repeats count once');
});

test('an end waits for the update in flight, so it never lands first', async () => {
  let release;
  const sent = [];
  const activities = new LiveActivities({
    maxActivities: 10,
    maxPerInstallation: 4,
    send: (installationId, push) => {
      sent.push(push.data.event);
      if (sent.length === 1) return new Promise((resolve) => { release = () => resolve({ ok: true }); });
      return { ok: true };
    },
  });
  activities.register('install-1', TOKEN_A, ['session-1']);
  activities.report('install-1', 'session-1', { phase: 'tool', tool: 'web', tools: 1 });
  await settle();
  activities.report('install-1', 'session-1', { phase: 'thinking', tools: 1 }, 'done');
  activities.report('install-1', 'session-1', { phase: 'thinking', tools: 1 });
  await settle();
  assert.deepEqual(sent, ['update']);
  release();
  await settle();
  await settle();
  assert.deepEqual(sent, ['update', 'end'], 'nothing after the end overtakes it');
  assert.equal(activities.size, 0);
});

test('an end Google did not take is tried again before the activity is dropped', async () => {
  const { activities, sent, results } = harness({ minIntervalMs: 10 });
  activities.register('install-1', TOKEN_A, ['session-1']);
  results.push({ ok: false, status: 500, reason: 'INTERNAL', tokenGone: false });
  activities.report('install-1', 'session-1', { phase: 'thinking', tools: 0 }, 'failed');
  await settle();
  assert.equal(sent.length, 1);
  assert.equal(activities.size, 1, 'kept until the end is delivered');
  await new Promise((resolve) => setTimeout(resolve, 30));
  await settle();
  assert.equal(sent.length, 2);
  assert.equal(sent[1].push.data.event, 'end');
  assert.equal(activities.size, 0);
});

test('a removed installation takes its activities with it', () => {
  const { activities } = harness();
  activities.register('install-1', TOKEN_A, ['session-1']);
  activities.register('install-2', TOKEN_B, ['session-1']);
  activities.removeInstallation('install-1');
  assert.equal(activities.find('install-1', 'session-1'), undefined);
  assert.ok(activities.find('install-2', 'session-1'));
});

test('a send that settles after its id was registered again leaves the new registration alone', async () => {
  let release;
  const sent = [];
  const activities = new LiveActivities({
    maxActivities: 10,
    maxPerInstallation: 4,
    minIntervalMs: 10,
    send: (installationId, push) => {
      sent.push(push.data.event);
      if (sent.length === 1) return new Promise((resolve) => { release = () => resolve({ ok: false, status: 500, reason: 'INTERNAL' }); });
      return { ok: true };
    },
  });
  activities.register('install-1', TOKEN_A, ['session-1']);
  activities.report('install-1', 'session-1', { phase: 'thinking', tools: 0 }, 'done');
  await settle();
  activities.register('install-1', TOKEN_A, ['session-2']);
  release();
  await settle();
  await new Promise((resolve) => setTimeout(resolve, 30));
  assert.deepEqual(sent, ['end'], 'the old end is not sent again to the new registration');
  assert.ok(activities.find('install-1', 'session-2'));
});
