import { strict as assert } from 'node:assert';
import { test } from 'node:test';

import { FCM_MAX_DATA_BYTES, dataBytes } from '../src/fcm.mjs';
import { SESSION_KEY_MAX_BYTES, notificationFor, phoneBackgroundPushFor, validateEvent, validateDecision } from '../src/server.mjs';
import { normalizePreferences } from '../src/store.mjs';

const preferences = { show_previews: true, completion_sound: false };

// The routing the app reads from a push's data.
const routing = (data) => JSON.parse(data.talaria);

// How many times `text` appears in the push's data, as sent.
const occurrences = (data, text) => JSON.stringify(data).split(text).length - 1;

const clarifyDecision = {
  kind: 'clarify',
  request_id: 'talaria-push-abc123',
  question: 'Which color?',
  choices: ['Red', 'Blue'],
};

const approvalDecision = {
  kind: 'approval',
  session_key: 'sess-1',
  description: 'Run a dangerous shell command',
  choices: ['once', 'deny'],
};

test('notificationFor embeds the decision once, in the routing beside the alert', () => {
  const event = {
    eventId: 'approval:12345678',
    type: 'approval.needed',
    sessionId: 'sess-1',
    profile: 'default',
    decision: approvalDecision,
  };
  const { data, urgent } = notificationFor(event, preferences);
  assert.equal(urgent, true);
  // The routing is the one rich copy the app reads.
  const { decision, ...rest } = routing(data);
  assert.equal(decision.kind, 'approval');
  assert.equal(decision.session_key, 'sess-1');
  assert.equal(decision.description, 'Run a dangerous shell command');
  assert.deepEqual(decision.choices, ['once', 'deny']);
  assert.deepEqual(rest, { type: 'approval.needed', session_id: 'sess-1', profile: 'default' });
  // Serialized once: a second copy of the structured decision would double
  // its byte cost against the FCM size guard.
  assert.deepEqual(Object.keys(data).sort(), ['body', 'sound', 'talaria', 'thread', 'title']);
  assert.equal(occurrences(data, 'Run a dangerous shell command'), 1);
});

test('notificationFor omits decision when the event has none', () => {
  const event = { eventId: 'response:12345678', type: 'response.ready', sessionId: 'sess-1', profile: 'default' };
  const { data } = notificationFor(event, preferences);
  assert.equal(routing(data).decision, undefined);
});

test('notificationFor drops a malformed decision so the notification degrades to a routing stub', () => {
  const event = {
    eventId: 'approval:12345678',
    type: 'approval.needed',
    sessionId: 'sess-1',
    profile: 'default',
    decision: { kind: 'approval', description: 'no session key' }, // not answerable
  };
  const { data } = notificationFor(event, preferences);
  assert.equal(routing(data).decision, undefined);
});

test('notificationFor gates decision on the dedicated decision_cards preference', () => {
  const event = {
    eventId: 'approval:12345678',
    type: 'approval.needed',
    sessionId: 'sess-1',
    profile: 'default',
    decision: approvalDecision,
  };
  // Decision cards are independent of show_previews: previews-off users who
  // left decision_cards on still get answerable cards...
  const previewsOff = notificationFor(event, { show_previews: false, completion_sound: false });
  assert.ok(routing(previewsOff.data).decision, 'previews-off must not disable decision cards');
  assert.equal(previewsOff.data.body.includes('Run a dangerous'), false, 'banner text stays generic');
  // ...and turning just decision_cards off keeps the push content-free.
  const cardsOff = notificationFor(event, { show_previews: true, decision_cards: false, completion_sound: false });
  assert.equal(routing(cardsOff.data).decision, undefined);
  assert.equal(JSON.stringify(cardsOff.data).includes('Run a dangerous'), false);
  // Legacy installations whose stored preferences predate the key default on.
  const legacy = notificationFor(event, { show_previews: false, completion_sound: false });
  assert.ok(routing(legacy.data).decision !== undefined);
});

test('notificationFor degrades to a routing stub when the data would exceed the FCM cap', () => {
  const sessionKey = 'k'.repeat(SESSION_KEY_MAX_BYTES);
  const event = {
    eventId: 'approval:12345678',
    type: 'approval.needed',
    sessionId: sessionKey,
    profile: 'default',
    title: 'Approval needed',
    // 500 three-byte chars in the alert body and in the description, plus a
    // session key at its byte cap (in the routing and again in the decision),
    // push the data past FCM's 4096 bytes.
    body: '€'.repeat(500),
    decision: { ...approvalDecision, session_key: sessionKey, description: '€'.repeat(500) },
  };
  const { data } = notificationFor(event, preferences);
  assert.equal(routing(data).decision, undefined, 'oversized decision must be dropped');
  assert.equal(routing(data).session_id, sessionKey, 'routing stub must survive');
  assert.ok(dataBytes(data) <= FCM_MAX_DATA_BYTES);
});

