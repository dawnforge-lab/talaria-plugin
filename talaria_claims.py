"""Letting go of Talaria's claim on a chat once it sits idle.

Hermes lets one live surface own a chat at a time (hermes_cli.active_sessions).
A chat Talaria opens in the dashboard claims it on its first turn and keeps the
claim for as long as the chat's runtime lives here, which is as long as
Talaria's connection stays open. Hermes Desktop running its own process is then
refused the same chat (SESSION_NOT_OWNED) long after Talaria's turn ended: the
reverse of the takeover Talaria offers (#304).

So the dashboard lets go of a claim Talaria holds once the chat has been idle
for IDLE_S: no turn running, nothing waiting on an answer, no delegated work or
wake-up still to come, and no turn marker. Hermes does the same for hosted room
turns (tui_gateway.session_lifecycle._release_hosted_room_turn_slot): the next
turn claims the chat again (_admit_prompt_turn), so Talaria's next send works
as before when nobody else took the chat, and meets the usual refusal and
takeover when Desktop did. Nothing running is interrupted; the runtime stays.

Only runtimes whose clients are all Talaria count: Talaria's sockets name
themselves in X-Talaria-Client. A chat Desktop or the web dashboard also has
open in this process keeps its claim, and Bot Chats keep theirs (some Hermes versions route
bot deliveries to the claim's holder).

X-Talaria-Client is the client's own claim, made by a client that already
holds a dashboard login; all it can get is its own idle claims let go sooner.
Talaria's calls run no Hermes turn and need no claim, so letting go during
one changes nothing for the call.

The gateway's helpers are read off ``tui_gateway.server`` as it loaded them;
when one this needs is missing, nothing is released.
"""

from __future__ import annotations

import importlib
import logging
import sys
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

IDLE_S = 60.0
SWEEP_S = 15.0
# Set on a runtime seen with only Talaria on it, so it still counts once that
# connection is gone (the gateway then parks it on a shared sentinel).
MARK = "_talaria_claim"
REQUIRED = ("_release_active_session_slot", "_session_pending_kind")
# Each is asked with (sid, session) or (session); a True keeps the claim.
OPTIONAL_BUSY = (("_session_has_active_delegations", True), ("_session_owns_live_wakeup_schedule", False),
                 ("_session_owns_pending_process_notification", False))


def _socket_closed(transport: Any) -> bool:
    try:
        closed = getattr(transport, "closed", None)
        if closed is None:
            closed = getattr(transport, "_closed", None)
        if callable(closed):
            closed = closed()
    except Exception:  # noqa: BLE001 — unreadable: no client to count
        return True
    return bool(closed)


def _headers(transport: Any) -> Optional[dict]:
    """The WebSocket upgrade's headers with lower-cased names, or None."""
    try:
        items = getattr(getattr(getattr(transport, "_ws", None), "headers", None), "items", None)
        if not callable(items):
            return None
        lowered: dict = {}
        for name, value in items():
            if isinstance(name, bytes):
                name = name.decode("latin-1")
            if isinstance(value, bytes):
                value = value.decode("latin-1")
            lowered.setdefault(str(name).lower(), value)
        return lowered
    except Exception:  # noqa: BLE001
        return None


def clients(server: Any, transport: Any) -> Optional[bool]:
    """True when every client on ``transport`` is Talaria, False when one
    isn't (or can't be told), None when it has no client left."""
    if transport is None or transport is getattr(server, "_detached_ws_transport", None):
        return None
    peers = getattr(transport, "transports", None)
    if callable(peers):  # Hermes' FanoutTransport: one runtime shown to several clients
        try:
            answers = [clients(server, peer) for peer in peers()]
        except Exception:  # noqa: BLE001
            return False
        live = [answer for answer in answers if answer is not None]
        return None if not live else all(live)
    socket_type = getattr(sys.modules.get("tui_gateway.ws"), "WSTransport", None)
    if not (isinstance(socket_type, type) and isinstance(transport, socket_type)) \
            and type(transport).__name__ != "WSTransport":
        return False  # stdio (a TUI), a hosted room, anything else
    if _socket_closed(transport):
        return None
    headers = _headers(transport)
    if headers is None:
        return False
    return str(headers.get("x-talaria-client") or "").strip().lower() == "talaria"


def _holds_claim(session: dict) -> Any:
    lease = session.get("active_session_lease")
    if lease is None or not getattr(lease, "enabled", False) or getattr(lease, "released", True):
        return None
    return lease


