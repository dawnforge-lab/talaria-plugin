import { createHash } from 'node:crypto';
import { createServer } from 'node:http';
import { mkdirSync, realpathSync, unlinkSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';
import { isIP } from 'node:net';
import { pathToFileURL } from 'node:url';
import { SWEEP_INTERVAL_MS } from './event-ledger.mjs';
import { FCM_MAX_DATA_BYTES, FcmClient, dataBytes, fcmConfigFromEnv, fcmMessage, validateFcmToken } from './fcm.mjs';
import { limitsFromEnv } from './limits.mjs';
import { LiveActivities, validateRegistration, validateReport } from './live-activities.mjs';
import { normalizeDashboardId, RelayStore, sanitizeBatchQuestions } from './store.mjs';
import { TokenChallenges, tokenChallengePush, tokenProofModeFromEnv } from './token-proof.mjs';

let config;
let store;
let limits;
// Sends one FCM message to a phone's token (fcm.mjs).
let fcmSend;
let liveActivities;
let tokenChallenges;

// The push data keys the app reads: the routing (with, unsealed, the
// content) and the sealed envelope.
const ROUTING_KEY = 'talaria';
const E2E_KEY = 'talaria_e2e';
// Hermes session keys (gateway `build_session_key`, custom API ids up to 256
// characters) carry the platform's own ids: Matrix `!room:server` and
// `@user:server`, WhatsApp `…@g.us`, a `main` profile's `agent:main~`. They
// are opaque routing strings, kept whole: one with a control character or
// over this many UTF-8 bytes is dropped, never cut (a cut key names another
// chat, or none). Mirrors the plugin's events.SESSION_KEY_MAX_BYTES; twice
// Hermes' 256-character cap, and small enough that a push carrying it twice
// stays well under FCM_MAX_DATA_BYTES.
export const SESSION_KEY_MAX_BYTES = 512;

// End-to-end encrypted events (#431): the plugin seals the content with a
// key this relay never sees and sends an `e2e` envelope instead. The relay
// routes on the visible fields (type, event id, thread token, clarify
// request id and question ids) and forwards the ciphertext untouched.
// The ciphertext cap leaves room for the alert, routing and envelope fields
// inside FCM_MAX_DATA_BYTES; the plugin keeps under it.
const E2E_MAX_CT_CHARS = 2600;
// Encrypted clarify answers are base64url ciphertext of up to 2,000
// characters of text (up to 4 bytes each in UTF-8).
export const E2E_ANSWER_PREFIX = 'e2e1.';
const E2E_MAX_ANSWER_CHARS = 11_000;
const E2E_ANSWER_PATTERN = new RegExp(`^${E2E_ANSWER_PREFIX.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}[0-9a-f]{32}\\.[A-Za-z0-9_-]{16}\\.[A-Za-z0-9_-]{22,10900}$`);
// The sealed answer's header with a ciphertext of any length: an answer of
// this shape the pattern above refuses is a broken or oversized sealed
// answer, never plaintext.
const E2E_ANSWER_SHAPE = new RegExp(`^${E2E_ANSWER_PREFIX.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}[0-9a-f]{32}\\.[A-Za-z0-9_-]{16}\\.[A-Za-z0-9_-]+$`);
// Plaintext clarify answers, in characters (UTF-16 units) after trimming.
const MAX_ANSWER_CHARS = 2000;
// Stands in for question text the relay can't read, so a parked batch keeps
// its qids (the store sanitizer drops questions without text).
const E2E_QUESTION_PLACEHOLDER = '[encrypted]';

// Hermes calls you (#449): a call request is a notification the app shows
// with a Talk action (this category), about one job: `call` names it.
// Kinds: how the job ended, or (asked through Hermes itself) why it calls.
const CALL_CATEGORY = 'HERMES_CALL';
const CALL_KINDS = new Set(['done', 'failed', 'stopped', 'approval', 'question']);
const CALL_WINDOW_MS = 24 * 60 * 60 * 1000;
// Calendar and reminder changes Hermes queued for the phone (phone.changes):
// one that asks is a notification in this category (the app opens the change
// card); one the user lets Hermes make without asking is a silent push that
// wakes the app to apply it. This is the relay's ceiling on those per phone.
const PHONE_CHANGE_CATEGORY = 'PHONE_CHANGE';
const PHONE_CHANGE_MODES = new Set(['alert', 'background']);
const PHONE_BACKGROUND_PER_HOUR = 30;
const PHONE_BACKGROUND_TTL_S = 24 * 60 * 60;
// A location request is only worth answering right away.
const LOCATION_PUSH_TTL_S = 60;
// Locate and maps lookups (talaria_maps) share it; the plugin keeps tighter budgets per kind.
// Test seam: the test suite can lower it to reach the limit quickly.
const LOCATION_PUSH_PER_HOUR = process.env.RELAY_TEST_SEAMS === '1' && /^\d{1,3}$/.test(process.env.RELAY_TEST_LOCATION_PUSHES_PER_HOUR || '')
  ? Number(process.env.RELAY_TEST_LOCATION_PUSHES_PER_HOUR)
  : 60;
// The answers the plugin retries: a send it can try again.
const RETRYABLE_STATUS = (status) => status === 408 || status === 429 || status >= 500;
// Event ids whose push is being sent -> a promise of whether it was kept
// (delivered, or failed in a way a retry can't fix).
const eventsInFlight = new Map();
// Registration proof (token-proof.mjs): how long a pushed challenge stays
// usable, and how often one device token may be challenged (each is a push).
const TOKEN_CHALLENGE_TTL_S = 10 * 60;
const TOKEN_CHALLENGES_PER_TOKEN = 5;
const TOKEN_CHALLENGE_WINDOW_MS = 10 * 60_000;

// Dashboard UUIDs (opaque app-generated identity, #148) are
// canonicalized and validated in the store (normalizeDashboardId); the route
// below pre-validates so the device gets a 400 where it is actionable.

function main() {
  config = readConfig();
  assertDataDirectoryWritable(config.dataPath);
  store = new RelayStore(config.dataPath, config.storeLimits);
  // Accepting an event sweeps expired IDs; this frees them on a quiet relay.
  // Sweeps throttle themselves, so the timer runs just past that interval.
  setInterval(() => store.events.sweep(), SWEEP_INTERVAL_MS + 1_000).unref();
  const fcm = FcmClient.fromFile(config.fcm.serviceAccountPath, { origin: config.fcm.origin, allowInsecure: config.fcm.allowInsecure });
  fcmSend = (token, message) => fcm.send(token, message);
  limits = new Map();
  tokenChallenges = new TokenChallenges({ ttlMs: TOKEN_CHALLENGE_TTL_S * 1000 });
  // A reclaimed slot's installation leaves nothing behind in memory either.
  store.onReclaim = (id) => {
    releaseInstallation(id);
    console.warn(JSON.stringify({ level: 'warn', message: 'relay full: reclaimed the longest-inactive installation', installation_id: id }));
  };
  setInterval(() => tokenChallenges.sweep(), 60_000).unref();

  liveActivities = new LiveActivities({
    maxActivities: config.liveActivityLimits.maxActivities,
    maxPerInstallation: config.liveActivityLimits.maxPerInstallation,
    // A live turn's update goes to the phone's own FCM token.
    send: (installationId, push) => {
      const installation = store.activeInstallation(installationId);
      if (!installation) return Promise.resolve({ ok: false, status: 410, reason: 'installation_inactive', tokenGone: true });
      return deviceSend(installation, installation.deviceToken, push);
    },
    warn: (message, fields) => console.warn(JSON.stringify({ ...fields, level: 'warn', message })),
  });
  setInterval(() => liveActivities.sweep(), 10 * 60_000).unref();

  const server = createServer(async (request, response) => {
    const startedAt = Date.now();
    try {
      await route(request, response);
    } catch (error) {
      const status = Number(error?.status ?? 500);
      const code = status < 500 && error instanceof Error ? error.message : 'internal_error';
      if (status >= 500) console.error(JSON.stringify({ level: 'error', message: 'request failed', error: error instanceof Error ? error.message : String(error) }));
      sendJson(response, status, { error: code });
    } finally {
      console.log(JSON.stringify({ level: 'info', method: request.method, path: safePath(request.url), status: response.statusCode, duration_ms: Date.now() - startedAt }));
    }
  });

  server.listen(config.port, config.host, () => {
    console.log(JSON.stringify({ level: 'info', message: 'Talaria relay listening', host: config.host, port: config.port, public_url: config.publicUrl }));
  });

  // Write deferred metadata before exiting, even if open connections keep
  // server.close waiting until the container is killed.
  const shutdown = () => {
    flushStore();
    server.close(() => process.exit(0));
  };
  process.on('SIGTERM', shutdown);
  process.on('SIGINT', shutdown);
  process.on('exit', flushStore);
}

function flushStore() {
  try {
    store.flush();
  } catch (error) {
    console.error(JSON.stringify({ level: 'error', message: 'relay save on shutdown failed', error: error instanceof Error ? error.message : String(error) }));
  }
}

// Start the server only when executed directly, so the pure notification
// builders can be unit-tested by importing this module without binding a port.
// Resolve argv[1] through realpath so a symlinked entrypoint still matches
// import.meta.url (which Node resolves to the real file). If an entrypoint
// exists but does not match, say so on stderr — a silent no-op start is the
// worst possible deployment failure mode.
function isEntryPoint() {
  if (!process.argv[1]) return false;
  try {
    return pathToFileURL(realpathSync(process.argv[1])).href === import.meta.url;
  } catch {
    return false;
  }
}
if (isEntryPoint()) {
  main();
} else if (process.argv[1] && !process.env.NODE_TEST_CONTEXT) {
  console.error(JSON.stringify({
    level: 'warn',
    message: 'relay module imported without matching entrypoint; not starting server',
    argv1: process.argv[1],
    module: import.meta.url,
  }));
}

async function route(request, response) {
  const url = new URL(request.url ?? '/', config.publicUrl);
  const client = clientAddress(request);
  if (request.method === 'GET' && url.pathname === '/healthz') return sendJson(response, 200, { ok: true });

  // Registration proof (#142, #146): push a one-time nonce to the device
  // token; the phone sends it back with the registration or token change.
  if (request.method === 'POST' && url.pathname === '/v1/token-challenges') {
    enforceRateLimit(`token-challenge:${client}`, 12, 60_000);
    const body = await readJson(request, 4_096);
    const target = registrationTarget(body);
    const { deviceToken } = target;
    enforceRateLimit(`token-challenge-token:${deviceToken}`, TOKEN_CHALLENGES_PER_TOKEN, TOKEN_CHALLENGE_WINDOW_MS);
    enforceRateLimit('token-challenge-global', config.budgets.tokenChallengesPerMinute, 60_000);
    const nonce = tokenChallenges.issue(deviceToken);
    let result;
    try {
      result = await deviceSend(target, deviceToken, tokenChallengePush(nonce, { ttlSeconds: TOKEN_CHALLENGE_TTL_S }));
    } catch (error) {
      console.error(JSON.stringify({ level: 'error', message: 'token challenge send threw', error: error instanceof Error ? error.message : String(error) }));
      tokenChallenges.withdraw(deviceToken, nonce);
      return sendJson(response, 502, { error: 'push_unreachable' });
    }
    if (!result.ok) {
      if (deviceTokenRefused(result)) {
        tokenChallenges.forget(deviceToken);
        return sendJson(response, 400, { error: 'invalid_device_token' });
      }
      // An earlier challenge the phone already got stays usable.
      tokenChallenges.withdraw(deviceToken, nonce);
      return sendJson(response, 502, { error: 'push_rejected', reason: result.reason, status: result.status });
    }
    return sendJson(response, 202, { challenge_sent: true, expires_in: TOKEN_CHALLENGE_TTL_S });
  }

  if (request.method === 'POST' && url.pathname === '/v1/installations') {
    enforceRateLimit(`registration:${client}`, 12, 60_000);
    const body = await readJson(request);
    const { bundleId, deviceToken } = registrationTarget(body);
    store.assertInstallationRoom();
    enforceRegistrationBudget();
    // Proof before reclaiming: only a registration that can succeed may
    // take an inactive installation's slot.
    const proven = proveDeviceToken(deviceToken, body.token_proof);
    store.assertInstallationCapacity();
    const created = store.createInstallation({ bundleId, deviceToken, preferences: body.preferences });
    // A proven phone's earlier installations are its own stale records,
    // retired once this one exists.
    if (proven) retireOtherInstallations(deviceToken, created.installation.id);
    return sendJson(response, 201, {
      installation: created.installation,
      credential: `${created.installation.id}.${created.deviceSecret}`,
      relay_url: config.publicUrl,
    });
  }

  const installationMatch = url.pathname.match(/^\/v1\/installations\/([0-9a-f-]+)$/i);
  if (installationMatch && request.method === 'PUT') {
    const installation = authorize(request, installationMatch[1], 'device');
    if (!installation) return sendJson(response, 401, { error: 'unauthorized' });
    enforceRateLimit(`installation-update:${installation.id}`, 30, 60_000);
    const body = await readJson(request);
    const deviceToken = body.device_token === undefined ? undefined : validateFcmToken(body.device_token);
    const moving = Boolean(deviceToken && deviceToken !== installation.deviceToken);
    const changes = { deviceToken, preferences: body.preferences };
    let proven = false;
    if (moving) {
      enforceDeviceChangeBudget();
      // Moving to another device token takes the same proof as registering it.
      proven = proveDeviceToken(deviceToken, body.token_proof);
    } else if (store.wouldUpdateInstallation(installation.id, changes)) {
      enforceDeviceChangeBudget();
    }
    const updated = store.updateInstallation(installation.id, changes);
    if (proven) retireOtherInstallations(deviceToken, installation.id);
    return sendJson(response, 200, { installation: updated });
  }
  if (installationMatch && request.method === 'DELETE') {
    const installation = authorize(request, installationMatch[1], 'device');
    if (!installation) return sendJson(response, 401, { error: 'unauthorized' });
    enforceRevocationBudget();
    deactivateInstallation(installation.id);
    response.writeHead(204).end();
    return;
  }

  // Live turn progress (live-activities.mjs): the phone registers an
  // activity id it made under the chat's session ids when it sends a turn.
  const activityMatch = url.pathname.match(/^\/v1\/installations\/([0-9a-f-]+)\/live-activities$/i);
  if (activityMatch && request.method === 'PUT') {
    enforceRateLimit(`live-activity-ip:${client}`, 120, 60_000);
    const installation = authorize(request, activityMatch[1], 'device');
    if (!installation) return sendJson(response, 401, { error: 'unauthorized' });
    enforceRateLimit(`live-activity-device:${installation.id}`, 60, 60_000);
    const registration = validateRegistration(await readJson(request, 4_096));
    if (!registration) return sendJson(response, 400, { error: 'invalid_live_activity' });
    enforceRateLimit('live-activity-registration-global', config.budgets.liveActivityRegistrationsPerMinute, 60_000);
    liveActivities.register(installation.id, registration.token, registration.sessionIds);
    return sendJson(response, 200, { registered: true });
  }

  // The host reports a turn's progress; it goes to the phone only when the
  // phone registered an activity for that chat. `tracked` tells the host
  // whether anyone is watching, so it can stop reporting a turn nobody is.
  if (request.method === 'POST' && url.pathname === '/v1/live-activity') {
    const gateway = authenticatedGateway(request);
    if (!gateway) return sendJson(response, 401, { error: 'unauthorized' });
    enforceRateLimit(`live-activity-host:${gateway.installationId}`, 120, 60_000);
    const report = validateReport(await readJson(request, 4_096));
    if (!report) return sendJson(response, 400, { error: 'invalid_live_activity_report' });
    if (!store.activeInstallation(gateway.installationId)) return sendJson(response, 200, { tracked: false });
    const watched = liveActivities.find(gateway.installationId, report.sessionId);
    if (watched) enforceRateLimit('live-activity-update-global', config.budgets.liveActivityUpdatesPerMinute, 60_000);
    const tracked = liveActivities.report(gateway.installationId, report.sessionId, report.state, report.end);
    return sendJson(response, 200, { tracked });
  }

  const pairingMatch = url.pathname.match(/^\/v1\/installations\/([0-9a-f-]+)\/pairings$/i);
  if (pairingMatch && request.method === 'POST') {
    const installation = authorize(request, pairingMatch[1], 'device');
    if (!installation) return sendJson(response, 401, { error: 'unauthorized' });
    enforceRateLimit(`pairing:${installation.id}`, 5, 60_000);
    const body = await readJson(request);
    // Optional dashboard binding (#148): the authenticated device names the
    // saved dashboard this pairing is for. It is bound NOW, at
    // pairing creation — never per event — so an event can never re-route
    // itself to a different dashboard. Absent (older devices) keeps the
    // pre-dashboard behavior; malformed values are rejected rather than
    // silently dropped so the device learns its binding did not land.
    let dashboardId;
    if (body.dashboard_id !== undefined && body.dashboard_id !== null) {
      // Normalize/validate here so the device gets a 400 where it is
      // actionable; the store re-validates at the persistence boundary.
      try {
        dashboardId = normalizeDashboardId(body.dashboard_id);
      } catch {
        return sendJson(response, 400, { error: 'invalid_dashboard_id' });
      }
    }
    enforceDeviceChangeBudget();
    const pairing = store.createPairing(installation.id, dashboardId);
    return sendJson(response, 201, { pairing_code: pairing.code, expires_at: pairing.expiresAt });
  }

  if (request.method === 'POST' && url.pathname === '/v1/pairings/claim') {
    enforceRateLimit(`claim:${client}`, 20, 60_000);
    const body = await readJson(request);
    if (!store.canClaimPairing(body.pairing_code)) return sendJson(response, 404, { error: 'invalid_or_expired_pairing' });
    enforceDeviceChangeBudget();
    const claimed = store.claimPairing(body.pairing_code, body.gateway_name);
    if (!claimed) return sendJson(response, 404, { error: 'invalid_or_expired_pairing' });
    return sendJson(response, 200, {
      installation_id: claimed.installationId,
      gateway_id: claimed.gatewayId,
      credential: `${claimed.installationId}.${claimed.gatewayId}.${claimed.gatewaySecret}`,
      relay_url: config.publicUrl,
    });
  }

  if (request.method === 'POST' && url.pathname === '/v1/events') {
    const credential = gatewayCredential(request);
    if (!credential) return sendJson(response, 401, { error: 'unauthorized' });
    const authenticated = store.authenticateGateway(credential.installationId, credential.gatewayId, credential.secret);
    if (!authenticated) return sendJson(response, 401, { error: 'unauthorized' });
    const { installation } = authenticated;
    enforceRateLimit(`event:${installation.id}`, 30, 60_000);
    const body = await readJson(request);
    const event = validateEvent(body);
    // The event body is NOT a source of dashboard identity: validateEvent
    // whitelists fields, so any plugin-supplied dashboard_id is dropped
    // here. Outgoing routing carries the dashboard binding of the
    // AUTHENTICATED gateway credential (bound at pairing/claim time).
    const duplicateEvent = store.hasAcceptedEvent(installation.id, event.eventId, credential.gatewayId);
    // A copy of an event whose push is still being sent (the plugin timed out
    // waiting and retried) waits for that send: a duplicate answer now would
    // end the plugin's retries even if the send then fails and gives its id
    // back. A failed send is answered as one, so the plugin tries again.
    const inFlightKey = `${installation.id}:${credential.gatewayId}:${event.eventId}`;
    const inFlight = duplicateEvent ? eventsInFlight.get(inFlightKey) : undefined;
    if (inFlight) {
      if (await inFlight) return sendJson(response, 200, { accepted: true, duplicate: true });
      return sendJson(response, 503, { error: 'delivery_failed' });
    }
    // Keep capacity preflight through savePendingDecision synchronous with no
    // await: otherwise requests could interleave after this check and consume
    // the same last slot. A catch after acceptEvent would still consume the ID.
    const clarifyRequestId = event.e2e
      ? event.clarify?.request_id
      : (event.decision?.kind === 'clarify' ? event.decision.request_id : undefined);
    if (!duplicateEvent && clarifyRequestId) {
      try {
        store.assertPendingDecisionCapacity(
          installation.id,
          RelayStore.decisionKey(installation.id, credential.gatewayId, clarifyRequestId),
        );
      } catch (error) {
        if (error?.code === 'decision_capacity_exceeded') {
          return sendJson(response, 429, { error: 'decision_capacity_exceeded' });
        }
        throw error;
      }
    }
    if (duplicateEvent && !event.pluginVersion) return sendJson(response, 200, { accepted: true, duplicate: true });
    enforceEventBudget();
    // The ceiling counts every new call request that gets this far, delivered
    // or not (paused, previews off, no device), and each retry of one that
    // failed: a host past it is misbehaving.
    if (!duplicateEvent && event.type === 'call.requested') enforceCallCeiling(installation.id);
    // Plugin version recording runs BEFORE the dedupe return: a second
    // gateway on the same installation running the same plugin version sends
    // the same deterministic plugin.hello id, and it must still be recorded.
    // Re-recording identical state is idempotent, unlike pending decisions.
    if (event.pluginVersion) {
      store.recordGatewayPlugin(installation.id, credential.gatewayId, {
        version: event.pluginVersion,
        capabilities: event.pluginCapabilities,
      });
    }
    // On the non-clarify path there must be no await from here until
    // eventsInFlight.set below: a copy arriving in between would see the id
    // taken with no send to wait for, and be told duplicate before the
    // send's outcome is known.
    if (!store.acceptEvent(installation.id, event.eventId, credential.gatewayId)) return sendJson(response, 200, { accepted: true, duplicate: true });
    // Control event: version announcement only, never a notification.
    if (event.type === 'plugin.hello') return sendJson(response, 202, { accepted: true, delivered: false });
    const { gateway } = authenticated;
    // A clarify decision carries a plugin-minted request id; park it so the
    // device can answer by id and the gateway can poll for the answer while
    // its middleware blocks the tool call. Saved after the dedupe check so a
    // re-delivered event can never overwrite (and wipe the answer of) the
    // already-parked decision.
    //
    // Two independent concepts must not be conflated:
    //
    //   notification_delivery  — did the user get the (plain or card-bearing)
    //                            input.needed banner? Governed by the
    //                            ordinary input.needed preference.
    //   relay_decision_deliverability (`deliverable`) — does the delivered
    //                            push contain an ANSWERABLE structured card?
    //                            Governed by decision_cards preference and
    //                            the FCM size guard.
    //
    // The final notification is built first and `deliverable` is taken from
    // what survived into the push. A decision whose card was stripped
    // (preference or size guard) still sends the plain banner and is parked
    // undeliverable, so the plugin stops relay-polling and falls back to
    // Hermes' native clarify path — it never suppresses the ordinary
    // notification.
    let parkedDecisionId = null;
    if (clarifyRequestId) {
      parkedDecisionId = clarifyRequestId;
      const deliverableBase = shouldDeliver(installation.preferences, event.type);
      const notification = deliverableBase
        ? notificationFor(event, installation.preferences, gateway)
        : null;
      // An encrypted card is deliverable when the plugin fit it in the
      // envelope, the device wants cards, and the envelope survived the size
      // guard; a plaintext one when the decision survived it.
      const decisionDeliverable = event.e2e
        ? Boolean(notification?.data[E2E_KEY]) && event.clarify.card && installation.preferences.decision_cards !== false
        : Boolean(notification && JSON.parse(notification.data[ROUTING_KEY]).decision);
      // An encrypted decision parks only what routing needs: its question
      // ids, never question text or choices.
      const saved = store.savePendingDecision(event.e2e
        ? {
            id: parkedDecisionId,
            installationId: installation.id,
            gatewayId: credential.gatewayId,
            question: E2E_QUESTION_PLACEHOLDER,
            choices: [],
            questions: event.clarify.qids.map((qid) => ({ qid, question: E2E_QUESTION_PLACEHOLDER, choices: [] })),
            deliverable: decisionDeliverable,
          }
        : {
            id: parkedDecisionId,
            installationId: installation.id,
            gatewayId: credential.gatewayId,
            question: event.decision.question,
            choices: event.decision.choices,
            questions: event.decision.questions,
            deliverable: decisionDeliverable,
          });
      if (!saved) return sendJson(response, 200, { accepted: true, duplicate: true });
      if (!notification) return sendJson(response, 202, { accepted: true, delivered: false });
      let result;
      // The token this push went to: the device can register a new one meanwhile.
      const sentToken = installation.deviceToken;
      try {
        result = await deviceSend(installation, sentToken, notification);
      } catch (error) {
        // A THROWN transport failure (dropped connection, DNS, TLS) is the
        // same outcome as a returned rejection: no device received the
        // answerable card, so the parked decision must flip to undeliverable
        // and the failure must still be reported upstream.
        store.markPendingDecisionUndeliverable(installation.id, credential.gatewayId, parkedDecisionId);
        console.error(JSON.stringify({ level: 'error', message: 'push send threw', error: error instanceof Error ? error.message : String(error) }));
        return sendJson(response, 502, { error: 'push_unreachable' });
      }
      if (deviceTokenRefused(result)) deactivateInstallationForToken(installation, sentToken);
      if (!result.ok) {
        // The answerable card never reached the device: flip the parked
        // decision to undeliverable so the plugin's next poll falls back to
        // the native clarify path instead of waiting out the budget.
        store.markPendingDecisionUndeliverable(installation.id, credential.gatewayId, parkedDecisionId);
        return sendJson(response, 502, { error: 'push_rejected', reason: result.reason, status: result.status });
      }
      return sendJson(response, 202, { accepted: true, delivered: true });
    }
    // The id was taken before the send so a second copy arriving meanwhile
    // stays a duplicate. A push that then fails (Google refused or
    // unreachable, an hourly limit) gives the id back: the plugin retries with the same
    // id, and that retry must send rather than be told it was a duplicate.
    // A clarify decision keeps its id, because a retry can't re-park it and
    // its undeliverable flag already sends the plugin to Hermes' own path.
    // One edge stays open by design: a send Google took whose answer was lost
    // (a timeout) can show twice on the phone. Delivering twice beats never.
    // Only a failure a retry can fix gives the id back: a request the relay
    // calls malformed (400) would fail the same way, so it keeps deduping.
    let settle;
    eventsInFlight.set(inFlightKey, new Promise((resolve) => { settle = resolve; }));
    let released = true;
    let thrownStatus = null;
    try {
      await deliverEvent(response, installation, event, gateway);
      released = RETRYABLE_STATUS(response.statusCode);
    } catch (error) {
      thrownStatus = Number(error?.status ?? 500);
      throw error;
    } finally {
      // Waiting copies are settled first so nothing below can strand them;
      // they resume only after this block, by which time the id is free.
      eventsInFlight.delete(inFlightKey);
      settle(!released);
      if (released) store.releaseEvent(installation.id, event.eventId, credential.gatewayId);
      if (released) console.warn(JSON.stringify({ level: 'warn', message: 'event push failed, id released for a retry', type: event.type, event_id: event.eventId, status: response.headersSent ? response.statusCode : thrownStatus, thrown: thrownStatus !== null }));
    }
    return;
  }

  // ── Clarify answer loop ──────────────────────────────────────────────
  // The device answers at /v1/decisions/{id}/respond; the gateway polls and
  // releases at /v1/decisions/{id}.
  const decisionMatch = url.pathname.match(/^\/v1\/decisions\/([A-Za-z0-9_-]{4,128})(\/respond)?$/);
  if (decisionMatch && decisionMatch[2] !== undefined && request.method === 'POST') {
    // The device that received the push answers by the plugin-minted id.
    // Authenticate and rate-limit before reading the body: this endpoint is
    // publicly reachable, and unauthenticated request bodies must never be
    // parsed (matches the pre-auth posture of /v1/pairings/claim).
    const id = decisionMatch[1];
    enforceRateLimit(`decision-respond-ip:${client}`, 60, 60_000);
    const credential = bearerCredential(request);
    if (!credential) return sendJson(response, 401, { error: 'unauthorized' });
    const installation = store.authenticate(credential.id, credential.secret, 'device');
    if (!installation) return sendJson(response, 401, { error: 'unauthorized' });
    enforceRateLimit(`decision-respond:${installation.id}`, 30, 60_000);
    const body = await readJson(request);
    const answer = validAnswer(body.answer);
    // question_id scopes the answer to ONE question of a batch decision
    // (first-answer-wins per qid, other qids stay open); its absence answers
    // a single-question card.
    const questionId = cleanText(body.question_id, 40);
    // gateway_id is the decision-routing discriminator: two gateways on one
    // installation can park SAME-id decisions, so the device names the
    // gateway it is answering (the id comes from the push's routing, which
    // the relay stamped from the AUTHENTICATED gateway — never from the
    // plugin event body).
    const gatewayId = cleanIdentifier(body.gateway_id, 80);
    if (!gatewayId) return sendJson(response, 400, { error: 'invalid_gateway_id' });
    if (store.wouldRespondPendingDecision(installation.id, gatewayId, id, questionId)) enforceDecisionBudget();
    const result = store.respondPendingDecision(installation.id, gatewayId, id, answer, questionId);
    if (result.outcome === 'unknown') return sendJson(response, 404, { error: 'unknown_decision' });
    // Distinct from 404: the decision is live but the sender addressed a qid
    // it does not contain — the request is malformed, not the decision gone.
    if (result.outcome === 'invalid_question') return sendJson(response, 400, { error: 'invalid_question_id' });
    // Distinct device-facing outcomes: a duplicate qid settles only that
    // question as answered elsewhere (409), while a RELEASED decision means
    // Hermes resolved through another surface and the whole pushed card must
    // be torn down (410). Collapsing them would either strand open qids or
    // clear a card that still has answerable questions.
    if (result.outcome === 'released') return sendJson(response, 410, { error: 'decision_released' });
    if (result.outcome === 'already_answered') return sendJson(response, 409, { error: 'already_answered' });
    const payload = { status: 'answered' };
    if (Array.isArray(result.remaining)) payload.remaining = result.remaining;
    return sendJson(response, 200, payload);
  }
  if (decisionMatch && decisionMatch[2] === undefined && request.method === 'GET') {
    // The gateway polls for the answer while its clarify middleware blocks.
    const credential = gatewayCredential(request);
    if (!credential || !store.authenticateGateway(credential.installationId, credential.gatewayId, credential.secret)) return sendJson(response, 401, { error: 'unauthorized' });
    enforceRateLimit(`decision-poll:${credential.gatewayId}`, 120, 60_000);
    const status = store.pendingDecisionStatus(credential.installationId, credential.gatewayId, decisionMatch[1]);
    return sendJson(response, 200, status);
  }
  if (decisionMatch && decisionMatch[2] === undefined && request.method === 'DELETE') {
    // The plugin releases a parked decision when the original clarify path
    // won the race or its poll budget fell back to it: a late device answer
    // must then be rejected (410 decision_released) instead of parking a
    // result nobody reads.
    const credential = gatewayCredential(request);
    if (!credential || !store.authenticateGateway(credential.installationId, credential.gatewayId, credential.secret)) return sendJson(response, 401, { error: 'unauthorized' });
    // Release has its OWN bucket: heavy clarify polling must never be able
    // to starve the DELETE that cleans up when the native path wins.
    enforceRateLimit(`decision-cancel:${credential.gatewayId}`, 30, 60_000);
    const beforeCancel = store.pendingDecisionStatus(credential.installationId, credential.gatewayId, decisionMatch[1]);
    if (beforeCancel.status === 'pending') enforceDecisionBudget();
    const outcome = store.cancelPendingDecision(credential.installationId, credential.gatewayId, decisionMatch[1]);
    if (outcome === 'unknown') return sendJson(response, 404, { error: 'unknown_decision' });
    // Diagnostic distinction only (same 200, the app never parses this
    // body): an already-completed decision was never released — it was
    // answered or completed on its own — and gateway logs should be able to
    // tell that apart from a live release.
    if (outcome === 'answered') return sendJson(response, 200, { status: 'already_completed' });
    return sendJson(response, 200, { status: 'cancelled' });
  }

  if (request.method === 'DELETE' && url.pathname === '/v1/gateways/current') {
    const credential = gatewayCredential(request);
    if (!credential || !store.authenticateGateway(credential.installationId, credential.gatewayId, credential.secret)) return sendJson(response, 401, { error: 'unauthorized' });
    enforceRevocationBudget();
    store.removeGateway(credential.installationId, credential.gatewayId);
    response.writeHead(204).end();
    return;
  }

  sendJson(response, 404, { error: 'not_found' });
}

// True when `proof` is the nonce last pushed to `deviceToken`. Without one,
// a relay in `optional` mode (the rollout window for phones that predate
// token proof) still accepts the token unproven; `required` refuses it so
// the phone asks for a challenge. A wrong or expired proof always fails.
function proveDeviceToken(deviceToken, proof) {
  if (proof === undefined || proof === null) {
    if (config.tokenProof === 'required') throw httpError(403, 'token_proof_required');
    return false;
  }
  if (!tokenChallenges.verify(deviceToken, proof)) throw httpError(403, 'token_proof_invalid');
  return true;
}

// The same phone's earlier installations for a token it just proved, with
// their live turns.
function retireOtherInstallations(deviceToken, exceptId) {
  for (const id of store.retireOtherInstallationsWithDeviceToken(deviceToken, exceptId)) releaseInstallation(id);
}

// What a challenge or registration is for: an FCM token, and the package
// name of an app this relay serves (FCM_PACKAGES). `platform` may be left
// out; when given, it must be `fcm`.
function registrationTarget(body) {
  if (body.platform !== undefined && body.platform !== 'fcm') throw httpError(400, 'invalid_platform');
  const deviceToken = validateFcmToken(body.device_token);
  if (!config.fcm.packages.has(body.bundle_id)) throw httpError(400, 'invalid_topic');
  return { bundleId: body.bundle_id, deviceToken };
}

// A push to the phone's own token. `target` is the installation, or a
// registration not yet stored: its package name restricts the message.
function deviceSend(target, deviceToken, push) {
  return fcmSend(deviceToken, fcmMessage(push, target.bundleId));
}

// Google says this device token will never work again.
function deviceTokenRefused(result) {
  return result.tokenGone === true;
}

// A deactivated installation's credentials stop authenticating, and what
// they already hold open ends with them.
function deactivateInstallation(id) {
  store.deactivateInstallation(id);
  releaseInstallation(id);
}

// A refused device token deactivates an installation only if it still owns
// the token the refused push was sent to: one the phone registered while
// the push was in flight stays, along with the activation and everything it
// holds open.
function deactivateInstallationForToken(installation, sentToken) {
  if (!store.deactivateInstallationForToken(installation.id, sentToken)) return;
  releaseInstallation(installation.id);
}

// Its live turns.
function releaseInstallation(id) {
  liveActivities?.removeInstallation(id);
}

// The authenticated gateway's ids, or null.
function authenticatedGateway(request) {
  const credential = gatewayCredential(request);
  if (!credential || !store.authenticateGateway(credential.installationId, credential.gatewayId, credential.secret)) return null;
  return { installationId: credential.installationId, gatewayId: credential.gatewayId };
}

function authorize(request, id, scope) {
  const credential = bearerCredential(request);
  if (!credential || credential.id !== id) return null;
  return store.authenticate(id, credential.secret, scope);
}

function bearerCredential(request) {
  const match = String(request.headers.authorization ?? '').match(/^Bearer\s+([0-9a-f-]+)\.([A-Za-z0-9_-]+)$/i);
  return match ? { id: match[1], secret: match[2] } : null;
}

function gatewayCredential(request) {
  const match = String(request.headers.authorization ?? '').match(/^Bearer\s+([0-9a-f-]+)\.([0-9a-f-]+)\.([A-Za-z0-9_-]+)$/i);
  return match ? { installationId: match[1], gatewayId: match[2], secret: match[3] } : null;
}

// `gateway` is the AUTHENTICATED gateway record (store.mjs). Its
// dashboardId — bound at pairing/claim time — is the only source of the
// outgoing dashboard identity; the event body is never consulted (#148).
function notificationFor(event, preferences, gateway = undefined) {
  // load()/claimPairing guarantee a valid canonical binding or its
  // absence; nullish coalescing keeps an empty-string persisted value from
  // being silently conflated with "unbound" once validation guarantees hold.
  const dashboardId = gateway?.dashboardId ?? undefined;
  // The authenticated gateway's relay id — the discriminator a device
  // echoes back when answering a parked decision. Derived from the
  // authenticated gateway record, never from the plugin event body.
  const gatewayId = gateway?.id || undefined;
  // Notification-scope token: same gateway + seed → same token; a DIFFERENT
  // gateway with an identical session id → a different token, so identical
  // dashboards never group each other's notifications. Hashed and bounded.
  const scopeToken = (seed) =>
    gatewayId ? createHash('sha256').update(`${gatewayId}:${seed}`).digest('hex').slice(0, 16) : undefined;
  if (event.e2e) return encryptedNotificationFor(event, preferences, { gatewayId, dashboardId, scopeToken });
  const generic = genericCopy(event.type);
  const title = preferences.show_previews && event.title ? event.title : generic.title;
  // Keep previews private by default, while still making notifications from
  // different profiles and sessions distinguishable.
  const body = preferences.show_previews && event.body ? event.body : notificationContext(generic.body, event);
  // `decision` carries structured approval card content so the app can
  // render an answerable card from the push alone. It has its own
  // `decision_cards` preference (default on, independent of show_previews):
  // previews only control banner text, while this controls answerability.
  // `!== false` keeps installations whose stored preferences predate the
  // key on the default.
  const decision = preferences.decision_cards !== false
    ? validateDecision(event.decision, event.type)
    : undefined;
  const routing = {
    type: event.type,
    session_id: event.sessionId,
    profile: event.profile,
    gateway: event.gateway,
    ...(gatewayId ? { gateway_id: gatewayId } : {}),
    ...(dashboardId ? { dashboard_id: dashboardId } : {}),
  };
  // The job's title is chat content: like the banner text, it rides only
  // with previews on (the app falls back to the chat's own title).
  const call = event.type === 'call.requested' && event.call
    ? (preferences.show_previews ? event.call : (({ title: _title, reason: _reason, ...rest }) => rest)(event.call))
    : undefined;
  // Thread grouping is gateway-scoped: two dashboards both using session
  // "default" must never share a thread. Gateway-less callers (direct/test
  // use) keep the raw-session shape.
  const thread = gatewayId ? scopeToken(event.sessionId ?? 'hermes') : (event.sessionId ?? 'hermes');
  const alert = alertData(event.type, title, body, chimes(event.type, preferences), thread);
  const data = { ...alert, [ROUTING_KEY]: JSON.stringify({ ...routing, ...(decision ? { decision } : {}), ...(call ? { call } : {}) }) };
  // FCM refuses a message whose data passes 4 KB. One that would loses its
  // decision card or call, never the notification: the routing alone still
  // opens the chat.
  if ((decision || call) && dataBytes(data) > FCM_MAX_DATA_BYTES) {
    return { data: { ...alert, [ROUTING_KEY]: JSON.stringify(routing) }, urgent: true };
  }
  return { data, urgent: true };
}

// An accepted event that isn't a clarify decision, sent to the phone. Every
// outcome is written to `response`, or thrown as a refusal (an hourly
// limit). The caller keeps the event id for a 2xx or a 4xx a retry can't
// fix, and gives it back for a throw, 408, 429 or 5xx.
async function deliverEvent(response, installation, event, gateway) {
  // A fresh location for Hermes: the phone answers it straight to its host.
  // Never a visible notification, and never paused by notification
  // preferences (the user controls it in the app's location settings).
  if (event.type === 'location.requested') return sendLocationRequest(response, installation, event, gateway);
  if (!shouldDeliver(installation.preferences, event.type)) return sendJson(response, 202, { accepted: true, delivered: false });
  // A change Hermes may make without asking: wake the app silently, never
  // a visible notification.
  if (event.type === 'phone.changes' && event.mode === 'background') return sendPhoneBackground(response, installation, event, gateway);
  const notification = notificationFor(event, installation.preferences, gateway);
  let result;
  // The token this push went to: the device can register a new one meanwhile.
  const sentToken = installation.deviceToken;
  try {
    result = await deviceSend(installation, sentToken, notification);
  } catch (error) {
    // Same contract as the decision path: a THROWN transport failure is
    // reported upstream, never left to surface as a 500. There is no
    // parked decision on this path, so there is nothing to flip.
    console.error(JSON.stringify({ level: 'error', message: 'push send threw', error: error instanceof Error ? error.message : String(error) }));
    return sendJson(response, 502, { error: 'push_unreachable' });
  }
  if (deviceTokenRefused(result)) deactivateInstallationForToken(installation, sentToken);
  if (!result.ok) return sendJson(response, 502, { error: 'push_rejected', reason: result.reason, status: result.status });
  return sendJson(response, 202, { accepted: true, delivered: true });
}

// A location request: an urgent data message the app answers in the
// background, straight to its host. It carries the sealed envelope (or,
// unsealed, the request id and profile) and the routing the phone needs to
// pick the host. Nothing to show, and short-lived.
function locationPushFor(event, gateway, { nowSeconds = Math.floor(Date.now() / 1000) } = {}) {
  if (!event.e2e && !event.locate && !event.maps) return null;
  const routing = {
    type: event.type,
    ...(event.e2e ? { e2e: 1 } : { profile: event.profile, ...(event.locate ? { locate: event.locate } : {}), ...(event.maps ? { maps: event.maps } : {}) }),
    ...(gateway?.id ? { gateway_id: gateway.id } : {}),
    ...(gateway?.dashboardId ? { dashboard_id: gateway.dashboardId } : {}),
  };
  const data = { [ROUTING_KEY]: JSON.stringify(routing), ...(event.e2e ? { [E2E_KEY]: JSON.stringify(event.e2e) } : {}) };
  if (dataBytes(data) > FCM_MAX_DATA_BYTES) return null;
  return { data, urgent: true, expiration: nowSeconds + LOCATION_PUSH_TTL_S };
}

async function sendLocationRequest(response, installation, event, gateway) {
  // The plugin keeps its own budget; this is the relay's ceiling per phone.
  enforceRateLimit(`location:${installation.id}`, LOCATION_PUSH_PER_HOUR, 60 * 60_000);
  const push = locationPushFor(event, gateway);
  if (!push) return sendJson(response, 400, { error: 'invalid_location_request' });
  // The token this push went to: the device can register a new one meanwhile.
  const sentToken = installation.deviceToken;
  let result;
  try {
    result = await deviceSend(installation, sentToken, push);
  } catch (error) {
    console.error(JSON.stringify({ level: 'error', message: 'location send threw', error: error instanceof Error ? error.message : String(error) }));
    return sendJson(response, 502, { error: 'push_unreachable' });
  }
  if (result.ok) return sendJson(response, 202, { accepted: true, delivered: true });
  console.warn(JSON.stringify({ level: 'warn', message: 'location send refused', status: result.status, reason: result.reason }));
  if (deviceTokenRefused(result)) deactivateInstallationForToken(installation, sentToken);
  return sendJson(response, 502, { error: 'push_rejected', reason: result.reason, status: result.status });
}

// A calendar change Hermes may make without asking, as a silent push: the
// app wakes, runs its sealed exchange with its host and applies it, then
// posts its own notification with Undo. It carries the sealed envelope (or,
// unsealed, the profile) and the routing the phone needs to pick the host;
// `mode` says it's silent, since a data message has no push type to say so.
// Not urgent: it may wait for the phone's next wake-up.
function phoneBackgroundPushFor(event, gateway, { nowSeconds = Math.floor(Date.now() / 1000) } = {}) {
  const routing = {
    type: event.type,
    mode: 'background',
    ...(gateway?.id ? { gateway_id: gateway.id } : {}),
    ...(gateway?.dashboardId ? { dashboard_id: gateway.dashboardId } : {}),
    ...(!event.e2e && event.profile ? { profile: event.profile } : {}),
  };
  let data = { [ROUTING_KEY]: JSON.stringify(routing), ...(event.e2e ? { [E2E_KEY]: JSON.stringify(event.e2e) } : {}) };
  // The phone pulls the change from its host either way: an envelope that
  // won't fit is dropped, never truncated.
  if (dataBytes(data) > FCM_MAX_DATA_BYTES) data = { [ROUTING_KEY]: JSON.stringify(routing) };
  return { data, urgent: false, expiration: nowSeconds + PHONE_BACKGROUND_TTL_S };
}

async function sendPhoneBackground(response, installation, event, gateway) {
  enforceRateLimit(`phone-background:${installation.id}`, PHONE_BACKGROUND_PER_HOUR, 60 * 60_000);
  const push = phoneBackgroundPushFor(event, gateway);
  let result;
  // The token this push went to: the device can register a new one meanwhile.
  const sentToken = installation.deviceToken;
  try {
    result = await deviceSend(installation, sentToken, push);
  } catch (error) {
    console.error(JSON.stringify({ level: 'error', message: 'background send threw', error: error instanceof Error ? error.message : String(error) }));
    return sendJson(response, 502, { error: 'push_unreachable' });
  }
  if (deviceTokenRefused(result)) deactivateInstallationForToken(installation, sentToken);
  if (!result.ok) return sendJson(response, 502, { error: 'push_rejected', reason: result.reason, status: result.status });
  return sendJson(response, 202, { accepted: true, delivered: true, background: true });
}

// An end-to-end encrypted event: the relay can't read the content, so the
// alert is the generic copy for the type (no profile or session context),
// which the app replaces with what it decrypts. The envelope rides along
// untouched. Threading uses the plugin's keyed thread token, scoped by
// gateway like the plaintext path.
function encryptedNotificationFor(event, preferences, { gatewayId, dashboardId, scopeToken }) {
  const generic = genericCopy(event.type);
  const thread = scopeToken(event.e2e.tok) ?? event.e2e.tok;
  const routing = JSON.stringify({
    type: event.type,
    e2e: 1,
    ...(gatewayId ? { gateway_id: gatewayId } : {}),
    ...(dashboardId ? { dashboard_id: dashboardId } : {}),
  });
  const alert = alertData(event.type, generic.title, generic.body, chimes(event.type, preferences), thread);
  const data = { ...alert, [ROUTING_KEY]: routing, [E2E_KEY]: JSON.stringify(event.e2e) };
  // Last guard: an envelope that would take the data past FCM's cap is
  // dropped, never truncated. The phone shows the generic alert and a
  // clarify card is parked undeliverable.
  if (dataBytes(data) > FCM_MAX_DATA_BYTES) return { data: { ...alert, [ROUTING_KEY]: routing }, urgent: true };
  return { data, urgent: true };
}

// The fields every notification carries: the alert copy, whether it chimes,
// the category the app shows it in, and the thread it groups by.
function alertData(type, title, body, sound, thread) {
  return {
    title,
    body,
    ...(sound ? { sound: '1' } : {}),
    ...(type === 'call.requested' ? { category: CALL_CATEGORY } : {}),
    ...(type === 'phone.changes' ? { category: PHONE_CHANGE_CATEGORY } : {}),
    thread,
  };
}

// Approval, clarify and call pushes wait on the user, so they chime by
// default: `!== false` keeps installations whose stored preferences predate
// attention_sound on that default. turn.failed is deliberately excluded: it
// reports, it doesn't wait on the user.
function chimes(type, preferences) {
  const completion = type === 'response.ready' || type === 'background_task.finished';
  return Boolean((completion && preferences.completion_sound) || (waitsOnUser(type) && preferences.attention_sound !== false));
}

function notificationContext(message, event) {
  const profile = String(event.profile ?? '').trim();
  const sessionId = String(event.sessionId ?? '').trim();
  const details = [profile && `Profile: ${profile}`, sessionId && `Session: ${shortSessionId(sessionId)}`].filter(Boolean);
  return details.length ? `${message}\n${details.join(' · ')}` : message;
}

function shortSessionId(sessionId) {
  return sessionId.length > 10 ? `…${sessionId.slice(-8)}` : sessionId;
}

function waitsOnUser(type) {
  return type === 'approval.needed' || type === 'input.needed' || type === 'call.requested' || type === 'phone.changes';
}

function genericCopy(type) {
  if (type === 'call.requested') return { title: 'Hermes wants to talk', body: 'Tap to talk to Hermes.' };
  if (type === 'phone.changes') return { title: 'Hermes has a change for you', body: 'Tap to review it in Talaria.' };
  if (type === 'approval.needed') return { title: 'Approval needed', body: 'Hermes is waiting for your approval.' };
  if (type === 'input.needed') return { title: 'Input needed', body: 'Hermes needs your response before it can continue.' };
  if (type === 'turn.failed') return { title: 'Turn failed', body: 'A Hermes turn could not be completed.' };
  if (type === 'background_task.finished') return { title: 'Background task finished', body: 'A delegated task has finished.' };
  return { title: 'Response ready', body: 'Hermes has finished responding.' };
}

function shouldDeliver(preferences, type) {
  if (!preferences.enabled) return false;
  return preferences[type.replace('.', '_')] !== false;
}

function validateEvent(body) {
  const types = new Set(['approval.needed', 'input.needed', 'response.ready', 'turn.failed', 'background_task.finished', 'call.requested', 'location.requested', 'phone.changes', 'plugin.hello']);
  if (!types.has(body.type)) throw httpError(400, 'invalid_event_type');
  const eventId = String(body.event_id ?? '');
  if (!/^[A-Za-z0-9:_-]{8,128}$/.test(eventId)) throw httpError(400, 'invalid_event_id');
  // How to wake the phone for a calendar change: plaintext routing beside
  // any envelope, alert unless said otherwise.
  const mode = body.type === 'phone.changes' ? validatePhoneMode(body.mode) : undefined;
  if (body.e2e !== undefined && body.e2e !== null) {
    // An encrypted event carries no readable content: any plaintext content
    // fields beside the envelope are ignored, never forwarded.
    const e2e = validateEnvelope(body.e2e, eventId);
    return {
      eventId,
      type: body.type,
      pluginVersion: cleanIdentifier(body.plugin_version, 40),
      pluginCapabilities: validCapabilities(body.plugin_capabilities),
      e2e,
      ...(mode ? { mode } : {}),
      // The clarify request id the relay parks must be the one the
      // ciphertext is bound to (the envelope's `req`).
      clarify: body.type === 'input.needed' && e2e.req
        ? validateClarifyRouting(body.clarify, e2e.req)
        : undefined,
    };
  }
  return {
    eventId,
    type: body.type,
    title: cleanText(body.title, 120),
    body: cleanText(body.body, 500),
    sessionId: cleanSessionKey(body.session_id),
    profile: cleanText(body.profile, 80),
    gateway: cleanIdentifier(body.gateway, 80),
    pluginVersion: cleanIdentifier(body.plugin_version, 40),
    pluginCapabilities: validCapabilities(body.plugin_capabilities),
    decision: validateDecision(body.decision, body.type),
    call: body.type === 'call.requested' ? validateCall(body.call) : undefined,
    locate: body.type === 'location.requested' ? validateLocate(body.locate) : undefined,
    maps: body.type === 'location.requested' ? validateMaps(body.maps) : undefined,
    ...(mode ? { mode } : {}),
  };
}

function validatePhoneMode(value) {
  if (value === undefined || value === null) return 'alert';
  if (!PHONE_CHANGE_MODES.has(value)) throw httpError(400, 'invalid_mode');
  return value;
}

// A location request's id, mirroring the plugin's events.push_event.
function validateLocate(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined;
  return typeof value.id === 'string' && /^l_[A-Za-z0-9_-]{16,64}$/.test(value.id) ? { id: value.id } : undefined;
}

// A maps lookup's id (talaria_maps): the phone fetches the lookup from its
// host with it. Routing only, like a locate id.
function validateMaps(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined;
  return typeof value.id === 'string' && /^m_[A-Za-z0-9_-]{16,64}$/.test(value.id) ? { id: value.id } : undefined;
}

// The job a call request is about (#449), mirroring the plugin's
// sanitize_call. A malformed one is dropped: the notification still opens
// the event's session.
function validateCall(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined;
  if (typeof value.id !== 'string' || !/^[A-Za-z0-9_-]{8,64}$/.test(value.id) || !CALL_KINDS.has(value.kind)) return undefined;
  if (!Array.isArray(value.session_ids)) return undefined;
  const sessionIds = [];
  for (const item of value.session_ids) {
    // Like the plugin (events.sanitize_call): one malformed id drops the call.
    const sessionId = cleanSessionKey(item);
    if (!sessionId) return undefined;
    if (!sessionIds.includes(sessionId)) sessionIds.push(sessionId);
  }
  // A call with no session has no chat to open; the plugin never sends one.
  if (!sessionIds.length) return undefined;
  const title = cleanText(value.title, 120);
  const reason = cleanText(value.reason, 200);
  return { id: value.id, kind: value.kind, session_ids: sessionIds.slice(0, 4), ...(title ? { title } : {}), ...(reason ? { reason } : {}) };
}

function validCapabilities(value) {
  return Array.isArray(value)
    ? value.map((capability) => cleanIdentifier(capability, 40)).filter((capability) => capability !== undefined).slice(0, 16)
    : [];
}

// The `e2e` envelope is forwarded byte for byte, so it is validated
// strictly: only the known fields, each in its exact wire shape. A
// malformed envelope is a 400, never a fallback to plaintext.
// Defense in depth against replaying a captured envelope after the event-id
// ledger forgot it: the phone rejects anything older than 24 hours (or more
// than an hour ahead) itself; the relay refuses the same, with an extra hour
// of slack for clocks that disagree.
const E2E_MAX_AGE_S = 25 * 60 * 60;
const E2E_MAX_FUTURE_S = 2 * 60 * 60;

export function validateEnvelope(value, eventId, nowSeconds = Math.floor(Date.now() / 1000)) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw httpError(400, 'invalid_e2e');
  const { v, kid, msg, iat, tok, z, req, n, ct } = value;
  const valid = v === 1
    && typeof kid === 'string' && /^[0-9a-f]{32}$/.test(kid)
    && msg === eventId
    && Number.isSafeInteger(iat) && iat >= nowSeconds - E2E_MAX_AGE_S && iat <= nowSeconds + E2E_MAX_FUTURE_S
    && typeof tok === 'string' && /^[0-9a-f]{16}$/.test(tok)
    && (z === 0 || z === 1)
    && typeof req === 'string' && /^([A-Za-z0-9_-]{4,128})?$/.test(req)
    && typeof n === 'string' && /^[A-Za-z0-9_-]{16}$/.test(n)
    && typeof ct === 'string' && /^[A-Za-z0-9_-]{22,}$/.test(ct) && ct.length <= E2E_MAX_CT_CHARS;
  if (!valid) throw httpError(400, 'invalid_e2e');
  return { v, kid, msg, iat, tok, z, req, n, ct };
}