test('validateDecision only accepts approval decisions on approval events', () => {
  assert.equal(validateDecision(approvalDecision, 'approval.needed').kind, 'approval');
  // Clarify is its own contract, bound to input.needed with a plugin-minted
  // request id; it may not ride an approval event.
  assert.equal(validateDecision(clarifyDecision, 'approval.needed'), undefined);
  // Kind and event type must agree.
  assert.equal(validateDecision(approvalDecision, 'input.needed'), undefined);
  assert.equal(validateDecision(approvalDecision, 'response.ready'), undefined);
  assert.equal(validateDecision({ kind: 'sudo', session_key: 's', description: 'd', choices: ['once'] }, 'approval.needed'), undefined);
});

test('validateDecision accepts the clarify contract on input events', () => {
  assert.deepEqual(
    validateDecision(clarifyDecision, 'input.needed'),
    { kind: 'clarify', request_id: 'talaria-push-abc123', question: 'Which color?', choices: ['Red', 'Blue'] },
  );
  // Answerability (request id) and display text (question) are both required.
  assert.equal(validateDecision({ kind: 'clarify', question: 'Which color?' }, 'input.needed'), undefined);
  assert.equal(validateDecision({ kind: 'clarify', request_id: 'talaria-push-abc123' }, 'input.needed'), undefined);
  // Open-ended clarifies (no choices) are valid.
  assert.deepEqual(
    validateDecision({ kind: 'clarify', request_id: 'talaria-push-abc123', question: 'What next?' }, 'input.needed'),
    { kind: 'clarify', request_id: 'talaria-push-abc123', question: 'What next?' },
  );
});

test('clarify request IDs match the poll/respond route grammar without truncation', () => {
  const valid = { kind: 'clarify', request_id: 'Abc_123-x', question: 'What next?' };
  assert.equal(validateDecision(valid, 'input.needed').request_id, valid.request_id);
  for (const requestId of ['abc', 'abc/def', 'abc def', 'a'.repeat(129), 'é'.repeat(4)]) {
    assert.equal(validateDecision({ ...valid, request_id: requestId }, 'input.needed'), undefined, requestId);
  }
});

test('validateDecision whitelists choices to the approval vocabulary', () => {
  const injected = validateDecision(
    { ...approvalDecision, choices: ['once', 'deny', 'not-a-real-choice', 'free text'] },
    'approval.needed',
  );
  assert.deepEqual(injected.choices, ['once', 'deny']);
  // All-invalid choices degrade the whole decision to a routing stub.
  assert.equal(
    validateDecision({ ...approvalDecision, choices: ['bogus'] }, 'approval.needed'),
    undefined,
  );
  // Missing or empty choices likewise.
  assert.equal(validateDecision({ kind: 'approval', session_key: 's', description: 'd' }, 'approval.needed'), undefined);
});

test('validateDecision rejects missing display text or session key', () => {
  assert.equal(validateDecision({ kind: 'approval', session_key: 's', choices: ['once'] }, 'approval.needed'), undefined);
  assert.equal(validateDecision({ kind: 'approval', description: 'd', choices: ['once'] }, 'approval.needed'), undefined);
  assert.equal(validateDecision(undefined, 'approval.needed'), undefined);
  // Unknown fields are never echoed into the push.
  const clean = validateDecision({ ...approvalDecision, command: 'secret' }, 'approval.needed');
  assert.equal(clean.command, undefined);
});

test('validateEvent passes a bounded decision through', () => {
  const event = validateEvent({
    type: 'approval.needed',
    event_id: 'approval:12345678',
    session_id: 'sess-1',
    decision: { kind: 'approval', session_key: 'sess-1', description: 'd'.repeat(900), choices: ['once', 'deny', 'x'.repeat(200)] },
  });
  assert.equal(event.decision.kind, 'approval');
  assert.equal(event.decision.session_key, 'sess-1');
  assert.equal(event.decision.description.length, 500);
  assert.deepEqual(event.decision.choices, ['once', 'deny'], 'unknown choice strings are filtered');
});

// Gateway session keys built by Hermes' build_session_key carry whatever
// the platform's ids hold: Matrix rooms and users (`!room:server`,
// `@user:server`), WhatsApp group JIDs (`…@g.us`), a profile named `main`
// (`agent:main~`). They travel whole; only a key with a control character or
// over SESSION_KEY_MAX_BYTES is dropped, never cut.
const gatewaySessionKeys = [
  'agent:main:matrix:group:!AbCdEf:matrix.org:@bob:matrix.org',
  'agent:main:whatsapp:group:120363012345678901@g.us:15551234567',
  'agent:main~:telegram:dm:12345',
  'agent:main:signal:dm:+15551234567',
  `agent:main:api:dm:${'k'.repeat(400)}`,
  'agent:main:matrix:dm:@zoë:matrix.org',
];