def _idle(session: dict, now: float) -> bool:
    if session.get("running") or session.get("_closing") or session.get("_finalized"):
        return False
    ready = session.get("agent_ready")
    if ready is not None and callable(getattr(ready, "is_set", None)) and not ready.is_set():
        return False
    last_active = session.get("last_active")
    if not isinstance(last_active, (int, float)) or isinstance(last_active, bool):
        return False
    return now - float(last_active) >= IDLE_S


def _turn_marked(session: dict, home: Callable[[], Any]) -> bool:
    """A turn marker under the chat's id: a turn may still be running (an
    isolated one runs in a child process). Unreadable counts as marked."""
    try:
        turn_marker = importlib.import_module("tui_gateway.turn_marker")
    except ImportError as exc:
        # Only a Hermes without the module writes no markers.
        return exc.name not in ("tui_gateway", "tui_gateway.turn_marker")
    key = str(session.get("session_key") or "")
    if not key:
        return True
    try:
        from . import chat_takeover
        root = session.get("profile_home") or home()
        return chat_takeover._owner_turn_running(turn_marker, root, {"session_id": key}, [])
    except Exception:  # noqa: BLE001 — can't tell, so keep the claim
        return True


def _busy(server: Any, sid: str, session: dict) -> bool:
    try:
        if server._session_pending_kind(sid):
            return True
        for name, with_sid in OPTIONAL_BUSY:
            helper = getattr(server, name, None)
            if callable(helper) and (helper(sid, session) if with_sid else helper(session)):
                return True
    except Exception:  # noqa: BLE001 — can't tell, so keep the claim
        return True
    return False


def releasable(server: Any, sid: str, session: Any, now: float, home: Callable[[], Any]) -> bool:
    if not isinstance(session, dict) or _holds_claim(session) is None:
        return False
    if session.get("follow_profile_config") or session.get("room_plumbing"):
        return False  # a Bot Chat or a group room's plumbing
    if str(session.get("source") or "").strip().lower() == "bot_room":
        return False
    on = clients(server, session.get("transport"))
    lock = session.get("history_lock")
    if lock is None:
        return False
    with lock:  # the mark changes only under the lock release takes
        if on is False:
            session.pop(MARK, None)
            return False
        if on is True:
            session[MARK] = True
        elif not session.get(MARK):
            return False
    if not _idle(session, now):
        return False
    return not _busy(server, sid, session) and not _turn_marked(session, home)


def release(server: Any, sid: str, session: dict, now: float) -> bool:
    """Lets go of the claim unless a turn started meanwhile (admission marks
    the turn running under the same lock; pending answers, delegations and
    wake-ups only start inside a turn) or the runtime was replaced. Hermes
    releases a hosted room's claim under this lock too."""
    lock = session.get("history_lock")
    if lock is None:
        return False
    with lock:
        lease = _holds_claim(session)
        sessions = getattr(server, "_sessions", None)
        current = sessions.get(sid) if callable(getattr(sessions, "get", None)) else None
        if lease is None or current is not session or not _idle(session, now):
            return False
        on = clients(server, session.get("transport"))  # a viewer may have joined since
        if on is False:
            session.pop(MARK, None)
            return False
        if on is None and not session.get(MARK):
            return False
        if not server._release_active_session_slot(session):
            return False
    logger.info("Talaria: let go of its idle claim on chat %r (runtime %r) so another Hermes window can open it"
                " (#1123)", getattr(lease, "session_id", None) or session.get("session_key"), sid)
    return True


def sweep(server: Any, home: Callable[[], Any], now: Optional[float] = None) -> int:
    """One pass over this process's chat runtimes; how many claims were let go."""
    if server is None or not all(callable(getattr(server, name, None)) for name in REQUIRED):
        return 0
    lock = getattr(server, "_sessions_lock", None)
    sessions = getattr(server, "_sessions", None)
    if lock is None or not callable(getattr(sessions, "items", None)):
        return 0
    now = time.time() if now is None else now
    with lock:
        runtimes = list(sessions.items())
    released = 0
    for sid, session in runtimes:
        try:
            if releasable(server, sid, session, now, home) and release(server, sid, session, now):
                released += 1
        except Exception:  # noqa: BLE001 — one odd runtime never stops the pass
            logger.debug("Talaria: idle claim check failed for %r", sid, exc_info=True)
    return released