// Routing metadata for an encrypted clarify: the plugin-minted request id,
// the gateway's question ids (empty for a single question) and whether the
// card fit the envelope. Malformed metadata parks nothing. qids and card are
// routing hints only: a relay that alters them can at most make a question
// unanswerable, since each sealed answer is bound to its request and qid.
function validateClarifyRouting(value, boundRequestId) {
  if (!value || typeof value !== 'object') return undefined;
  if (value.request_id !== boundRequestId) throw httpError(400, 'invalid_e2e');
  const requestId = boundRequestId;
  const qids = [];
  for (const qid of Array.isArray(value.qids) ? value.qids : []) {
    if (typeof qid !== 'string' || !/^[A-Za-z0-9_-]{1,40}$/.test(qid)) continue;
    if (['__proto__', 'constructor', 'prototype'].includes(qid) || qids.includes(qid)) continue;
    qids.push(qid);
  }
  return { request_id: requestId, qids: qids.slice(0, 8), card: value.card !== false };
}

// Mirror the plugin's decision sanitization at the relay trust boundary.
// Two contracts, each bound to its event type:
// - approval: session-keyed, answered via the gateway's approval.respond;
//   choices whitelisted to the gateway's approval vocabulary.
// - clarify: keyed by a plugin-minted request id (the gateway's own clarify
//   id is unreachable to plugins), answered via this relay's /v1/decisions
//   endpoints. Choices are model-generated labels, so they are bounded but
//   deliberately not vocabulary-whitelisted.
// Anything malformed degrades to undefined and the notification ships as a
// routing stub.
function validateDecision(value, eventType) {
  if (!value || typeof value !== 'object') return undefined;
  const kind = cleanText(value.kind, 40);
  if (kind === 'approval' && eventType === 'approval.needed') {
    const sessionKey = cleanSessionKey(value.session_key);
    const description = cleanText(value.description, 500);
    if (!sessionKey || !description) return undefined;
    const allowed = new Set(['once', 'session', 'always', 'deny']);
    const choices = [...new Set(
      (Array.isArray(value.choices) ? value.choices : [])
        .map((choice) => cleanText(choice, 80))
        .filter((choice) => allowed.has(choice)),
    )];
    if (!choices.length) return undefined;
    return { kind: 'approval', session_key: sessionKey, description, choices };
  }
  if (kind === 'clarify' && eventType === 'input.needed') {
    const requestId = typeof value.request_id === 'string' && /^[A-Za-z0-9_-]{4,128}$/.test(value.request_id)
      ? value.request_id
      : undefined;
    const question = cleanText(value.question, 500);
    if (!requestId || !question) return undefined;
    const choices = (Array.isArray(value.choices) ? value.choices : [])
      .map((choice) => cleanText(choice, 80))
      .filter((choice) => choice !== undefined)
      .slice(0, 8);
    // Batch decisions (plugin 0.3+) carry the FULL question set. It MUST
    // survive this boundary through the same shared sanitizer the
    // pending-decision store uses — dropping it here would silently reduce
    // every pushed batch to its collapsed first question. Deduplication and
    // bounds are inside the shared sanitizer.
    const questions = sanitizeBatchQuestions(value.questions);
    return {
      kind: 'clarify',
      request_id: requestId,
      question,
      ...(choices.length ? { choices } : {}),
      ...(questions.length ? { questions } : {}),
    };
  }
  return undefined;
}