test('a plaintext approval keeps its gateway session key whole in the push', () => {
  for (const key of gatewaySessionKeys) {
    const event = validateEvent({
      type: 'approval.needed',
      event_id: 'approval:12345678',
      session_id: key,
      decision: { kind: 'approval', session_key: key, description: 'Run rm -rf build?', choices: ['once', 'deny'] },
    });
    const { data } = notificationFor(event, preferences, { id: 'gw-1' });
    assert.equal(routing(data).session_id, key, key);
    assert.equal(routing(data).decision?.session_key, key, key);
  }
});

test('a session key over the byte limit or with a control character is dropped, never cut', () => {
  for (const key of ['k'.repeat(SESSION_KEY_MAX_BYTES + 1), 'é'.repeat(SESSION_KEY_MAX_BYTES / 2 + 1), 'agent:main\n:x', 'a\u0000b', 'a\u007fb', '\ud800', '']) {
    const event = validateEvent({
      type: 'approval.needed',
      event_id: 'approval:12345678',
      session_id: key,
      decision: { kind: 'approval', session_key: key, description: 'Run rm -rf build?', choices: ['once', 'deny'] },
    });
    assert.equal(event.sessionId, undefined, JSON.stringify(key));
    assert.equal(event.decision, undefined, JSON.stringify(key));
  }
  const atLimit = 'k'.repeat(SESSION_KEY_MAX_BYTES);
  assert.equal(validateEvent({ type: 'response.ready', event_id: 'response:12345678', session_id: atLimit }).sessionId, atLimit);
});

test('notificationFor embeds every batch question in the routing', () => {
  const event = {
    type: 'input.needed',
    sessionId: 'sess-1',
    profile: 'default',
    title: 'Input needed',
    body: 'Which environment?',
    decision: {
      kind: 'clarify',
      request_id: 'talaria-push-batch-e2e',
      question: 'Which environment?',
      choices: ['staging', 'prod'],
      questions: [
        { qid: 'q0', question: 'Which environment?', choices: ['staging', 'prod'], multi_select: false },
        { qid: 'q1', question: 'Which tests?', choices: ['unit', 'ui'], multi_select: true },
      ],
    },
  };
  const preferences = { enabled: true, show_previews: true, decision_cards: true };
  const { data } = notificationFor(event, preferences);
  // The routing — exactly what FCM delivers to the phone — carries the FULL
  // batch, once.
  const { decision } = routing(data);
  assert.equal(decision.questions.length, 2);
  assert.deepEqual(decision.questions.map((question) => question.qid), ['q0', 'q1']);
  assert.equal(decision.questions[1].multi_select, true);
  assert.equal(occurrences(data, 'Which tests?'), 1);
});

test('notificationFor strips an oversized batch decision so the card cannot exceed FCM limits', () => {
  // A VALID protocol-max batch (8x8, maximal text): the answerable-card
  // capacity is bounded by the FCM data budget, not by the 8x8 protocol
  // ceiling. The degradation is ALL-OR-NOTHING — the serialized push must
  // never carry a partial questions[] (Hermes is still waiting on every
  // qid, so a truncated card would collect answers for a batch that can
  // never complete) — and the plain banner still ships.
  const event = {
    type: 'input.needed',
    sessionId: 'sess-1',
    profile: 'default',
    decision: {
      kind: 'clarify',
      request_id: 'talaria-push-huge',
      question: 'Huge?',
      questions: Array.from({ length: 8 }, (_, i) => ({
        qid: `q${i}`,
        question: 'x'.repeat(500),
        choices: Array.from({ length: 8 }, (_, j) => 'y'.repeat(80)),
        multi_select: false,
      })),
    },
  };
  const preferences = { enabled: true, show_previews: true, decision_cards: true };
  const { data } = notificationFor(event, preferences);
  // No decision in the routing, and no partial questions anywhere.
  assert.equal(routing(data).decision, undefined, 'the size guard must strip the decision');
  assert.equal(JSON.stringify(data).includes('questions'), false, 'no partial question list may survive anywhere in the push');
  assert.equal(JSON.stringify(data).includes('talaria-push-huge'), false, 'no answerable card fragments may survive');
  // The plain input.needed banner still ships alongside the stripped card.
  assert.equal(data.title, 'Input needed');
  assert.equal(typeof data.body, 'string');
  // The stripped data must fit under the same cap that triggered the strip.
  assert.ok(dataBytes(data) <= FCM_MAX_DATA_BYTES, 'stripped data must fit under the FCM cap');
});

