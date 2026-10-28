# Talaria relay

The push relay for [Talaria](https://github.com/dawnforge-lab/talaria), an Android app for
[Hermes Agent](https://github.com/NousResearch/hermes-agent). It takes events from a Hermes host
running the Talaria plugin (the rest of [this repository](../README.md)) and delivers them to the
phone through Firebase Cloud Messaging (FCM HTTP v1). The plugin and the relay live together and
change together; the relay runs on its own, usually in Docker, so a Hermes host never runs its code.

```
Hermes (talaria_push plugin) ──POST /v1/events──▶ relay ──FCM──▶ Android phone
```

The relay handles device registration, pairing codes, per-installation preferences, rate limiting
and idempotent event delivery. It uses only the Node.js standard library (no npm dependencies).
Notification content is end-to-end encrypted between the plugin and the phone; the relay sees only
routing data.

It started as the push relay in
[kaishi00/hermes-conduit-notifier](https://github.com/kaishi00/hermes-conduit-notifier) (MIT), built
for the iOS app Hermes Conduit. Talaria is a separate project, not made or supported by the Conduit
author, and this relay now serves Android phones only.

## Running it

You need a Firebase project of your own (the app's `google-services.json` comes from the same
project), Docker, and a public HTTPS address for the relay. The commands below run from this
`relay/` folder.

1. In the Firebase console, open Project settings > Service accounts > Generate new private key,
   and save the key file as `deploy/secrets/fcm/service-account.json`. It stays on the relay host.
2. Set up and start the relay:

   ```shell
   cd deploy
   cp .env.example .env      # set PUBLIC_URL and FCM_PACKAGES
   mkdir -p data             # owned by the image's node user (uid 1000)
   sudo chown 1000:1000 data
   docker compose up -d
   ```

Skip the `chown` and Docker creates `data/` owned by root on a Linux host. The relay runs as
`node`, can't write there, and exits at startup naming the directory.

The relay listens on port 9120. Put it behind an HTTPS reverse proxy (the relay checks that
`PUBLIC_URL` uses HTTPS), for example `tailscale serve` or Cloudflare Tunnel. If the proxy sends
`X-Forwarded-For`, set `TRUST_PROXY` to the number of proxies in front of the relay (`1` for one).
The Compose file publishes the port on 127.0.0.1 only; don't publish it more widely.

Settings:

- `PUBLIC_URL`: the relay's public HTTPS address.
- `FCM_SERVICE_ACCOUNT_PATH`: the service account key file (the Compose file mounts
  `deploy/secrets/fcm/service-account.json` there).
- `FCM_PACKAGES`: the Android package names allowed to register, comma-separated (for example
  `lab.dawnforge.hermes,lab.dawnforge.hermes.debug`).
- `RELAY_TOKEN_PROOF` and the capacity limits below; see
  [`deploy/.env.example`](deploy/.env.example).

`FCM_ORIGIN` and `RELAY_TEST_LOCATION_PUSHES_PER_HOUR` are test settings, refused unless
`RELAY_TEST_SEAMS=1`.

**What stays secret:**

| File | Contents | Gitignored |
|------|----------|------------|
| `deploy/.env` | Public URL and settings | Yes |
| `deploy/secrets/fcm/service-account.json` | Firebase signing key | Yes |
| `deploy/data/relay.json` | Installation records, gateway credentials | Yes |

## What the phone receives

Every push is a data-only FCM message: the app shows the notification itself. The message is
restricted to the registered package, and a Firebase installation id (FID) goes in the message's
`fid` field, an older SDK's registration token in `token`. Every data value is a string:

- `title` and `body`: the generic copy for the event type (the app replaces it with what it
  decrypts), `sound` (`"1"` when it should chime), `thread` and `category`.
- `talaria`: the routing as JSON (event type, gateway, dashboard).
- `talaria_e2e`: the sealed envelope as JSON, untouched.
- `talaria_token_challenge`: a registration nonce (see "Registration proof").

A running turn's progress has its own shape: `category: HERMES_LIVE_TURN`, with `activity`,
`event` (`update` or `end`), `phase`, `tools`, and `tool`, `context`, `stale_at` or `ended_at`
when known.

What the user acts on goes at high priority; a silent calendar change and a turn's routine
progress go at normal priority. FCM caps a message's data at 4 KB: a push that would pass it loses
its sealed envelope or card, never the notification itself.

FCM answers that mean the token is dead (`UNREGISTERED`, `SENDER_ID_MISMATCH`, or
`INVALID_ARGUMENT` naming the token) end the installation. Other failures are answered with
`502 push_rejected` (with Google's `reason` and `status`) or `502 push_unreachable`, and the
plugin retries them.

## Privacy

What the relay keeps, in its data file:

- Each phone's installation: its FCM token, its app's package name, its notification preferences,
  and a hash of its credential.
- Each paired Hermes profile (a gateway): its name, a hash of its credential, the dashboard it
  pairs with, its plugin version, and when it last sent an event.
- Questions waiting for an answer from the phone, for at most two hours. With end-to-end
  encryption (the Talaria app always pairs with it), the relay stores only their ids; answers are
  sealed for the plugin.

What passes through without being kept: the sealed notifications, which the relay can't open, and
a running turn's coarse progress (phase, tool category, counts). Event ids are kept in memory only.

Google sees the same as the relay for each push it delivers. The relay's logs hold request paths,
statuses and timings, never message content or credentials.

## Relay API

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/healthz` | Health check |
| POST | `/v1/token-challenges` | Push a one-time registration nonce to `device_token` (`bundle_id` as for registering) |
| POST | `/v1/installations` | Register a phone: `device_token` (its FCM token), `bundle_id` (its package name), `token_proof`, optional `preferences` and `platform: "fcm"` |
| PUT | `/v1/installations/:id` | Update the device token (a new one needs `token_proof`) or preferences |
| DELETE | `/v1/installations/:id` | Deactivate a phone |
| POST | `/v1/installations/:id/pairings` | Create a pairing code, optionally bound to a `dashboard_id` |
| POST | `/v1/pairings/claim` | Claim a pairing code (gateway side) |
| POST | `/v1/events` | Deliver an event (gateway side) |
| POST | `/v1/decisions/:id/respond` | Answer a question pushed to the phone (`answer`, `gateway_id`, optional `question_id`) |
| GET | `/v1/decisions/:id` | Poll for the answer (gateway side) |
| DELETE | `/v1/decisions/:id` | Release a question Hermes no longer waits on (gateway side) |
| DELETE | `/v1/gateways/current` | Revoke a gateway credential |
| PUT | `/v1/installations/:id/live-activities` | Follow a running turn: `push_token` (a UUID the phone made) and the chat's `session_ids` |
| POST | `/v1/live-activity` | Report a running turn's progress (`session_id`, `state`, optional `end`); answers `tracked` (gateway side) |

## Registration proof

A phone proves it holds the FCM token it registers. It asks `POST /v1/token-challenges` for a
challenge, the relay sends a data message carrying a one-time nonce (`talaria_token_challenge`) to
that token, and the phone sends the nonce back as `token_proof` with `POST /v1/installations`, or
with a `PUT` that moves the installation to a new device token. A nonce lasts 10 minutes and works
once. Without a proof the relay answers `403 token_proof_required`; a wrong or expired one gets
`token_proof_invalid`.

So a token that leaks (a data-file backup, a log) can't be bound to someone else's installation,
and filling the relay's installation slots takes real phones running the app rather than a script.
A proven registration retires the same phone's earlier installations for that token.

A challenge answers `400 invalid_device_token` when Google says the token is dead, so it shows
whether a token is currently live for the app. The nonce never reaches whoever asked, so that
grants nothing else, and per-token, per-IP and relay-wide limits bound such probing.

`RELAY_TOKEN_PROOF` is `required` by default. `optional` still accepts registrations and token
changes without a proof (a proof that is sent must be valid): use it only while app builds from
before token proof still need to register. Installations registered before token proof keep
working either way; they need a proof only to move to a new device token.

## Storage

Check retained installation capacity without changing the data file. From the repository root,
pass the mounted JSON path directly:

```shell
node src/storage-status.mjs deploy/data/relay.json
```

Or, from `deploy`, inspect the running Compose service:

```shell
docker compose exec relay npm run --silent storage:status
```

The command prints only total, active, inactive, and maximum installation counts. Inactive
installations remain in the store and still consume slots: tokens Google calls dead and repeated
uninstall/reinstall cycles count toward the installation limit (50,000 by default,
`RELAY_MAX_INSTALLATIONS`), which the printed maximum reflects. `DELETE /v1/installations/:id`
deactivates an installation and does not free its slot right away. When the relay is full, a new
registration takes the slot of the longest-inactive installation (with its pairing codes and
parked decisions): an inactive installation can never be used again, since its credential no
longer authenticates and its hosts' events are refused. Only a relay full of active installations
refuses new ones.

For offline reclamation, `storage:prune-inactive` previews the number of eligible records by
default. It removes only installations that have been inactive and unchanged for at least 30 days,
have no gateway credentials, and have no pairing or pending-decision references. Active, recent,
gateway-bound, and referenced records are preserved; other store sections are left unchanged.
Malformed state causes a generic failure without writes.

From `deploy`, stop every relay writer before previewing or applying:

```shell
docker compose stop relay
docker compose run --rm --no-deps relay npm run --silent storage:prune-inactive
```

Review the counts, then explicitly apply and restart:

```shell
docker compose run --rm --no-deps relay npm run --silent storage:prune-inactive -- --apply --relay-stopped
docker compose start relay
```

The command creates an exclusive, exact-byte `relay.json.backup-*` beside the data file before
atomically replacing it. Backups contain credentials: protect them like the original data file.
The `--relay-stopped` flag acknowledges the offline requirement; it does not stop running writers.
A byte comparison detects changes before replacement, but cannot make concurrent maintenance safe.
Preview and zero-eligible runs create no backup and change no files. For a direct Node invocation,
use `node src/storage-prune-inactive.mjs PATH_TO_RELAY_JSON` and the same explicit flags when
applying. Reclamation is operator-run, never automatic; gateway-bound inactive records still
require separately reviewed maintenance.

## Capacity and admission limits

Each bound is a backstop against floods, not a quota ordinary traffic reaches. Every one can be
changed with the environment variable next to it: unset or empty keeps the default, and anything
other than a positive integer stops the relay at boot.

| Bound | Default | Variable |
|-------|---------|----------|
| Installations, inactive ones included | 50,000 | `RELAY_MAX_INSTALLATIONS` |
| Gateways per installation | 64 | `RELAY_MAX_GATEWAYS_PER_INSTALLATION` |
| Event IDs remembered per installation | 5,000 | `RELAY_MAX_EVENT_IDS_PER_INSTALLATION` |
| Event IDs remembered across the relay | 250,000 | `RELAY_MAX_EVENT_IDS` |
| Active clarify decisions across the relay | 1,024 | `RELAY_MAX_ACTIVE_DECISIONS` |
| Retained clarify decisions across the relay | 4,096 | `RELAY_MAX_RETAINED_DECISIONS` |
| Registrations per minute | 24 | `RELAY_REGISTRATIONS_PER_MINUTE` |
| Registration challenges per minute (each is a push; also 5 per device token per 10 minutes and 12 per IP address per minute, fixed) | 240 | `RELAY_TOKEN_CHALLENGES_PER_MINUTE` |
| Events per minute | 6,000 | `RELAY_EVENTS_PER_MINUTE` |
| Device updates that change state, pairing creations, and valid claims per minute | 240 | `RELAY_DEVICE_CHANGES_PER_MINUTE` |
| Decision answers and releases that change state per minute | 600 | `RELAY_DECISION_ACTIONS_PER_MINUTE` |
| Installation deactivations and gateway revocations per minute | 24 | `RELAY_REVOCATIONS_PER_MINUTE` |
| Hermes call requests per installation per day (each new request counts, delivered or not) | 60 | `RELAY_CALLS_PER_INSTALLATION_PER_DAY` |
| Followed turns held in memory, relay-wide / per phone (the oldest is forgotten past either) | 50,000 / 8 | `RELAY_MAX_LIVE_ACTIVITIES` / `RELAY_MAX_LIVE_ACTIVITIES_PER_INSTALLATION` |
| Followed-turn registrations per minute, relay-wide | 3,000 | `RELAY_LIVE_ACTIVITY_REGISTRATIONS_PER_MINUTE` |
| Progress reports for a followed turn per minute, relay-wide | 30,000 | `RELAY_LIVE_ACTIVITY_UPDATES_PER_MINUTE` |
| Followed-turn registrations per phone / per IP address, per minute (fixed) | 60 / 120 | |

Event IDs only prevent a repeated delivery of the same event within 24 hours, so the relay keeps
them in memory and never writes them to the data file. A restart forgets them, which costs at most
one duplicate push; replaying an event after a restart needs that gateway's credential, which can
send new events anyway. An event whose push fails in a way a retry can fix (Google refused or
unreachable, an hourly limit) gives its ID back, so the plugin's retry is sent; a push Google took
but whose answer was lost can therefore show twice. A clarify decision is the exception: it keeps
its ID, because a retry can't re-park it, and the plugin falls back to Hermes' own clarify path. An
installation at its event-ID bound forgets its own oldest ID, and at the relay-wide bound the
installation holding the most IDs forgets its oldest, so neither bound rejects an event. A
gateway's last-seen time and plugin version are written at most every five seconds, and on
shutdown, instead of on every event.

Installations, gateways, and clarify decisions are persistent. A new record is rejected with HTTP
429 when its bound is full; the relay never evicts existing credentials. A store already above a
limit when upgraded keeps every record, but cannot add records of that kind until the operator
raises the limit or removes records while the relay is stopped. Re-pairing a profile adds a gateway
without revoking the previous one, which is why the gateway bound is generous.

The per-minute budgets are process-wide and separate for each class of action, so a flood of one
kind cannot starve the others. Validation, capacity rejection, and per-client limits run before a
budget is charged. Unchanged device updates, already-accepted events without plugin metadata,
non-mutating decision responses, and repeated cancellations use no budget and do not rewrite the
store; health checks and decision polling use none either. The per-installation limits of 30
events and 30 device updates per minute still apply.

Run exactly one relay process per `DATA_PATH`. The relay caches its state and rewrites the whole
JSON file on every persistent change; atomic replacement does not coordinate multiple writers, and
sharing the file between processes can lose updates.

## Tests

```shell
npm test
```

See [CONTRIBUTING.md](../CONTRIBUTING.md) before opening a pull request.

The end-to-end tests run real relay processes against a fake Google (`testing/harness.mjs`).

## License

MIT, as the project it started from. See [LICENSE](../LICENSE).