// A clarify answer, kept whole or refused, never cut: a cut sealed answer
// no longer opens, and a cut plaintext one isn't what the user wrote.
// - An encrypted answer is opaque base64url, kept whole (never trimmed) and
//   only for the strict wire shape. One with the sealed header that is too
//   long is `413 answer_too_large`; one whose ciphertext is too short to
//   hold its tag is `400 invalid_answer`.
// - Anything else, including plaintext that merely starts like one, is an
//   ordinary answer (the plugin of an encrypted pairing rejects it): control
//   characters become spaces and it is trimmed; over MAX_ANSWER_CHARS it is
//   `413 answer_too_large`.
function validAnswer(value) {
  if (typeof value !== 'string') throw httpError(400, 'invalid_answer');
  if (E2E_ANSWER_SHAPE.test(value)) {
    if (E2E_ANSWER_PATTERN.test(value) && value.length <= E2E_MAX_ANSWER_CHARS) return value;
    // Only the ciphertext's length can fail the pattern here.
    const tooShort = value.length - value.lastIndexOf('.') - 1 < 22;
    throw tooShort ? httpError(400, 'invalid_answer') : httpError(413, 'answer_too_large');
  }
  const answer = value.replace(/[\u0000-\u001f]/g, ' ').trim();
  if (!answer) throw httpError(400, 'invalid_answer');
  if (answer.length > MAX_ANSWER_CHARS) throw httpError(413, 'answer_too_large');
  return answer;
}