test('validateDecision preserves a sanitized batch and deduplicates identities', () => {
  const decision = validateDecision({
    kind: 'clarify',
    request_id: 'talaria-push-dedupe',
    question: 'summary',
    questions: [
      { qid: 'q0', question: 'First', choices: ['a', 'a', 'b'], multi_select: false },
      { qid: 'q0', question: 'Duplicate qid dropped' },
      { qid: '__proto__', question: 'Prototype qid dropped' },
      { qid: 'q1', question: 'Second', choices: [], multi_select: 'yes' },
    ],
  }, 'input.needed');
  assert.deepEqual(decision.questions.map((question) => question.qid), ['q0', 'q1']);
  assert.deepEqual(decision.questions[0].choices, ['a', 'b'], 'duplicate choice values collapse');
  assert.equal(decision.questions[1].multi_select, false, 'non-boolean multi_select never coerces to true');
});

test('a representative multi-question batch fits the budget with a single structured copy', () => {
  // 3 questions x ~200-char text x 4 choices: comfortably deliverable
  // because the decision is serialized once, and it carries every qid.
  const event = {
    eventId: 'input:midsize0001',
    type: 'input.needed',
    sessionId: 'sess-midsize',
    profile: 'default',
    decision: {
      kind: 'clarify',
      request_id: 'talaria-push-midsize',
      question: 'Implementation plan?',
      questions: [0, 1, 2].map((index) => ({
        qid: `q${index}`,
        question: `Describe step ${index} of the rollout, including which services are affected, the expected downtime, and how a rollback would be performed if the step fails validation.` + ' Detail '.repeat(index + 1),
        choices: [
          `Proceed with step ${index} during the maintenance window`,
          `Delay step ${index} until the follow-up release ships`,
          `Ask the platform team to review step ${index} first`,
          `Skip step ${index} entirely for this iteration`,
        ],
        multi_select: index === 2,
      })),
    },
  };
  const preferences = { enabled: true, show_previews: true, decision_cards: true };
  const { data } = notificationFor(event, preferences);
  const size = dataBytes(data);
  assert.equal(
    routing(data).decision.questions.length,
    3,
    'the full batch must reach the phone',
  );
  assert.ok(
    size <= FCM_MAX_DATA_BYTES,
    `a representative 3-question batch must fit the guard: ${size} bytes`,
  );
});

// ── Dashboard identity in outgoing pushes (#148) ─────────────────────────
// The outgoing dashboard_id comes from the AUTHENTICATED gateway record
// (bound at pairing/claim time) — never from the event body, which plugins
// must not be able to author.

test('notificationFor stamps dashboard_id from the authenticated gateway into the routing', () => {
  const event = { eventId: 'response:12345678', type: 'response.ready', sessionId: 'sess-1', profile: 'default' };
  const gateway = { id: 'gw-1', name: 'Mac gateway', dashboardId: '0f5c8a34-1b2d-4e5f-8a9b-0c1d2e3f4a5b' };
  const { data } = notificationFor(event, preferences, gateway);
  assert.equal(routing(data).dashboard_id, gateway.dashboardId);
});

test('notificationFor omits dashboard_id for pre-dashboard gateways and absent gateway', () => {
  const event = { eventId: 'response:12345678', type: 'response.ready', sessionId: 'sess-1', profile: 'default' };
  const legacy = notificationFor(event, preferences, { id: 'gw-1', name: 'legacy' });
  assert.equal(routing(legacy.data).dashboard_id, undefined);
  const noGateway = notificationFor(event, preferences);
  // Wire shape for unbound gateways: no key at all, not null.
  assert.equal('dashboard_id' in routing(legacy.data), false);
  assert.equal('dashboard_id' in routing(noGateway.data), false);
});

test('validateEvent drops a plugin-supplied dashboard_id: the event body is never a trust source', () => {
  const validated = validateEvent({
    type: 'response.ready',
    event_id: 'response:12345678',
    session_id: 'sess-1',
    dashboard_id: '99999999-9999-4999-8999-999999999999',
  });
  assert.equal(validated.dashboard_id, undefined);
  // The event object carries only the whitelisted fields.
  assert.equal('dashboard_id' in validated, false);
});

// ── Thread isolation across dashboards (#review round 2) ─────────────────
// One installation, two gateways bound to two dashboards, both using
// profile "default" and session "default": their notifications must never
// share a thread on the phone, while repeated events from the SAME
// gateway/session keep grouping together under a bounded thread token.

