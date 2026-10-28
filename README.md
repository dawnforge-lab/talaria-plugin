# Talaria plugin

The Talaria plugin (`talaria_push`) is a plugin for
[Hermes Agent](https://github.com/NousResearch/hermes-agent) that connects a
Hermes host to [Talaria](https://github.com/dawnforge-lab/talaria), an Android
app for Hermes. It sends Hermes' notifications and calls to the phone, keeps
the data the phone shares with this host (health, location, calendar,
reminders, phone status, contacts), gives the agent tools to use that data,
and serves the dashboard routes Talaria calls. It needs the Talaria app on
an Android phone and a Talaria relay, which delivers pushes to the phone
through Firebase Cloud Messaging (FCM). The relay is in this repository too,
in [`relay/`](relay/README.md); it runs on its own, not inside Hermes.

Every route sits behind the Hermes dashboard's own auth. The plugin holds no
push service key and no dashboard credentials: the Firebase key stays on the
relay, and provider keys and sign-ins never leave this host. What each party
can see is under [Privacy](#privacy).

## Requirements

- Hermes Agent on Linux, macOS or Windows, with the Hermes dashboard
  running. Talaria talks to this host through the dashboard, so the phone
  must reach it: use HTTPS or a private tunnel (Tailscale, for example).
- The Talaria app on an Android phone.
- A Talaria relay. The relay is a separate server; see
  [Running your own relay](#running-your-own-relay).
- Python's `cryptography` package for end-to-end encryption. Hermes already
  depends on it.
- Optional: a Google Maps Platform key for the maps tool (see
  [Google Maps on the host](#google-maps-on-the-host)).

Some features use newer Hermes hooks (`register_middleware`,
`post_approval_response`, `pre_api_request`, `pre_llm_call`, unload
callbacks). On a Hermes without one, the plugin logs a warning and the rest
keeps working.

## Install

Run these commands on the machine where Hermes is installed:

```bash
hermes plugins install dawnforge-lab/talaria-plugin --enable
hermes gateway restart
```

Restart the Hermes dashboard as well: it imports the plugin's routes when
it starts.

## Pairing from Talaria

Pairing links one Hermes profile to one phone through the relay. It creates
a revocable credential for that profile, stored in the profile's Hermes home
as `talaria-push.json` (mode `0600` where the system supports it).

Talaria pairs the dashboard's profile by itself. It creates a pairing code at
its relay and hands it to this host over your signed-in dashboard
connection:

| Method | Route | Body | Returns |
| --- | --- | --- | --- |
| POST | `/api/plugins/talaria_push/pair?profile=` | `{"pairing_code": "ABCDE-FGHJK", "relay_url": "https://relay.example.com"}` | `{ok, paired, installation_id, gateway_id, gateway_name, relay_url}` |

- `relay_url` may be left out when the profile is already paired: its
  current relay is used. With neither, the route answers 400 `relay_url is
  required`. It must be a bare http(s) URL (see
  [Running your own relay](#running-your-own-relay) for which URLs are
  accepted).
- The claim carries only the code and the gateway name and never follows
  redirects. The answer never includes the credential.
- A re-pair keeps the profile's redaction choice. It keeps the encryption
  key when the same phone and gateway pair again, and otherwise drops it
  (Talaria sets up a new one). The credential it replaces is revoked at its
  relay.
- 400 for a malformed code or URL, 410 when the code expired or was used,
  429 past 10 requests a minute, 502 when the relay can't be reached.

To pair by hand instead, create a pairing code in Talaria under
**Settings > Notifications** and claim it from the matching Hermes profile:

```bash
hermes talaria pair XXXXX-XXXXX --relay-url https://relay.example.com
```

Pair other profiles the same way, each with its own code:

```bash
hermes -p coder plugins enable talaria_push
hermes -p coder talaria pair YYYYY-YYYYY --relay-url https://relay.example.com
```

Pairing codes expire after ten minutes and can be claimed only once.

## The `hermes talaria` command

```bash
# Claim a pairing code (--relay-url URL unless the profile is already paired;
# optional: --name "Gateway name")
hermes talaria pair XXXXX-XXXXX --relay-url https://relay.example.com

# Show the pairing without printing the credential: gateway name, relay,
# redaction, end-to-end encryption, and any refusal or block from the relay
hermes talaria status

# Send a test notification through the relay
hermes talaria test

# Send an encrypted test with a broken ciphertext: the phone must show only
# generic text
hermes talaria test --corrupt

# Keep chat text out of pushes, or send it again (see Privacy)
hermes talaria redact on
hermes talaria redact off

# Revoke this profile's relay credential
hermes talaria unpair
```

Each command acts on the active Hermes profile; use `hermes -p <profile>
talaria ...` for another. `status` also says when the relay refused the
pairing's credential (pair again from Talaria) or when something in front of
the relay, such as a firewall or rate limiter, is blocking this host (pairing
again won't help; ask whoever runs the relay).

## Updating

```bash
hermes plugins update talaria_push
hermes gateway restart
```

Then restart the dashboard, which keeps serving the routes it imported at
start. Talaria can do all three from **Settings > Talaria plugin**: it runs
Hermes' own plugin update and gateway restart routes, then asks this plugin
to restart the dashboard:

- `GET /api/plugins/talaria_push/capabilities` reports `version` (on disk),
  `loaded_version` (what this dashboard process imported), `restart_needed`
  (the two differ) and `boot_id` (new with every dashboard process). See
  [Capabilities](#capabilities).
- `POST /api/plugins/talaria_push/self/restart` answers
  `{ok, restarting, boot_id, loaded_version, version}` and a second later
  re-executes the dashboard with the command line it started with, keeping
  its pid, so systemd, launchd and terminal launches carry on. 409 while a
  chat turn this process runs is still going; 501 on Windows, inside a plugin
  host, and for Hermes Desktop's own backend, where you restart Hermes
  yourself.

Every version is listed in [CHANGELOG.md](CHANGELOG.md). Talaria's update
notice reads it. [`channels.yaml`](channels.yaml) sets the lowest version
that release builds of Talaria ask users to update to.

## Notifications

The plugin sends a notification for:

- an approval Hermes needs. The card offers `once` and `deny`; the command
  itself is never sent, since it can carry secrets.
- a clarify question or other input Hermes needs (see
  [Clarify questions](#clarify-questions)).
- a reply, once the turn has ended. A turn that failed sends only its
  failure notice, not Hermes' error text as a reply.
- a failed turn.
- a finished background task. A delegated task that ends while the turn that
  started it is still running is part of that turn's reply and sends nothing
  of its own.
- calendar, reminder and alarm changes Hermes asks your phone to make (see
  [Calendar, reminders and phone status](#calendar-reminders-and-phone-status)):
  a notification to approve, or a silent wake for adds you let Hermes make
  without asking.
- call requests (see [Hermes calls you](#hermes-calls-you)).

An exact `[Silent]` reply sends nothing. Reply and task bodies are plain
text: Markdown fences, emphasis, headings, link targets and table rules are
dropped.

Each notification is titled with the session's title when it has one (for
example a Telegram topic name): a reply shows the title, and the others put
it in front (`Approval needed · Infra`). A Bot Chat, whose title is always
`Bot Chat`, is titled with its bot's profile name instead. Untitled sessions
keep the generic text. With `redact on` or end-to-end encryption, the title
doesn't reach the relay in the clear.

In a gateway-hosted group room, a member's reply or failed turn sends
nothing: the room is where you follow the conversation. An approval or
question the member waits on still notifies, titled with the room's name and
the member.

Which kinds of notification arrive, and whether they make a sound, are set
in Talaria and kept by the relay as the phone's preferences.

### Cron runs

A cron run notifies like any other turn, with two exceptions:

- A run whose reply is Hermes' cron silence marker doesn't notify, by the
  same rule Hermes uses to skip delivering it: `[SILENT]` (or `SILENT` /
  `NO_REPLY`) as the whole reply, on its own first or last line, or opening
  the reply (`[SILENT] No changes since yesterday`). A marker mid-sentence
  is real content and still notifies.
- You can turn a job's notifications off in Talaria's Cron tab. Its runs
  then send no reply or failure notification, and no automatic alert call
  (a call you asked for on the job still rings). The muted job ids live in
  the profile's `talaria-cron.json`.
  `GET /api/plugins/talaria_push/cron/notifications` lists them
  (`{"ok": true, "muted": [...]}`) and `PUT` with
  `{"job_id": "...", "notify": false}` changes one. Capability
  `cron-notifications`.

The plugin tells a cron run by its task id (`cron:<job id>:…`) or its
classic session id (`cron_<job id>_<date>_<time>`).

### Delivery

- Hooks put events on a bounded queue. A background worker sends them over
  HTTPS, so delivery never blocks the agent loop. Titles and bodies are
  length-limited before they go out.
- A delivery that fails on the way (no connection, a timeout, `408`, `429`
  or a `5xx` from the relay) is sent again with the same event id after 1, 2
  and 4 seconds, for at most about 30 seconds per event; the relay drops
  repeats it already accepted. Other refusals are not retried.
- A `401` means the relay no longer knows this gateway (Talaria removed it,
  or the relay lost its data). The pairing is kept, and `status` says to pair
  again from Talaria until a delivery is accepted again.
- A `401` or `403` parks notifications for that pairing: events are dropped
  for 1 minute, doubling with each refusal in a row up to 30 minutes. Then
  the next event goes out as a probe, and an accepted one (or an accepted
  `test`) ends the pause. A clarify question asked meanwhile goes straight
  to Hermes' own prompt.
- A `403` with no JSON body comes from something in front of the relay
  blocking this host's address. `status` says so instead of telling you to
  pair again, since a pairing claim meets the same block, and Talaria's
  pairing status reports it as `relay_blocked`.
- Disabling or reloading the plugin (on a Hermes with unload callbacks)
  stops its background work: queued notifications and replies waiting for
  their turn's end are dropped, the delivery and progress threads end, and a
  call held back is kept for the next load.

## Live turn progress

While a turn you sent from Talaria runs, the plugin reports its progress
through the relay, and Talaria shows it as an ongoing notification: what
Hermes is doing (thinking, running a kind of tool, waiting for your approval
or answer), how many tools it used and how full its context is. The end of
the turn ends it.

- Talaria registers the turn with the relay under the chat's session ids.
  The plugin reports phase changes, tool starts, approvals, clarify
  questions and each model request's context use (`pre_api_request` /
  `post_api_request`; without them the rest still works).
- Reports are coalesced: at most one every 5 seconds per watched turn, and
  the end at once. The relay answers whether the phone follows the turn; a
  turn nobody follows is asked about again only every 15 seconds. A relay
  without the route, or one that refuses the credential, turns reporting off
  for half an hour.
- These reports aren't sealed end to end, so they are coarse on purpose:
  the session id, a phase, a tool category from a fixed list (`terminal`,
  `files`, `web`, `browser`, `connector`…, never a tool's name, arguments or
  output), a tool count and a context percentage. With `redact on` the tool
  category is left out too.
- The relay keeps these registrations in memory only, for up to 8 hours. A
  relay restart stops the updates.

## Clarify questions

Hermes' `clarify` can ask several questions in one call
(`questions: [{qid, question, choices, multi_select}]`). With the clarify
middleware (a Hermes with `register_middleware`), the plugin sends the whole
batch to Talaria as an answerable card, and Hermes takes whichever answer
comes first: the phone's, or one given in Desktop, the TUI or the CLI.

- The card carries `questions[]` with the qids, choices and `multi_select`
  flags. The relay keeps the first answer per qid, and the decision is done
  once every qid has one.
- The plugin polls the relay for the answers while Hermes' own prompt waits.
  The first surface to answer the whole batch wins. The plugin then releases
  the decision at the relay, so late answers from the phone are refused
  rather than reported as accepted.
- The plugin returns the answers in the built-in tool's result shape
  (`{"responses": [...]}`, multi-select answers as lists). A one-entry
  `questions[]` call keeps the batch shape; a legacy single-question call
  keeps the single shape.
- An answer is kept whole or refused, never cut: plain answers up to 2,000
  characters, sealed answers (encrypted pairings) up to 11,000.
- At most 8 questions with 8 choices each per batch. FCM limits a message's
  data to 4 KB, so a batch with long text can still be too large for a card.
  It is never cut or split: the plain "input needed" notification goes out
  without the card, and the question is answered through Hermes' own prompt.
  The same happens when the phone's preferences turn cards off.
- When the relay holds too many open questions, it answers `429
  decision_capacity_exceeded`. The plugin retries for about 30 seconds, then
  gives up, and Hermes' own prompt still works.
- Releasing a decision runs off the answer's path, so a slow relay never
  delays an answer given in Hermes.

A batch answered partly on the phone and partly in Hermes stays open until
Hermes' clarify timeout ends it: neither side sees the other's answers.

## Chat takeover

Hermes lets one app own a chat at a time. Hermes Desktop claims a chat on its
first turn and keeps the claim until the chat is closed there, so sending to
the same chat from Talaria is refused with "This chat is open in another
Hermes window/terminal". This route is what Talaria's **Take over** does: it
drops the other app's claim in `runtime/active_sessions.json`, under Hermes'
own registry lock, and Talaria's next send claims the chat.

| Method | Route | Returns |
| --- | --- | --- |
| POST | `/api/plugins/talaria_push/sessions/takeover` with `{session_ids: [..]}` | `{ok, status, surface?}` |

- `status` is `taken_over` (send again), `free` (nobody else holds it),
  `busy` (the other app is running a turn on it, or it's a live worker or
  gateway whose turns can't be seen; ask again shortly, nothing is
  interrupted) or `same_host` (this dashboard process holds it, for example
  Talaria on another device; that claim is left alone). A turn marker that
  can't be read counts as running. Only a marker whose writer is provably
  gone, or a gone owner's marker that names no writer, is ignored.
- The other app writes its turn marker without the registry lock, so a turn
  it starts in the instant between the check and the takeover still loses
  its claim. That turn keeps running there.
- Only Desktop and TUI claims are dropped while their owner is alive: they
  are the surfaces that write turn markers. A live claim from a CLI or kanban
  worker, a gateway, or with no surface answers `busy`. A claim whose owner
  process is gone is dropped whatever its surface, as long as no live claim
  of that kind also holds the chat.
- Only another process's claim is dropped; a claim without a valid pid is
  kept. The other app isn't told: Hermes Desktop still believes it owns the
  chat and doesn't show what you sent from Talaria until you reopen it there.
- `session_ids` takes 1 to 4 ids of one chat (its stored id and its live
  id), never ids of different chats.
- 400 for a bad body, 501 on a Hermes without the ownership registry or turn
  markers, 503 when the registry can't be read (ownership is never guessed),
  429 past 60 requests a minute, 504 if the takeover doesn't finish within
  20 seconds.

The other way round: a chat Talaria opened in the dashboard holds its claim
for as long as Talaria stays connected. The dashboard lets go of that claim
once the chat has been idle for a minute: no turn running, nothing waiting
on an answer, no delegated work, wake-up or background process still to
report, and no turn marker. The chat stays open in Talaria, and its next
send claims it again, or meets the usual refusal and **Take over** if
Desktop took it meanwhile. Only chats whose every client here is Talaria
count; a chat Desktop or the web dashboard also has open in this process,
and Bot Chats, keep their claim.

## Chats opened on Desktop

Talaria's Unread filter can count a chat as read when you have it open in
Hermes Desktop or the web dashboard on the same host. Hermes has no hook for
a client opening a chat, so the plugin wraps the gateway's `session.activate`
and `session.resume` handlers in the dashboard process. Per chat it keeps
the newest open (`opened_at`) and how long that chat then stayed the one its
Desktop connection had selected (`seen_through`), in
`<hermes home>/talaria-desktop-views.json` (at most 2,000 chats). It never
writes Hermes' own read flag, so Desktop's unread dots don't change.

| Method | Route | Returns |
| --- | --- | --- |
| GET | `/api/plugins/talaria_push/sessions/desktop-views?profile=&since=` | `{ok, observing, reason, views: {stored id: {opened_at, seen_through, client, open?}}}` |

- An open counts when it comes over the gateway's WebSocket from a browser
  engine (Desktop's window or the web dashboard: a `Mozilla/` User-Agent);
  `client` is `desktop` when it also says `Electron/`, else `browser`.
  Talaria's own socket, the TUI and hosted rooms never count. The User-Agent
  is the client's own claim, so this is best-effort sorting among clients
  that already hold a dashboard login.
- A connection's selection moves with its next open and ends when its socket
  closes (noticed within 5 seconds). `open: true` marks a chat that is
  selected right now; its `seen_through` is the moment of the read. The host
  can't tell whether anyone is looking at the window, and Desktop also
  re-attaches chats after a reconnect.
- `observing` is false, with a `reason`, while the gateway hasn't loaded
  (`gateway-not-in-process`, also what `plugins.isolation: host` reports) or
  when a Hermes update moved those handlers or the socket's headers
  (`gateway-unsupported`). The wrapper never fails a call and always returns
  Hermes' own response.
- `since` returns only chats seen after that time (seconds since the epoch);
  a value that isn't a finite number returns every chat. 429 past 60 reads a
  minute per profile.
- An open is skipped when the gateway can't say which profile its chat
  belongs to, rather than guessed onto the default profile.

## Quiet while you read in Desktop

A reply that comes back within 2 minutes in a chat Hermes Desktop (or the web
dashboard) has selected on this host sends no reply notification: you most
likely watched it arrive there. A longer turn still notifies, and every other
notification stays as it was: other chats, background tasks, approvals,
questions, failed turns and calls. There is nothing to set up.

- The selection is the one [Chats opened on Desktop](#chats-opened-on-desktop)
  follows. The host can't see whether the window is in front, so a short
  reply in a chat left selected on an unattended computer stays quiet on the
  phone.
- The dashboard writes the selected chat ids, per Hermes home, to
  `<hermes home>/talaria-desktop-selected.json` (private to the user) when
  they change, and renews the file every 30 seconds while anything is
  selected. The turn hooks trust it for 45 seconds, so a stopped dashboard
  stops quieting anything.
- A turn's 2 minutes run from its `pre_llm_call` to its `post_llm_call`. A
  turn with no start time, a file that can't be read, or `plugins.isolation:
  host` all notify as before.

## Quiet while you read in Talaria

A reply that lands in a chat you have open in Talaria sends no reply
notification. A long turn stays quiet as long as you keep using Talaria; one
left running with nobody touching or typing for 2 minutes notifies, like
Desktop's rule. Every other notification stays as it was. There is nothing
to set up.

- While a turn runs in the open chat, Talaria holds the chat as on screen
  with `PUT /api/plugins/talaria_push/notifications/on-screen?profile=…` and
  `{"device": "phone", "session_ids": ["<runtime id>", "<stored id>"],
  "hold_s": 90}`, renews the hold while the turn runs and someone used
  Talaria in the last 2 minutes, and sends `"hold_s": 0` once nobody has.
  Nothing is sent while no turn runs.
- After the turn ends the last hold runs out on its own, so the reply's own
  hooks still see it. Leaving the chat or the app sends `"hold_s": 0`.
- `device` is `phone` or `tablet`. Each device kind keeps its own file in
  `<hermes home>/talaria-on-screen/`
  (private to the user), so one device leaving never ends another's hold;
  two devices of the same kind share a file, and the later report wins.
- A turn longer than 30 minutes notifies whatever Talaria says. A hold
  longer than 10 minutes, a file that can't be read, or a turn whose hooks
  don't share the profile's home with the dashboard notifies as before.
  Capability `chat-on-screen`.

## Reply notifications by source

If you also talk to Hermes in Desktop, a messaging app or the terminal, you
already see those replies there. In Talaria's **Settings > Notifications**
you choose, per profile, which sources send a reply notification: Talaria,
Hermes Desktop and the web dashboard, messaging apps (Telegram, Discord,
Slack, plugin platforms…), and the terminal. Everything is on until you turn
it off. Approvals, questions, failed turns, background tasks, cron runs
(they have their own switch) and calls don't follow this.

- The source is where the message that started the reply's turn was sent,
  not where the chat began: a Telegram chat you continue in Talaria is a
  Talaria reply. Hermes binds each turn to the connection that submitted
  last before it started, so a message queued meanwhile belongs to the next
  turn.
- Talaria's socket carries `X-Talaria-Client`; another browser-engine socket
  is Desktop or the web dashboard. A turn with no app connection is placed by
  its Hermes platform: `cli` is the terminal, a Hermes messaging platform is
  a messaging app.
- A turn that can't be placed (an app connection under `plugins.isolation:
  host`, the stdio TUI, a Hermes that moved these) notifies as before.
- The choice lives in the profile's `talaria-notifications.json`.
  `GET /api/plugins/talaria_push/notifications/reply-sources` reads it
  (`{"ok": true, "sources": {"talaria": true, "desktop": false, …}}`) and
  `PUT` with `{"source": "desktop", "notify": false}` changes one.
  Capability `reply-sources`.

## Group rooms

### Files in group chats

A group chat room isn't a session, so Hermes' attach RPCs can't take a file
for it. Talaria uploads photos and files picked in a room's composer with
`POST /api/plugins/talaria_push/room-files` (`{"name": "photo.jpg",
"data_base64": "…"}`, up to 50 MB) and puts the `@file:` reference it gets
back (`{"ok": true, "path": "…", "ref_text": "@file:…"}`) on its own line
above the message, as Hermes Desktop does for a chat. The room's members run
on this host and read the file from there. Uploads go to
`<hermes home>/talaria/room-files/<id>/` and are deleted a month later, or
sooner, oldest first, once they pass 2 GB together (checked hourly). An
upload that would leave under 1 GB free on the host is refused (507).
Capability `room-files`.

### Group pictures

Hermes' gateway-hosted group rooms carry no picture, so Talaria keeps a
room's custom picture here. `GET /api/plugins/talaria_push/room-images`
returns each room's revision (`{"ok": true, "revisions": {"<room id>":
"<rev>"}}`), so Talaria fetches only pictures it hasn't cached, with
`GET /room-images/<room id>` (`{"rev": "…", "mime": "image/jpeg",
"data_base64": "…"}`). `PUT /room-images/<room id>` with `{"data_base64":
"…"}` sets a JPEG or PNG of up to 512 KB; `DELETE` clears it. Records live in
`<hermes home>/talaria/room-images/` (a small record plus the picture per
room, so listing never reads picture bytes), up to 500 rooms (the oldest go
first). Capability `room-images`.

### Group rooms and memory

In a gateway-hosted group room, each member's turn runs on its own host in a
hidden session (`source: bot_room`, titled `Group: <room id>`), and the
turn's message is the room's new messages, from several people and bots.
Hermes hands that turn to the profile's external memory provider (Honcho,
Hindsight and the like) with no author, so the provider would file
everything said in the room as the owner's own words. The plugin keeps room
sessions from saving into the provider: the per-turn sync, the session-end
extraction and the provider's part of the summary before a compaction are
skipped for them, also after a compaction moves the session to a new id.
Recall still runs, and `MEMORY.md` and `USER.md` still load, so a member
knows its owner in the room; it just doesn't learn from what others say
there. The memory and skill tools work as before. If Hermes' memory manager
lacks one of the methods the plugin guards, the plugin guards the rest and
logs a warning naming what's missing. If Hermes' session store (`state.db`)
can't be read when the plugin needs it to tell whether a session is a room,
the plugin logs a warning and treats that session as an ordinary chat.

## Artifacts

Talaria's Artifacts screens list what a profile's chats produced.
`GET /api/plugins/talaria_push/artifacts` reads the profile's `state.db`
(read only), newest message first, and returns
`{"ok": true, "items": [...], "next_before": 1234}`.

- Each item has a `kind` (`image`, `video`, `audio`, `page`, `document`,
  `file` or `link`), a `source` (`delivered` for a `MEDIA:` file, `preview`
  for a `::preview` page, `written` for a document or media file a tool
  wrote, `attached` for a file the user sent with `@file:`, `link` for a web
  link in a reply), a `name`, the chat's `session_id` and `session_title`,
  the `message_id` it came from and its `timestamp`. A file has its `path`,
  whether it still `exists` and its `size`; a link has its `url` and `host`.
- Within a page, one path or URL is listed once, at its newest mention (a
  later page can list it again at an older one).
- Query: `session_id` (one chat; repeat it for each id the chat is known by,
  and the sessions it continues from or spawned are read too), `kind`
  (`images`, `pages`, `files` or `links`), `q` (words in the name, chat
  title, path or URL), `limit` (up to 200), `before` (the previous page's
  `next_before`; `null` once the history is read to its start) and
  `missing=hide` (leave out files no longer on the host; links always stay).
- A request reads a bounded window of rows, so a page can come back short
  with a `next_before` to keep going. With `session_id` only that chat's own
  rows are read. One message adds at most 200 artifacts to a page (a page
  ends after the message that fills it, so it can run past `limit`).

Capability `artifacts`.

## Hermes calls you

Hermes can phone you through Talaria: the plugin sends a call request
through the relay, and the phone rings like a call. Answering opens a voice
conversation in the job's chat, where Hermes opens with its reason and the
turn's result. A call request takes the place of the usual reply or failure
notification for that turn.

Calls are off until you turn them on in Talaria's Settings, under **Calls
from Hermes**. Settings and pending calls live per profile in
`<hermes home>/talaria-calls.json` (mode `0600`), shared by the turn-end
hooks and the dashboard routes under one file lock, so they survive
restarts:

| Setting | Default | Range |
| --- | --- | --- |
| `enabled` (Hermes can call me) | off | |
| `when_asked` (call when I explicitly ask) | on | |
| `decides` (Hermes decides when to call) | off | |
| `alerts` (approval, question and failure calls) | off | |
| `min_gap_s` (time between calls) | 120 s | 30–3600 s |
| `per_hour` | 6 | 1–30 |
| `per_day` | 20 | 1–60 |
| `rules` (what's worth a call) | empty | up to 500 characters |
| `avoid_places` (saved place ids) | none | |
| `avoid_unknown_place` | off | |

| Method | Route | Body | Returns |
| --- | --- | --- | --- |
| GET | `/api/plugins/talaria_push/calls?profile=` | | `{ok, paired, settings, bounds, rules_max, watches}` |
| PUT | `/api/plugins/talaria_push/calls?profile=` | `{settings: {...}}` (any subset) | `{ok, settings}` |
| POST | `/api/plugins/talaria_push/calls/outcomes?profile=` | `{call_id, session_ids: [...], outcome: "declined" or "missed", kind?, title?, reason?, age_s?}` | `{ok, status: "recorded"}` |

- `watches` is how many calls are waiting for their turn to end. Each fires
  once, on the first turn end of its session: `done`, `failed` or `stopped`.
  They expire after 24 hours, and a profile holds at most 20.
- 400 for an unknown setting or a value outside its range, 409 when the
  profile isn't paired, 429 past 30 requests a minute per profile.
- A call held back by your limits, or by calls being turned off, sends the
  usual notification instead. So does a relay that refuses the call request
  (the relay caps call requests per phone per day), one that took the
  request but couldn't reach Google, or one that stays unreachable or keeps
  failing (5xx) after three tries 2 and 4 seconds apart, with the same event
  id.
- The call request carries the job's id, outcome, title, reason (up to 200
  characters) and session ids, so Talaria can open the right chat. With
  end-to-end encryption they are sealed like any other content; with `redact
  on` the title and reason are dropped. Kinds besides how a job ended are
  `approval` and `question`.
- **No calls at places.** With location sharing on, a call that would ring
  while you are at a place in `avoid_places` goes out as a notification
  instead (status `limited`, reason `place`). `avoid_unknown_place` also
  holds calls when there's no recent location, or sharing is paused or off.
  The latest fix counts for 24 hours with background sharing on, otherwise
  1 hour.
- **Declined and missed calls.** When you decline a call, or it rings out or
  is silenced, Talaria reports it on the outcomes route with `age_s` (how
  long ago, by the phone's clock). The next turn of one of those sessions
  gets a one-line note through the `pre_llm_call` hook, for example "the
  user declined the phone call you placed 12 minutes ago", once. Nothing
  rings again. A retried report keeps the first; a profile keeps at most 20
  waiting, each for a day. 409 when the profile isn't paired, 400 for
  anything malformed.

### Hermes asks to call

The plugin gives Hermes a `talaria_call_user` tool (toolset `talaria`) and a
bundled skill, `talaria_push:calling-the-user` (plugin skills aren't listed
in the skill index, so the tool's description points to it). Hermes calls it
with a `reason` (one spoken sentence, up to 200 characters) and
`asked_by_user`:

- The call rings when the asking turn ends, so it opens on the finished
  result: the turn's final reply is in the chat the call opens, and the
  reason is said first. This works from any chat Hermes runs in (Talaria,
  Desktop, other platforms, cron jobs). It doesn't ring while you are
  already in a voice call with Hermes.
- `asked_by_user: true` needs "call when I ask"; `false` needs "Hermes
  decides". Either way the limits apply, and a session already waiting on a
  call keeps one (it takes the new reason).
- **What's worth a call.** With `rules` written (your own words, such as
  "only if production is down"), a call Hermes decides on first answers
  `check_rules` with them. It goes only when Hermes calls again with
  `fits_rules: true` in the same chat, within 10 minutes, while the rules are
  unchanged. Calls you asked for skip the rules, and Hermes can read them but
  not change them. The rules steer Hermes rather than bind it:
  `asked_by_user` is Hermes' own word, so a call it marks as asked for skips
  them.
- A call you asked for while calls are off answers `calls_off` with a
  `talaria://settings/calls` link for Hermes' reply, which opens Calls from
  Hermes in Talaria.
- The tool shows only while this profile is paired and one of "call when I
  ask" or "Hermes decides" is on. Subagents can't call; their parent can.
- If `hermes tools` lists the `talaria` toolset as off for a platform, turn
  it on there.

### Call watches

The rules only filter the calls Hermes makes; they don't watch anything.
When you ask to be called if something happens ("call me if the homelab is
down for more than 10 minutes"), Hermes uses `talaria_call_watch` and only
describes the watch: `what`, `how` a run checks it, `for_minutes` it must
last, and optional `hours` (HH:MM-HH:MM) and `unless`.

- The plugin writes the cron job through Hermes' own `cronjob` function:
  named "Call watch: <what>", checked every few minutes (picked from
  `for_minutes`), with a fixed run prompt. The same `what` is the same
  watch, so asking again changes it instead of adding another.
- Actions are `list`, `set`, `sync` and `remove`. `sync` takes every watch
  your "What's worth a call" note describes: it changes and adds watches to
  match, and lists the ones the note no longer mentions, which Hermes asks
  about before removing.
- Each run checks and calls `talaria_watch_report` with whether it's
  happening. The plugin keeps the incident per job in
  `talaria-call-watches.json`, waits out `for_minutes` and the allowed
  hours, calls once per incident with the run's sentence as the reason, and
  tells the run what to reply (`[SILENT]` while there's nothing to send).
  `talaria_call_user` refuses in such a run.
- `talaria_call_watch` shows while the profile is paired on a Hermes with
  scheduled jobs, and `talaria_watch_report` while it's paired (it only
  works in a watch's run). When `talaria` is off for cron in `hermes tools`,
  each watch's job gets its own toolset list (cron's, plus `talaria`) so its
  runs can report.

### Alert calls

With `alerts` on, Hermes also calls when it waits on you:

- **Approval:** an approval request still unanswered after a minute rings
  (kind `approval`, the request's description as the reason), beside its
  usual notification. Answering it anywhere first (`post_approval_response`)
  or the turn ending cancels the call.
- **Question:** the same for a clarify question (kind `question`). Needs the
  clarify middleware.
- **Failed turn:** a turn that fails calls in place of its failure
  notification (kind `failed`).

Alert calls count against the same limits.

## Talaria first

Every turn of a main conversation gets one line, through `pre_llm_call`,
naming what the phone shares with this profile (calendar and reminders,
health, location, phone status, contacts) and saying to use the Talaria tool
before other tools, skills or command-line tools for the same data, such as
gog. Each of those tools' descriptions ends the same way. Hermes drops
`pre_llm_call` context after the turn, so the line never piles up; what's
shared is looked up at most once a minute. Subagents don't get it.

## Hermes changes Talaria settings

Ask Hermes to set Talaria up ("make calls sound more relaxed", "use a
different voice") and it can, through the `talaria_settings` tool, within
what Talaria allows:

- **Talaria decides what Hermes sees.** Each Talaria uploads the settings it
  shares (key, plain-language title and meaning, choices, value and tier)
  and your **Let Hermes change settings** mode to
  `PUT /api/plugins/talaria_push/talaria-settings` when it connects, comes
  to the foreground, changes a shared setting, or a turn used the tool.
  Anything it doesn't list is out of Hermes' reach.
- **Hermes only asks.** `get` lists the settings; `change` keeps a request
  with the value Hermes saw and a reason; `cancel` takes back one Talaria
  hasn't picked up. Requests wait in the profile's `talaria-settings.json`
  for a day.
- **Talaria applies or asks.** It checks every request against its own list
  again. With the default mode, **Ask me every time**, each change waits for
  your Apply. With **Small changes without asking**, the small tier applies
  with an Undo and the rest still asks. **Off** refuses everything. A
  setting you changed after Hermes read it keeps your value. Talaria reports
  each outcome (applied, dismissed, conflict, refused, undone) on its next
  sync, and `get` shows it to Hermes.
- Speech providers and their keys, and models, are Hermes settings, not
  Talaria's: the tool tells Hermes to change those in its own config.

The tool shows only once a Talaria has synced with the profile. Limits: 20
requested changes per device per hour, 10 per call, 8 devices per profile.

## Hermes asks you to confirm

Before an action you should OK (sending a message for you, buying,
deleting), Hermes can ask on your phone with the `talaria_present` tool:

- **Tool.** `confirm` keeps a prompt (title up to 80 characters, a Markdown
  preview up to 2,000, a button label up to 24, up to 5 `spoken_yes`
  phrases, `ttl_minutes` 30 by default and at most 1,440) and wakes the
  phone with a `phone.changes` push. `wait_seconds` (up to 120) waits for
  the answer. `status` returns waiting prompts and answers Hermes hasn't
  seen yet (or one by `id`); `cancel` takes one back. States: `waiting`,
  `confirmed`, `declined`, `edit`, `expired`, `cancelled`. Only `confirmed`
  means go ahead; on `edit` you send the corrected request yourself.
- **Talaria answers.** You confirm by tap, or by voice during a call (the
  `spoken_yes` phrases). Talaria reports answers and fetches waiting prompts
  with `POST /api/plugins/talaria_push/present/exchange?profile=`:
  `{"installation_id", "device", "app_version", "results": [{"id", "outcome": "confirmed|declined|edit", "edit_text"?, "via": "tap|voice"}]}`
  answers `{"ok": true, "prompts": [{"id", "title", "preview", "action_label", "spoken_yes", "session_id", "created_at", "expires_at"}]}`.
  The first answer wins; answers for unknown or finished prompts are
  ignored.

The tool shows only once a Talaria with `present-v1` has exchanged with the
profile; subagents can't use it. Prompts live in the profile's
`talaria-present.json` (answers are kept a week). Limits: 20 prompts an
hour, 20 waiting at once, 60 exchanges a minute per profile.

## Cards on screen

In a Talaria voice conversation, a table, steps, a chart or links are better
seen than heard. Hermes puts them on the call's screen with `talaria_show`
(`title` up to 80 characters, `markdown` up to 8,000) and says in a sentence
that they're there. Voice turns get a note saying so while a Talaria that
shows cards is connected.

- **No push.** The card is kept in `talaria-present.json` and listed in the
  same `present/exchange` answer as
  `"cards": [{"id", "title", "markdown", "session_id", "created_at", "expires_at"}]`
  (only when there are any). Talaria exchanges as soon as it sees the tool
  in the turn and again when the reply ends, and shows each card once.
- **Short-lived.** A card is dropped after ten minutes: it belongs to the
  conversation it was shown in.

The tool shows only while a Talaria that says `show-cards` in its exchange's
`features` has exchanged with the profile in the last six hours, and only
that phone gets the cards; subagents can't use it. Limits: 60 cards an hour,
the newest 20 kept.

### Rich cards

`talaria_show` also takes `cards`: up to four cards Talaria draws natively,
in a typed chat as well as on a call. The types are `map`, `place`,
`places`, `stats`, `chart`, `weather`, `comparison`, `steps`, `media`,
`contact` and `status`. `markdown` is optional with cards; without it, the
cards' own Markdown (each place with a maps link) is what a phone that
doesn't draw cards, or another client, shows. A call-screen card whose
Markdown is only that text carries `"fallback": true` in the exchange, so a
phone that draws the cards leaves it out.

- **One schema.** [`cards/card-schema.json`](cards/card-schema.json) is the
  only definition. The tool's `cards` parameter is built from it and
  `card_schema.py` normalizes against it. Talaria ships a byte-identical
  copy; [`cards/card-vectors.json`](cards/card-vectors.json) holds examples
  both test suites run. Change both files together and merge here first.
- **Sourced facts only.** The schema has no fields for a place's address,
  category, phone, website or travel time. A rating, hours, price or photo
  needs a `source`, or it's dropped; a place's or person's photo also needs
  an https `url`.
- **Kept in history.** The tool's answer carries the normalized cards
  (`{"ok": true, "cards": [...]}`), so the chat's history keeps them and
  Talaria draws them whenever the chat is opened. With no call screen, a
  Talaria that says `chat-cards` in its exchange's `features` still gets the
  tool, and the card shows in the chat only. A turn that isn't from Talaria
  (Desktop, or a messaging app through Hermes' gateway) is refused with
  `not_talaria`. A classic `hermes` terminal turn can't be told apart yet
  (Hermes doesn't bind its platform for tools), so it isn't refused.
- **Information cards.** `stats` (big numbers with a change and a small
  line), `chart` (line, area, bar or pie), `weather` (now, the next hours
  and days), `comparison` (two to four options, at most one recommended) and
  `steps` (what you need, then steps to tick off, with timers). Each type has
  its own field, and the schema's rules keep only that field. These cards
  take a card-level `source` (`name`, and a `url` if any); a forecast needs
  one or it's dropped.
- **Media cards.** `media`: up to 12 photos, videos or audio clips, as
  direct https links (`kind`, `url`, and `title`, `by`, `caption`,
  `duration`, `artwork`, `page`). A media card needs a `source`.
- **Contact cards.** `contact`: up to six people (`people`: `name`,
  `detail`, `phone`, `email`, `url`, `address`, `note`, and a `photo` with
  its source). A phone or email that doesn't read as one is dropped. A
  person with a `phone` can carry a `message`, a text Hermes wrote for you to
  send them: Talaria opens the messaging app with it filled in, and only
  your tap sends it.
- **Status cards.** `status`: where something in progress stands (`state`,
  `phase` waiting/active/done/problem, `progress` 0–1, `detail`, `eta`,
  `milestones`).
- **Live names.** Any card can carry `live`, a name Hermes picks. Showing a
  card with the same live name again in the conversation replaces the
  earlier one, on the call screen and in the chat, so Hermes keeps a
  delivery, a score or a job's progress up to date by showing it again.
- **Only where they're drawn.** A Talaria says `info-cards`, `media-cards`,
  `live-cards` and `text-drafts` in its exchange's `features` when it
  supports them. Unless every Talaria that draws cards (`show-cards` or
  `chat-cards`) and exchanged with the profile in the last 30 days says it,
  the tool leaves those cards out (`unsupported_cards` when nothing else is
  left), and leaves a contact's `message` off the card and tells Hermes to
  give it in its reply. Each phone's exchange also leaves out the cards it
  can't draw and shows the Markdown instead.

## Health data from Talaria

Talaria can share the phone's health data from Health Connect with a Hermes
profile, so the agent can answer "how did I sleep?" or "how active was I
this week?". It's off until you turn it on in Talaria, per category
(activity, heart, sleep, workouts, body, vitals, fitness), with how far back
to start.

- **Straight to this host.** The phone posts to
  `POST /api/plugins/talaria_push/health/ingest` over your dashboard
  connection. Health data never passes through the push relay or any
  Talaria server.
- **Sealed end to end.** Each phone agrees a key with the profile once
  (`POST /health/key`, X25519: only public keys cross the wire, so watching
  that exchange doesn't reveal the key). Every batch is sealed with
  ChaCha20-Poly1305 under an HKDF-SHA256 key from it, on top of any TLS, so
  even a dashboard on plain http in your LAN never carries health data in
  the clear. Someone actively tampering with a plain-http connection while
  the key is set up could still intercept it: use HTTPS or a private tunnel
  when you can.
- **Who can use these routes.** Anyone signed in to your dashboard, as for
  every other dashboard route; push pairing isn't needed. A signed-in client
  could register a new key under a phone's device id and send data in its
  name (the phone notices on its next sync and sets up a new key), so treat
  dashboard access as access to your health data. A new device key for a
  device id drops what the earlier key sent (the phone sends it all again),
  so a re-key can't be used to read a phone's data back.
- **Refusals.** A refused request answers with its HTTP status and
  `{"detail": "<message>", "code": "<word>"}`. `unknown_key` and `bad_seal`
  (409) mean this host doesn't have the phone's key (the phone sets up a new
  one); `too_many_devices` and `schema` (also 409) don't. The location and
  phone routes answer the same way. Other errors carry only `detail`.
- **Summaries first.** Talaria sends hourly and daily totals computed on the
  phone (every source already merged), a summary per night of sleep, sleep
  stage segments and workouts. Raw readings go only for categories where you
  turn on detailed readings.
- **Stored here only.** The profile's `talaria-health.sqlite3`, mode
  `0600`. A newer computation of a window replaces the older one; a deleted
  workout or reading stays deleted. `GET /health/status` reports counts and
  dates, never values; `POST /health/wipe` deletes one device's data and key
  (or everything with `{"all": true}`). No route returns health data.
- **Read-only tool.** `talaria_health` (actions `overview`, `daily`,
  `hourly`, `sleep`, `workouts`, `samples`; at most 92 days per call) with
  its skill `talaria_push:reading-health-data`. It shows only once a phone
  has synced data, and subagents can't use it. What it returns goes to your
  agent's model like any tool result, which Talaria tells you before you
  turn sharing on.

Limits: 4 devices per profile, 6,000 items and 4 MiB per batch, 120 uploads
a minute per profile.

## Location from Talaria

Talaria can tell a Hermes profile where you are. It's a building block
rather than one feature: the agent can answer "how far am I from work?",
follow place rules ("call me with my brief when I leave home on weekdays"),
use it from any scheduled job or skill you teach it, and calls can skip
places you pick. Two separate opt-ins in Talaria:

- **While talking:** one fix when you send a message or start a voice call,
  and again when you've moved over 200 m during a call. Precise by default.
- **In the background:** arriving at and leaving saved places (up to 20),
  visits and significant moves. Area by default.

Each layer has its own precision: `places` (saved place names only), `area`
(about 1 km: neighbourhood and town, coordinates rounded to 2 decimals) or
`precise`. The phone applies it before sending and the host applies it
again. Saved places live on the phone: this host gets their names and kinds,
never their coordinates, and each fix carries distances to them instead. A
place marked private is reported only as "at a private place".

- **Straight to this host, sealed.** Same scheme as health with its own key
  and labels (`POST /location/key`, `POST /location/ingest`): X25519, then
  ChaCha20-Poly1305 under an HKDF-SHA256 key. Location never passes through
  the push relay; the relay only carries the on-demand request (below).
- **Stored here only.** The profile's `talaria-location.sqlite3`, mode
  `0600`. History is kept as long as you pick in Talaria (latest only, 1, 7
  by default, 30 or 365 days). `GET /location/status` reports counts, never
  locations, and `locate.available` (whether `locate` can reach the phone:
  this profile is paired for notifications). `GET /location/reads` lists
  when the agent read location (kept 30 days, without the locations).
  `POST /location/wipe` deletes one device's data and key, or everything
  with `{"all": true}`. A new device key for a device id (a reinstall,
  sharing turned off and on, a new pairing) drops that device's fixes,
  visits and saved places; the phone sends its places again with the new
  key.
- **Tool.** `talaria_location` (actions `now`, `locate`, `places`,
  `history`, `distance_to`, `rules`, `set_rule`, `remove_rule`) with its
  skill `talaria_push:using-location`. It answers "paused" or "off" when you
  have paused sharing, logs every read, and subagents can't use it.
- **On-demand locate.** `locate` asks the phone for a fresh fix with a
  silent push through the relay, which the phone answers only while
  background sharing is on and not paused. At most one every 5 minutes and
  30 a day per location store (profiles reading a shared one spend its
  budget).
- **Place rules.** `set_rule` keeps a Hermes cron job named
  `Place rule: <what>` that never runs on its own schedule. An arrive or
  leave event that matches the rule's place (one saved place or any), move
  (arrive, leave or either), days and hours triggers it with a line naming
  the place and move (`GET /location/rules`, `DELETE /location/rules/<id>`).
  The task is anything a cron run can do. A rule fires once a day, or every
  time at least 30 minutes apart (`repeat: "every"`), never for an event
  over 15 minutes old, and never while sharing is paused.
- **Other profiles on this host.** Talaria binds sharing to one profile,
  which keeps the store, key and place rules. With "Share with this host's
  other profiles" on (`sharing.other_profiles: true`), every other profile on
  the same host paired with the same phone (the same relay installation in
  its `talaria-push.json`) reads that store: `now`, `places`, `history`,
  `distance_to`, `locate` and maps lookups. A locate goes through the asking
  profile's own pairing and names the sharing profile, so the phone answers
  it as that profile's. Reads are logged in the sharing profile's store as
  `<profile>: <chat>`. `rules`, `set_rule` and `remove_rule` answer
  `other_profile`: rules stay with the sharing profile. A profile that
  shares location itself always reads its own, and if several profiles share
  with the others the first wins (default, then named ones alphabetically).
  The locate and maps budgets belong to the sharing profile's store. Calls
  placed from a reading profile are held back by the shared position, but
  the places to avoid are that profile's own calls settings.

Limits: 4 devices per profile, 20 places, 256 KiB per batch, 60 uploads a
minute per profile.

## Maps

`talaria_maps` gives Hermes Google Maps, looked up from this host: `search`
(places or categories near you, a place or an address), `lookup` (one place
or address), `reverse_geocode`, `directions` (routes with traffic and steps,
by car, on foot, by transit or bike, and `leave_by` for `arrive_by`) and
`travel_times` (up to 10 destinations at once). Places can be `"me"`, a
saved place's name, an address or a name, `place_id:<id>` from an earlier
answer, or `lat,lon`. It's a building block like location: crons, place
rules and skills can use it. Lookups are logged in the location read log
(action and chat, never the query).

`navigate` opens Maps on the phone with directions to a place. It's a
confirm prompt (`talaria_present`'s store and exchange) carrying
`maps: {to, mode}`: Talaria opens Maps at once when you are in that chat
with Talaria open, and otherwise shows a Go button and a notification,
answered by tap or by saying "go" in a call. Talaria says it can do this
with `features: ["maps-navigate"]` in its present exchange; without one that
said so in the last 30 days, the tool returns a maps link for a text chat
instead. Only a Talaria that says so gets the Go prompt, and only its answer
counts.

### Google Maps on the host

The host answers lookups with its own Google Maps Platform key and needs no
relay for them. Put the key in Hermes' `.env` as `GOOGLE_MAPS_API_KEY`;
without one the tool is hidden.

- Search and places use Places API (New), directions and travel times the
  Routes API (with traffic when driving), and what's at a spot the
  Geocoding API, so the key's project needs those three enabled.
- Saved places can't be looked up here: their coordinates live on the phone
  (a trip from "me" to one gets a labelled straight-line guess). "me" is the
  last shared fix, at the detail you shared it.
- The key stays on this host: no phone, relay or app build carries it.
  Restrict it to those three APIs (and, if the host has a fixed address, to
  that address), and set a budget alert: Google bills the key's project.
  Each host makes at most 200 calls a day.
- `navigate` to a `place_id` from a Google answer sends the place's
  coordinates to the phone, and the link for a text chat is a Google Maps
  one.

## Calendar, reminders and phone status

Talaria can share your calendars, reminders, Focus or Do Not Disturb state
and a snapshot of the phone (battery, charging, power saving, audio route,
network, motion) with a Hermes profile, so the agent can answer "what's on
today?", find a free slot, add the dentist on Thursday, remind you to call
mum when you get home, or keep it short when the battery is low. Each kind
is off until you turn it on in Talaria, with which calendars and lists, how
far back and ahead, and whether notes and attendees are included (off by
default).

- **Straight to this host, sealed end to end.** The phone posts to
  `POST /api/plugins/talaria_push/phone/ingest` over your dashboard
  connection, never through the push relay. It agrees its own key with the
  profile (`POST /phone/key`, X25519, separate from health and location, so
  each can be on alone and wiping one never touches another), and every
  body is sealed with ChaCha20-Poly1305 under HKDF-SHA256 keys from it, on
  top of any TLS. The same caveats as health apply: use HTTPS or a private
  tunnel when you can, and treat dashboard access as access to this data.
- **What you chose, enforced here too.** The phone says what it shares in
  every batch and the store holds it to that: no notes, attendees or links
  without "Include notes and attendees", no attendee emails without that
  switch, no events at all when a calendar is "add only" or off, and status
  or Focus forgotten as soon as either is turned off. Only the shared window
  is kept (past days to future days). `calendars` is your current selection,
  replaced whole: events and reminders on a calendar or list it leaves out
  are deleted. Status and Focus are the latest value only. A batch, status or
  Focus time more than 5 minutes ahead of this host's clock is refused with
  code `clock_skew`. Calendars or reminders shared "read only"
  (`calendar_read_only` / `reminders_read_only`) are read but never written:
  every change to them is refused with `shared_read_only`.
- **Writes are changes the phone applies.** Hermes can't touch the phone's
  calendar, so `talaria_calendar`'s write actions queue a pending change. The
  plugin nudges the phone with a `phone.changes` relay event (sealed like any
  notification when the pairing has end-to-end encryption): a visible
  notification for a change you approve on the phone (`mode: "alert"`), or
  a silent push for an add you let Hermes make without asking
  (`mode: "background"`). Updates, deletes and completions always ask, and
  adds to shared calendars or lists always ask. The phone picks the queue up
  and reports outcomes (applied, edited, dismissed, failed, expired) with one
  sealed `POST /phone/exchange`. The notification carries no calendar data
  beyond the sealed title and body: the change itself comes from this host.
  Pending changes expire after 7 days, at most 50 per profile. If the nudge
  fails, the phone still finds the change on its next exchange.
- **Stored here only.** The profile's `talaria-phone.sqlite3`, mode `0600`.
  `GET /phone/status` reports counts and dates, never content;
  `POST /phone/wipe` deletes one device's data, key and pending changes (or
  everything with `{"all": true}`).
- **Tools.** `talaria_calendar` (reads `overview`, `calendars`, `events`,
  `free_busy`, `next`, `reminders`; writes `add_event`, `update_event`,
  `delete_event`, `add_reminder`, `update_reminder`, `complete_reminder`,
  `delete_reminder`; `pending` for how changes went) and `talaria_phone`
  (`status`), with the skill `talaria_push:using-calendar`. Each shows only
  once a phone shares what it reads; subagents can't use them. Reads say how
  fresh the copy is ("as of" the last sync). What they return goes to your
  agent's model like any tool result, which Talaria tells you before you turn
  sharing on.
- **Alarms and timers.** When you let Hermes set them, `talaria_phone` gains
  `alarms` (the alarms and timers Hermes set that are still on), `set_alarm`
  (one-off `at`, or `time` HH:MM with `repeats` days), `set_timer`
  (`seconds`) and `cancel_alarm`, as pending changes like calendar writes.
  Setting one goes without asking when you let Hermes add without asking;
  cancelling always asks.
- **Contacts.** Only the people you pick in Talaria: name, nickname, a
  relation you type ("wife"), phone numbers, emails, birthday and
  organization, read with `talaria_contacts` (`people`, with an optional
  `query`). Turned off, they are deleted here.

Limits: 4 devices per profile, 5,000 items and 4 MiB per batch, 50 alarms
and 200 contacts per device, 120 uploads and 60 exchanges a minute per
profile, 64 KiB per exchange.

## Capabilities

`GET /api/plugins/talaria_push/capabilities` returns the plugin version and
the Talaria features its routes serve:

```json
{"ok": true, "version": "1.0.0", "loaded_version": "1.0.0", "restart_needed": false, "boot_id": "9f2c41d07a3b5e68", "capabilities": ["session-takeover", "e2e-notifications", "...", "pairing", "self-restart"]}
```

Talaria reads it on connect. When a feature it uses is missing, Settings asks
you to update the plugin. Every new route family adds its name here.

## Privacy

What the plugin sends, and where:

- **To the relay** (over HTTPS): notifications, call requests, clarify cards,
  live turn progress, and the silent pushes that wake the phone for a locate
  or a pending change, for the profiles you paired. The relay passes them to
  Google's FCM, which delivers them to the phone.
- **End-to-end encryption.** Talaria creates a secret for each pairing on
  the phone and hands it to this profile over your Hermes dashboard
  connection, so it never passes through the relay or Google. From then on
  every notification's title, body, profile, session and decision card, and
  every call request, is sealed with it (ChaCha20-Poly1305, keys derived
  with HKDF-SHA256), and clarify answers come back sealed for this profile.
  A pairing that has a key never sends plaintext content again, and accepts
  only clarify answers sealed by the phone. Talaria turns it on by itself
  when it connects to this host; `hermes talaria status` shows whether it's
  on, and `hermes talaria test --corrupt` checks that the phone shows only
  generic text for a push it can't verify. The secret is only as private in
  transit as your dashboard connection.
- **What the relay and Google see:** routing data only. That is the event
  type, opaque ids, a keyed thread token and timing, plus the generic text
  for the event type. Live turn progress isn't sealed: it carries a phase, a
  tool category from a fixed list, a tool count and a context percentage,
  never chat text.
- **Redaction.** `hermes talaria redact on` keeps chat content from leaving
  the host, with or without encryption: events carry no title or body text,
  approval cards say "Hermes needs your approval. Open Talaria for details.",
  clarify cards replace question text with generic text, call requests drop
  their title and reason, and live progress drops the tool category. What's
  needed to answer stays (session key or request id, question ids, choice
  labels), so cards remain answerable; choice labels still pass through the
  relay because the answer is one of them. `redact off` restores full
  content, including for events still waiting in the local queue.
- **Never through the relay:** health, location, calendar, reminders, phone
  status, alarms and contacts. Your phone sends them straight to this host
  over your signed-in dashboard connection, sealed end to end, and only when
  you turn sharing on. What the tools read goes to your model like any tool
  result.
- **To Google Maps**, only when you put `GOOGLE_MAPS_API_KEY` in Hermes'
  `.env`: the maps lookups the agent makes, with your last shared location
  at the detail you shared it. The key stays on this host.
- **Kept on this host:** the pairing credential (`talaria-push.json`, mode
  `0600`) and the plugin's other `talaria-*` files in the profile's Hermes
  home. Credentials are never logged or printed by `status`.
- **Subagents.** The health, location, maps, calendar, phone, contacts,
  calls and display tools refuse delegated subagents, including one whose
  session id a compaction replaced: the plugin asks Hermes' delegation flag
  and follows the compaction links in Hermes' session store back to the
  subagent it was.

## Running your own relay

The relay is the server between this plugin and the phone:

```
Hermes (talaria_push plugin) ──HTTPS──▶ Talaria relay ──FCM──▶ Talaria on Android
```

It handles phone registration, pairing codes, per-phone preferences, rate
limiting and idempotent event delivery. It runs on its own server, with
your own Firebase project. The code and setup steps are in
[`relay/`](relay/README.md).

The plugin keeps the relay URL per profile, in `talaria-push.json`, and sets
it at pairing time. There is no separate setting:

- Pairing from Talaria uses the relay the app sends. If it sends none, the
  profile's current relay is used; there is no built-in relay.
- `hermes talaria pair` uses `--relay-url`, else the profile's current
  relay, and refuses to pair with neither. Pass your relay's address when you
  pair a profile by hand for the first time:

  ```bash
  hermes talaria pair XXXXX-XXXXX --relay-url https://relay.example.com
  ```

- To move a profile to another relay, pair it again against that relay.
  `hermes talaria status` shows which relay a profile uses.

Which URLs the plugin accepts (`relay_urls.py`):

- Every request that carries a credential goes to an `https://` relay. Plain
  `http://` is allowed only to this machine (loopback).
- A pairing claim may also use plain `http://` to a private address (LAN,
  Tailscale, link-local, or a name ending in `.local`, `.lan`, `.ts.net` and
  the like). The relay then answers with its HTTPS `PUBLIC_URL`, and that is
  what gets saved.
- The saved relay is the one the relay names, when it is a valid HTTPS URL on
  the same origin as the claim, or on any origin when the claim went to a
  private address. Otherwise the claimed URL itself is kept, if it is fit to
  carry a credential.
- No URL with credentials, a query or a fragment is used, and no cloud
  metadata address.

## Development

The plugin uses the Python standard library, Hermes' own APIs and
`cryptography` (a Hermes dependency). CI
([`.github/workflows/tests.yml`](.github/workflows/tests.yml)) runs the tests
on every PR and every push to main. To run them locally:

```bash
pip install -r tests/requirements.txt
python -m pytest --rootdir=tests tests
```

Most features here have siblings in the Talaria app, and a version bump
changes several files together. Read [CONTRIBUTING.md](CONTRIBUTING.md)
before you open a PR.

## Credits

The Talaria plugin started from kaishi00's
[hermes-conduit-notifier](https://github.com/kaishi00/hermes-conduit-notifier)
(MIT), the `conduit_push` plugin for the iOS app Hermes Conduit. Talaria is a
separate project, not made or supported by the Conduit author.

## License

MIT. See [LICENSE](LICENSE).