function cleanText(value, max) {
  return typeof value === 'string' ? value.replace(/[\u0000-\u001f]/g, ' ').trim().slice(0, max) || undefined : undefined;
}

function cleanIdentifier(value, max) {
  return typeof value === 'string' && /^[A-Za-z0-9:_.\/-]+$/.test(value) ? value.slice(0, max) : undefined;
}

// A session key, whole, or undefined (see SESSION_KEY_MAX_BYTES).
function cleanSessionKey(value) {
  return typeof value === 'string' && value && value.isWellFormed() && !/[\u0000-\u001f\u007f]/.test(value)
    && Buffer.byteLength(value) <= SESSION_KEY_MAX_BYTES ? value : undefined;
}

function readJson(request, maxChars = 32_768) {
  return new Promise((resolve, reject) => {
    let data = '';
    request.setEncoding('utf8');
    request.on('data', (chunk) => {
      data += chunk;
      if (data.length > maxChars) { reject(httpError(413, 'payload_too_large')); request.destroy(); }
    });
    request.on('end', () => {
      if (!data) { resolve({}); return; }
      let parsed;
      try { parsed = JSON.parse(data); } catch { reject(httpError(400, 'invalid_json')); return; }
      // Deliberate contract: every route reads named fields, so ONLY a JSON
      // object is a valid body. A top-level null/array/string/number is a
      // client mistake — rejected as invalid_json rather than silently
      // normalized to {} (which would turn a malformed request into a
      // successful no-field operation).
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        reject(httpError(400, 'invalid_json'));
        return;
      }
      resolve(parsed);
    });
    request.on('error', reject);
  });
}