const gatewayA = { id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', name: 'A', dashboardId: '0f5c8a34-1b2d-4e5f-8a9b-0c1d2e3f4a5b' };
const gatewayB = { id: 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', name: 'B', dashboardId: '11111111-2222-4333-8444-555555555555' };

function sessionEvent(gateway, sessionId, suffix) {
  return {
    event: {
      eventId: `response:${suffix}`,
      type: 'response.ready',
      sessionId,
      profile: 'default',
    },
    gateway,
  };
}

test('identical sessions on different dashboards never share a thread', () => {
  const a = notificationFor(sessionEvent(gatewayA, 'default', 'e1').event, preferences, sessionEvent(gatewayA, 'default', 'e1').gateway);
  const b = notificationFor(sessionEvent(gatewayB, 'default', 'e2').event, preferences, sessionEvent(gatewayB, 'default', 'e2').gateway);
  assert.notEqual(a.data.thread, b.data.thread, 'A/default and B/default must never share a thread');
  // The routing carries the authenticated gateway's discriminator.
  assert.equal(routing(a.data).gateway_id, gatewayA.id);
  assert.equal(routing(b.data).gateway_id, gatewayB.id);
});

test('repeated events for the SAME gateway/session share a thread, whatever their type', () => {
  const first = notificationFor(sessionEvent(gatewayA, 'default', 'e1').event, preferences, sessionEvent(gatewayA, 'default', 'e1').gateway);
  const repeat = notificationFor(sessionEvent(gatewayA, 'default', 'e1').event, preferences, sessionEvent(gatewayA, 'default', 'e1').gateway);
  const sameSessionOtherType = notificationFor(
    { eventId: 'approval:1', type: 'approval.needed', sessionId: 'default', profile: 'default' },
    preferences,
    gatewayA,
  );
  assert.equal(first.data.thread, repeat.data.thread);
  assert.equal(first.data.thread, sameSessionOtherType.data.thread);
});

test('scoped threads are a bounded token, however long the session id', () => {
  const longSession = 'x'.repeat(200);
  const { data } = notificationFor(
    { eventId: 'response:1', type: 'background_task.finished', sessionId: longSession, profile: 'default' },
    preferences,
    gatewayA,
  );
  assert.match(data.thread, /^[0-9a-f]{16}$/);
});

test('a plugin-supplied gateway_id in the event body is dropped and can never rebind routing', () => {
  const validated = validateEvent({
    type: 'response.ready',
    event_id: 'response:gwspoof001',
    session_id: 'sess-1',
    gateway_id: 'evil-identity',
  });
  assert.equal('gateway_id' in validated, false, 'the event body is never a trust source for gateway_id');
  // The outgoing push's discriminator comes from the AUTHENTICATED record.
  const { data } = notificationFor(
    { eventId: 'response:gwspoof001', type: 'response.ready', sessionId: 'sess-1', profile: 'default' },
    preferences,
    gatewayA,
  );
  assert.equal(routing(data).gateway_id, gatewayA.id);
});

test('no-session threads are gateway-scoped: identical events differ per gateway', () => {
  const event = { eventId: 'cron:run-42', type: 'background_task.finished' };
  const a = notificationFor(event, preferences, gatewayA);
  const b = notificationFor(event, preferences, gatewayB);
  // Same gateway + same event → deterministic.
  assert.equal(a.data.thread, notificationFor(event, preferences, gatewayA).data.thread);
  // Cross-gateway → different threads.
  assert.notEqual(a.data.thread, b.data.thread);
  // Bounded and token-shaped.
  assert.match(a.data.thread, /^[0-9a-f]{16}$/);
});

test('gateway-less direct callers keep the legacy thread shapes', () => {
  const withSession = notificationFor(
    { eventId: 'response:legacy01', type: 'response.ready', sessionId: 'sess-legacy', profile: 'default' },
    preferences,
  );
  assert.equal(withSession.data.thread, 'sess-legacy');

  const noSession = notificationFor(
    { eventId: 'cron:run-42', type: 'background_task.finished' },
    preferences,
  );
  assert.equal(noSession.data.thread, 'hermes');
});

test('approval and clarify pushes chime by default; attention_sound opts out; completion keeps its own toggle', () => {
  const base = { eventId: 'e:12345678', sessionId: 'sess-1', profile: 'default' };
  const sound = (type, prefs) => notificationFor({ ...base, type }, prefs).data.sound;
  // Legacy stored preferences (no attention_sound key) default to sound on.
  assert.equal(sound('approval.needed', { completion_sound: false }), '1');
  assert.equal(sound('input.needed', { completion_sound: false }), '1');
  assert.equal(sound('approval.needed', { attention_sound: true }), '1');
  assert.equal(sound('approval.needed', { attention_sound: false, completion_sound: true }), undefined);
  assert.equal(sound('input.needed', { attention_sound: false, completion_sound: true }), undefined);
  // Completion sound is unchanged and independent of attention_sound.
  assert.equal(sound('response.ready', { completion_sound: true, attention_sound: false }), '1');
  assert.equal(sound('response.ready', { completion_sound: false, attention_sound: true }), undefined);
  assert.equal(sound('turn.failed', { completion_sound: true, attention_sound: true }), undefined);
});

test('normalizePreferences defaults attention_sound on and keeps an explicit opt-out', () => {
  assert.equal(normalizePreferences({}).attention_sound, true);
  assert.equal(normalizePreferences({ attention_sound: false }).attention_sound, false);
});

test('normalizePreferences treats null as the default preference object', () => {
  assert.deepEqual(normalizePreferences(null), normalizePreferences());
});

// ── End-to-end encrypted events (#431) ─────────────────────────────────

const envelope = {
  v: 1,
  kid: '0123456789abcdef0123456789abcdef',
  msg: 'input:0123456789abcdef0123456789abcdef',
  iat: Math.floor(Date.now() / 1000),
  tok: '0123456789abcdef',
  z: 1,
  req: 'talaria-push-abc123def456',
  n: 'AAAAAAAAAAAAAAAA',
  ct: 'A'.repeat(400),
};

function encryptedEvent(overrides = {}) {
  return {
    type: 'input.needed',
    event_id: envelope.msg,
    e2e: { ...envelope },
    clarify: { request_id: envelope.req, qids: ['q0', 'q1'], card: true },
    // Plaintext content beside an envelope is never forwarded.
    title: 'Secret title',
    body: 'Secret body',
    session_id: 'sess-secret',
    profile: 'secret-profile',
    decision: clarifyDecision,
    ...overrides,
  };
}

test('validateEvent keeps an envelope and drops every plaintext content field', () => {
  const event = validateEvent(encryptedEvent());
  assert.deepEqual(event.e2e, envelope);
  assert.deepEqual(event.clarify, { request_id: envelope.req, qids: ['q0', 'q1'], card: true });
  for (const field of ['title', 'body', 'sessionId', 'profile', 'gateway', 'decision']) {
    assert.equal(event[field], undefined, field);
  }
});

test('validateEvent rejects malformed envelopes instead of falling back to plaintext', () => {
  const broken = [
    { v: 2 },
    { kid: 'ABC' },
    { msg: 'input:other0000000000' },
    { iat: 1.5 },
    { iat: Math.floor(Date.now() / 1000) - 26 * 60 * 60 },
    { iat: Math.floor(Date.now() / 1000) + 3 * 60 * 60 },
    { tok: 'zz' },
    { z: 2 },
    { req: 'x' },
    { n: 'short' },
    { ct: 'A'.repeat(2601) },
    { ct: 'not base64url!' },
  ];
  for (const patch of broken) {
    assert.throws(() => validateEvent(encryptedEvent({ e2e: { ...envelope, ...patch } })), /invalid_e2e/, JSON.stringify(patch));
  }
  // The parked request id must be the one the ciphertext is bound to.
  assert.throws(
    () => validateEvent(encryptedEvent({ clarify: { request_id: 'talaria-push-000000000000', qids: [] } })),
    /invalid_e2e/,
  );
});

test('notificationFor sends the generic alert and the untouched envelope', () => {
  const event = validateEvent(encryptedEvent());
  const { data, urgent } = notificationFor(event, { show_previews: true, attention_sound: true }, { id: 'gw-1', dashboardId: '0f5c8a34-1b2d-4e5f-8a9b-0c1d2e3f4a5b' });
  assert.equal(urgent, true);
  assert.equal(data.title, 'Input needed');
  assert.equal(data.body, 'Hermes needs your response before it can continue.');
  assert.equal(data.sound, '1');
  assert.deepEqual(JSON.parse(data.talaria_e2e), envelope);
  assert.deepEqual(routing(data), { type: 'input.needed', e2e: 1, gateway_id: 'gw-1', dashboard_id: '0f5c8a34-1b2d-4e5f-8a9b-0c1d2e3f4a5b' });
  assert.deepEqual(Object.keys(data).sort(), ['body', 'sound', 'talaria', 'talaria_e2e', 'thread', 'title']);
  assert.notEqual(data.thread, envelope.tok, 'the thread token is scoped by gateway');
  for (const secret of ['Secret', 'sess-secret', 'secret-profile', 'Which color?']) {
    assert.ok(!JSON.stringify(data).includes(secret), secret);
  }
});

test('an envelope that would overflow FCM is dropped, never truncated', () => {
  const event = validateEvent(encryptedEvent({ e2e: { ...envelope, ct: 'A'.repeat(2600) }, event_id: envelope.msg }));
  // Push the data over the cap with a long gateway id stand-in.
  const { data } = notificationFor(event, {}, { id: 'g'.repeat(1400) });
  assert.equal(data.talaria_e2e, undefined);
  assert.equal(JSON.stringify(data).includes(envelope.n), false, 'no part of the envelope rides along');
  assert.equal(routing(data).e2e, 1);
  assert.equal(data.title, 'Input needed');
  assert.ok(dataBytes(data) <= FCM_MAX_DATA_BYTES);
});

// ── Hermes calls you (#449) ──────────────────────────────────────────────

const callBody = {
  type: 'call.requested',
  event_id: 'call:0123456789abcdef01234567',
  session_id: 'st-1',
  profile: 'default',
  title: 'Hermes wants to talk',
  body: '“Check the server” finished.',
  call: { id: '0123456789abcdef01234567', kind: 'done', title: 'Check the server', session_ids: ['rt-1', 'st-1'] },
};

test('validateEvent accepts a call request and keeps its job', () => {
  const event = validateEvent(callBody);
  assert.equal(event.type, 'call.requested');
  assert.deepEqual(event.call, { id: '0123456789abcdef01234567', kind: 'done', title: 'Check the server', session_ids: ['rt-1', 'st-1'] });
});

test('validateEvent bounds the call and drops a malformed one', () => {
  const bounded = validateEvent({ ...callBody, call: { ...callBody.call, title: undefined, session_ids: ['a', 'a', 'b', 'c', 'd', 'e'] } });
  assert.deepEqual(bounded.call, { id: callBody.call.id, kind: 'done', session_ids: ['a', 'b', 'c', 'd'] });
  for (const call of [null, [], { ...callBody.call, kind: 'maybe' }, { ...callBody.call, id: 'short' }, { ...callBody.call, id: 'has space here' }, { ...callBody.call, session_ids: [] }, { ...callBody.call, session_ids: 'rt-1' },
    { ...callBody.call, session_ids: ['bad\nid'] }, { ...callBody.call, session_ids: ['a', 'bad\nid'] }, { ...callBody.call, session_ids: ['a', 'x'.repeat(SESSION_KEY_MAX_BYTES + 1)] }, { ...callBody.call, session_ids: ['a', 7] }]) {
    assert.equal(validateEvent({ ...callBody, call }).call, undefined, JSON.stringify(call));
  }
  // Only a call request carries a call.
  assert.equal(validateEvent({ ...callBody, type: 'response.ready' }).call, undefined);
});

test('a call request keeps gateway session keys whole', () => {
  const event = validateEvent({ ...callBody, call: { ...callBody.call, session_ids: gatewaySessionKeys.slice(0, 4) } });
  assert.deepEqual(event.call.session_ids, gatewaySessionKeys.slice(0, 4));
});

test('a call request is a notification with the call category, its job and the generic copy when previews are off', () => {
  const { data, urgent } = notificationFor(validateEvent(callBody), { show_previews: false }, { id: 'gw-1' });
  assert.equal(urgent, true);
  assert.equal(data.category, 'HERMES_CALL');
  assert.equal(data.sound, '1');
  assert.equal(data.title, 'Hermes wants to talk');
  assert.match(data.body, /^Tap to talk to Hermes\./);
  assert.ok(!data.body.includes('Check the server'));
  const { title: _title, ...untitled } = validateEvent(callBody).call;
  assert.deepEqual(routing(data).call, untitled, 'the job title stays home with previews off');
  assert.ok(!JSON.stringify(data).includes('Check the server'));
  const silenced = notificationFor(validateEvent(callBody), { attention_sound: false }, { id: 'gw-1' });
  assert.equal(silenced.data.sound, undefined);
});

test('a call request with previews on shows the plugin copy; other pushes get no call category', () => {
  const { data } = notificationFor(validateEvent(callBody), { show_previews: true }, { id: 'gw-1' });
  assert.equal(data.title, 'Hermes wants to talk');
  assert.equal(data.body, '“Check the server” finished.');
  assert.deepEqual(routing(data).call, validateEvent(callBody).call);
  const ready = notificationFor(validateEvent({ ...callBody, type: 'response.ready' }), { show_previews: true }, { id: 'gw-1' });
  assert.equal(ready.data.category, undefined);
});

test('call requests have their own preference, on by default', () => {
  assert.equal(normalizePreferences({}).call_requested, true);
  assert.equal(normalizePreferences({ call_requested: false }).call_requested, false);
});

test('an encrypted call request keeps the call category and seals its job', () => {
  const event = validateEvent({ ...callBody, event_id: envelope.msg, e2e: { ...envelope, req: '' } });
  assert.equal(event.call, undefined);
  const { data } = notificationFor(event, {}, { id: 'gw-1' });
  assert.equal(data.category, 'HERMES_CALL');
  assert.deepEqual(JSON.parse(data.talaria_e2e), event.e2e);
  assert.equal(data.title, 'Hermes wants to talk');
  assert.equal(data.body, 'Tap to talk to Hermes.');
  assert.ok(!JSON.stringify(data).includes('Check the server'));
});

test('a call can say why Hermes calls and be about an approval or a question', () => {
  const event = validateEvent({ ...callBody, call: { ...callBody.call, kind: 'approval', reason: `  Needs your OK\n${'x'.repeat(300)}` } });
  assert.equal(event.call.kind, 'approval');
  assert.equal(event.call.reason.length, 200);
  assert.ok(event.call.reason.startsWith('Needs your OK'));
  assert.equal(validateEvent({ ...callBody, call: { ...callBody.call, kind: 'question' } }).call.kind, 'question');
  const { data } = notificationFor(event, { show_previews: false }, { id: 'gw-1' });
  assert.equal(routing(data).call.reason, undefined, 'the reason stays home with previews off, like the title');
  assert.ok(routing(notificationFor(event, { show_previews: true }, { id: 'gw-1' }).data).call.reason);
});

// ── Calendar changes for the phone (phone.changes) ────────────────────

const phoneBody = {
  type: 'phone.changes',
  event_id: 'phone:ch_0123456789abcdef',
  profile: 'work',
  title: 'Hermes wants to add an event',
  body: 'Dentist · Fri 16 Oct 15:00',
};

test('a phone change is an alert unless it says background; any other mode is refused', () => {
  assert.equal(validateEvent(phoneBody).mode, 'alert');
  assert.equal(validateEvent({ ...phoneBody, mode: null }).mode, 'alert');
  assert.equal(validateEvent({ ...phoneBody, mode: 'background' }).mode, 'background');
  assert.throws(() => validateEvent({ ...phoneBody, mode: 'loud' }), (error) => error.status === 400 && error.message === 'invalid_mode');
  assert.equal(validateEvent({ ...callBody, mode: 'background' }).mode, undefined, 'mode belongs to phone changes only');
  const sealed = validateEvent({ ...phoneBody, mode: 'background', event_id: envelope.msg, e2e: { ...envelope, req: '' } });
  assert.equal(sealed.mode, 'background', 'mode stays readable beside the envelope');
  assert.equal(sealed.title, undefined);
});

test('a phone change that asks is a notification in its category, generic until previews are on', () => {
  const { data } = notificationFor(validateEvent(phoneBody), {}, { id: 'gw-1' });
  assert.equal(data.category, 'PHONE_CHANGE');
  assert.equal(data.title, 'Hermes has a change for you');
  assert.match(data.body, /^Tap to review it in Talaria\./);
  assert.equal(data.sound, '1', 'it waits on the user, so it chimes');
  assert.ok(!JSON.stringify(data).includes('Dentist'));
  const shown = notificationFor(validateEvent(phoneBody), { show_previews: true }, { id: 'gw-1' }).data;
  assert.equal(shown.title, 'Hermes wants to add an event');
  assert.equal(shown.body, 'Dentist · Fri 16 Oct 15:00');
});

test('a sealed phone change shows the fallback copy and keeps its category', () => {
  const event = validateEvent({ ...phoneBody, event_id: envelope.msg, e2e: { ...envelope, req: '' } });
  const { data } = notificationFor(event, {}, { id: 'gw-1' });
  assert.equal(data.title, 'Hermes has a change for you');
  assert.equal(data.body, 'Tap to review it in Talaria.');
  assert.equal(data.category, 'PHONE_CHANGE');
  assert.deepEqual(JSON.parse(data.talaria_e2e), event.e2e);
});

test('a background phone change is a silent normal-priority push with only routing', () => {
  const plain = phoneBackgroundPushFor(validateEvent({ ...phoneBody, mode: 'background' }), { id: 'gw-1', dashboardId: 'dash-1' }, { nowSeconds: 1_800_000_000 });
  assert.equal(plain.urgent, false);
  assert.equal(plain.expiration, 1_800_000_000 + 24 * 60 * 60);
  assert.deepEqual(Object.keys(plain.data), ['talaria'], 'nothing to show: no alert fields');
  assert.deepEqual(routing(plain.data), { type: 'phone.changes', mode: 'background', gateway_id: 'gw-1', dashboard_id: 'dash-1', profile: 'work' });
  assert.ok(!JSON.stringify(plain.data).includes('Dentist'), 'the change itself never rides the push');
  const event = validateEvent({ ...phoneBody, mode: 'background', event_id: envelope.msg, e2e: { ...envelope, req: '' } });
  const sealed = phoneBackgroundPushFor(event, { id: 'gw-1' });
  assert.deepEqual(routing(sealed.data), { type: 'phone.changes', mode: 'background', gateway_id: 'gw-1' }, 'no profile beside an envelope');
  assert.deepEqual(JSON.parse(sealed.data.talaria_e2e), event.e2e);
  assert.equal(sealed.data.title, undefined);
});

test('phone changes have their own preference, on by default', () => {
  assert.equal(normalizePreferences({}).phone_changes, true);
  assert.equal(normalizePreferences({ phone_changes: false }).phone_changes, false);
});