function sendJson(response, status, body) {
  if (response.headersSent) return;
  const encoded = JSON.stringify(body);
  response.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'content-length': Buffer.byteLength(encoded), 'cache-control': 'no-store', 'x-content-type-options': 'nosniff' });
  response.end(encoded);
}

let lastLimitsSweepAt = 0;
// Past this many buckets, a key without one is refused until a sweep frees
// room, so a flood of rotating keys can't grow the map without bound.
const MAX_LIMIT_BUCKETS = 100_000;

function enforceRateLimit(key, maximum, windowMs) {
  const now = Date.now();
  // The map never evicts on its own and some keys (per-IP buckets) are
  // attacker-rotatable. Sweep expired entries once it grows large, but at
  // most once per 30s (5s when full) so a sustained flood cannot turn every
  // request into a full-map scan.
  const sweepEveryMs = limits.size >= MAX_LIMIT_BUCKETS ? 5_000 : 30_000;
  if (limits.size > 10_000 && now - lastLimitsSweepAt > sweepEveryMs) {
    lastLimitsSweepAt = now;
    for (const [limitKey, limit] of limits) {
      if (limit.resetAt <= now) limits.delete(limitKey);
    }
  }
  const value = limits.get(key);
  if (!value && limits.size >= MAX_LIMIT_BUCKETS) {
    const full = httpError(429, 'rate_limited');
    full.limitsFull = true;
    throw full;
  }
  if (!value || value.resetAt <= now) { limits.set(key, { count: 1, resetAt: now + windowMs }); return; }
  value.count += 1;
  if (value.count > maximum) throw httpError(429, 'rate_limited');
}

// Process-wide admission budgets reserve capacity for each class of action,
// so a flood of one kind cannot starve the others. Sizes live in limits.mjs.
function enforceRegistrationBudget() {
  enforceRateLimit('registration-global', config.budgets.registrationsPerMinute, 60_000);
}

function enforceEventBudget() {
  enforceRateLimit('event-global', config.budgets.eventsPerMinute, 60_000);
}

function enforceDeviceChangeBudget() {
  enforceRateLimit('device-change-global', config.budgets.deviceChangesPerMinute, 60_000);
}

function enforceDecisionBudget() {
  enforceRateLimit('decision-global', config.budgets.decisionActionsPerMinute, 60_000);
}

// Each host holds its user to their own call limits; this backstops one that
// doesn't, per installation over 24 hours.
function enforceCallCeiling(installationId) {
  try {
    enforceRateLimit(`call:${installationId}`, config.callLimits.callsPerInstallationPerDay, CALL_WINDOW_MS);
  } catch (error) {
    if (error?.status === 429 && !error.limitsFull) throw httpError(429, 'call_limit');
    throw error;
  }
}

function enforceRevocationBudget() {
  enforceRateLimit('revocation-global', config.budgets.revocationsPerMinute, 60_000);
}

function clientAddress(request) {
  // Only the entries the trusted proxies appended count: earlier ones are
  // whatever the client sent, so taking the first would let anyone pick
  // their own rate-limit bucket.
  if (config.proxyHops > 0) {
    const forwarded = String(request.headers['x-forwarded-for'] ?? '').split(',').map((entry) => entry.trim());
    const address = forwarded.length >= config.proxyHops ? forwarded[forwarded.length - config.proxyHops] : '';
    if (isIP(address)) return address;
  }
  return request.socket.remoteAddress ?? 'unknown';
}

function httpError(status, code) {
  const error = new Error(code);
  error.status = status;
  return error;
}

function safePath(value) {
  try { return new URL(value ?? '/', config.publicUrl).pathname; } catch { return '/invalid'; }
}

function readConfig() {
  for (const name of ['PUBLIC_URL', 'DATA_PATH']) if (!process.env[name]) throw new Error(`${name} is required.`);
  const publicUrl = new URL(process.env.PUBLIC_URL);
  if (publicUrl.protocol !== 'https:') throw new Error('PUBLIC_URL must use HTTPS.');
  const proxyHops = process.env.TRUST_PROXY && /^\d{1,2}$/.test(process.env.TRUST_PROXY) ? Number(process.env.TRUST_PROXY) : process.env.TRUST_PROXY ? NaN : 0;
  if (!Number.isInteger(proxyHops) || proxyHops > 10) {
    throw new Error('TRUST_PROXY must be the number of reverse proxies in front of the relay (0 to 10).');
  }
  return {
    host: process.env.HOST || '127.0.0.1',
    port: Number(process.env.PORT || 9120),
    publicUrl: publicUrl.toString().replace(/\/$/, ''),
    dataPath: process.env.DATA_PATH,
    // The service account key file and the package names allowed to
    // register (fcm.mjs).
    fcm: fcmConfigFromEnv(process.env),
    // How many reverse proxies sit in front of the relay; each appends the
    // address it saw to X-Forwarded-For.
    proxyHops,
    // `required` (default) or `optional` while phones without token proof
    // still need to register (token-proof.mjs).
    tokenProof: tokenProofModeFromEnv(process.env),
    ...limitsFromEnv(process.env),
  };
}

// Loading the store only reads, so a data directory the relay can't write
// would pass /healthz and fail the first registration instead. A Compose bind
// mount Docker created itself is root's, while the image runs as node.
function assertDataDirectoryWritable(dataPath) {
  const directory = dirname(dataPath);
  const probe = `${dataPath}.${process.pid}.probe`;
  try {
    mkdirSync(directory, { recursive: true, mode: 0o700 });
    writeFileSync(probe, '', { flag: 'wx', mode: 0o600 });
    unlinkSync(probe);
  } catch (error) {
    const uid = process.getuid?.();
    throw new Error(`DATA_PATH directory ${directory} is not writable${uid === undefined ? '' : ` by uid ${uid}`} (${error?.code ?? error}). `
      + 'With the Compose file, create it owned by the image\'s node user first: mkdir -p data && sudo chown 1000:1000 data');
  }
}

export { locationPushFor, notificationFor, phoneBackgroundPushFor, validateEvent, validateDecision };
