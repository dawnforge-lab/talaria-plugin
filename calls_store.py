"""Hermes calls you (#449): this profile's call settings and call watches.

A watch says "call the user when this turn ends". Hermes sets one through
its ``talaria_call_user`` tool, on the asking session and with the reason it
gave: because the user asked for the call, or on its own judgment. The
agent's turn-end hooks fire it: the first turn end of its session consumes
it, under the file lock, so a replayed hook finds nothing and the user is
called at most once per watch.

With "approval, question and failure calls" on, a request for the user's
approval or answer that goes unanswered for a minute calls too (an alert: a
watch that comes due after its delay unless the user answers or the turn
ends first), and so does a failed turn.

A call the user declines, or that goes unanswered, comes back from Talaria
as an outcome on the call's sessions; the next turn of one of them takes it,
so Hermes hears it once (``calls.outcome_note``).

State lives in the profile's ``talaria-calls.json`` beside the pairing state,
so watches survive gateway and dashboard restarts. The hooks (agent process)
and the dashboard routes (dashboard process) both use this module: the
dashboard loads it by path, so it imports nothing from the plugin package.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]
try:
    import msvcrt
except ImportError:  # everything but Windows
    msvcrt = None  # type: ignore[assignment]

FILE_NAME = "talaria-calls.json"
VERSION = 1

DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": False,
    # Call when the user asked for it ("call me when it's done").
    "when_asked": True,
    # Hermes may call on its own judgment (the talaria_call_user tool).
    "decides": False,
    # Call when Hermes waits on the user's approval or answer, or a turn fails.
    "alerts": False,
    "min_gap_s": 120,
    "per_hour": 6,
    "per_day": 20,
    # The user's own words on what's worth a call Hermes decides to make
    # ("only if production is down"): Hermes reads them before such a call
    # (call_tool). Empty: the skill's rule alone.
    "rules": "",
    # Saved places (Talaria's ids, location sharing) where the phone must
    # not ring: a call then sends the usual notification.
    # avoid_unknown_place: also hold calls while Talaria can't say where the
    # user is (no recent location, or sharing paused or off).
    "avoid_places": [],
    "avoid_unknown_place": False,
}
BOOL_SETTINGS = ("enabled", "when_asked", "decides", "alerts", "avoid_unknown_place")
MAX_AVOID_PLACES = 20
_PLACE_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}\Z")
MAX_RULES_CHARS = 500
# Inclusive bounds. per_day stops at the relay's own daily ceiling.
INT_BOUNDS: dict[str, tuple[int, int]] = {
    "min_gap_s": (30, 3600),
    "per_hour": (1, 30),
    "per_day": (1, 60),
}

OUTCOMES = ("done", "failed", "stopped")
# What an alert calls about: Hermes waits on the user.
ALERT_KINDS = ("approval", "question")
# Who set the watch: Hermes, through its tool, or an alert.
ORIGINS = ("tool", "alert")
# An approval or question the user hasn't answered in this long calls.
ALERT_DELAY_S = 60
MAX_REASON_CHARS = 200
WATCH_TTL_S = 24 * 3600
MAX_WATCHES = 20
MAX_WATCH_SESSIONS = 4
MAX_TITLE_CHARS = 120
# The longest an alert may wait for the user before it calls.
MAX_ALERT_DELAY_S = 600
# Call times kept for the limits: no more than the highest daily limit allows.
MAX_HISTORY = INT_BOUNDS["per_day"][1]
HOUR_S = 3600
DAY_S = 24 * 3600
# Failed turns' decisions by seeded call id, for a replayed hook: kept as long
# as the relay remembers an event id.
MAX_SEEDED = 64
# How a call that rang ended without the user answering, as Talaria reports
# it: declined, or missed (rang out, silenced by Do Not Disturb, or reached a
# phone that was offline).
CALL_OUTCOMES = ("declined", "missed")
# What a call is about (the call request's kind).
CALL_KINDS = (*OUTCOMES, *ALERT_KINDS)
# Outcomes waiting for their session's next turn: older ones say nothing
# worth saying.
OUTCOME_TTL_S = 24 * 3600
MAX_OUTCOMES = 20
# Calls whose outcome a turn has taken, so a late retry of the report isn't
# heard twice: kept as long as an outcome could still arrive.
MAX_TOLD = 64

# A Hermes session key, kept whole: one with a control character or over this
# many UTF-8 bytes is dropped, never cut. The same rule as the plugin's
# events.session_key; this module imports nothing from the plugin package.
SESSION_KEY_MAX_BYTES = 512
_SESSION_KEY_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# The shape the call event carries (events.sanitize_call).
_WATCH_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_thread_lock = threading.Lock()


class CallStore:
    def __init__(self, home: Path, clock: Callable[[], float] = time.time,
                 place_blocked: Callable[[dict[str, Any]], bool] | None = None) -> None:
        self.path = Path(home) / FILE_NAME
        self.clock = clock
        # Whether the user is at a place they don't want calls at, given the
        # settings (calls.py asks the location store; the dashboard never
        # decides calls, so it passes nothing).
        self.place_blocked = place_blocked

    # --- Settings ---------------------------------------------------------

    def settings(self) -> dict[str, Any]:
        return dict(self._load()["settings"])

    def update_settings(self, changes: Any) -> dict[str, Any]:
        if not isinstance(changes, dict):
            raise ValueError("settings must be an object")
        kept = dict(changes)
        for key, value in changes.items():
            if key in BOOL_SETTINGS:
                if not isinstance(value, bool):
                    raise ValueError(f"{key} must be true or false")
            elif key in INT_BOUNDS:
                low, high = INT_BOUNDS[key]
                if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
                    raise ValueError(f"{key} must be a whole number from {low} to {high}")
            elif key == "rules":
                rules = _rules(value)
                if rules is None:
                    raise ValueError(f"rules must be text of up to {MAX_RULES_CHARS} characters")
                kept[key] = rules
            elif key == "avoid_places":
                places = _place_ids(value)
                if places is None:
                    raise ValueError(f"avoid_places must be a list of up to {MAX_AVOID_PLACES} place ids")
                kept[key] = places
            else:
                raise ValueError(f"unknown setting {key}")
        with self._locked() as state:
            state["settings"].update(kept)
            return dict(state["settings"])

    # --- Watches ------------------------------------------------------------

    def add_tool_watch(self, session_id: Any, reason: Any, asked: bool) -> dict[str, Any]:
        """Hermes asked to call the user when this turn of ``session_id``
        ends (``asked``: because the user asked it to). ``{"status":
        "watching", "id"}``; a session already watched keeps its watch,
        taking the reason if it had none. Raises ValueError("calls_off") when
        calls, or this kind of call, are off, or ("too_many")."""
        ids = _session_ids([session_id])
        clean_reason = _reason(reason)
        with self._locked() as state:
            settings = state["settings"]
            if not settings["enabled"] or not settings["when_asked" if asked else "decides"]:
                raise ValueError("calls_off")
            now = self.clock()
            for watch in state["watches"]:
                if watch["origin"] == "tool" and ids[0] in watch["session_ids"]:
                    if not watch["reason"]:
                        watch["reason"] = clean_reason
                    watch["asked"] = watch["asked"] or asked
                    return {"status": "watching", "id": watch["id"]}
            if len(state["watches"]) >= MAX_WATCHES:
                raise ValueError("too_many")
            watch = _new_watch(ids, now, origin="tool", reason=clean_reason, asked=asked)
            state["watches"].append(watch)
            return {"status": "watching", "id": watch["id"]}

    def add_alert(self, session_id: str | list[str], kind: str, reason: Any,
                  delay_s: int = ALERT_DELAY_S) -> str | None:
        """Hermes is waiting on the user's approval or answer in
        ``session_id`` (or a list of the ids one session goes by, the first
        the one the call opens): unless it's answered within ``delay_s``
        seconds (``cancel_alerts``), the waiter calls. The alert's id, or None
        when alerts are off (nothing is written then) or one is already
        waiting. An id that isn't a session key is left out, so the others
        still call."""
        if kind not in ALERT_KINDS:
            raise ValueError(f"unknown alert {kind}")
        named = session_id if isinstance(session_id, list) else [session_id]
        ids = _session_ids([value for value in named if isinstance(value, str) and _session_key(value.strip())])
        delay = _seconds(delay_s, "delay_s", MAX_ALERT_DELAY_S)
        settings = self.settings()
        if not settings["enabled"] or not settings["alerts"]:
            return None
        with self._locked() as state:
            if not state["settings"]["enabled"] or not state["settings"]["alerts"]:
                return None
            # The same wait, whichever of its ids a replayed hook names.
            if any(watch["origin"] == "alert" and set(watch["session_ids"]) & set(ids)
                   and watch["pending"]["outcome"] == kind for watch in state["watches"]):
                return None
            if len(state["watches"]) >= MAX_WATCHES:
                return None
            now = self.clock()
            watch = _new_watch(ids, now, origin="alert", reason=_reason(reason), hold_until=now + delay)
            watch["pending"] = {"outcome": kind, "session_id": ids[0]}
            state["watches"].append(watch)
            return watch["id"]

    def cancel_alerts(self, session_id: Any, kind: str | None = None) -> int:
        """The user answered (or the turn moved on): ``session_id``'s waiting
        alerts, of ``kind`` or all, call no more. How many were removed."""
        if not isinstance(session_id, str) or not session_id.strip() or not self.path.exists():
            return 0
        session_id = session_id.strip()

        def cancelled(watch: dict[str, Any]) -> bool:
            return (watch["origin"] == "alert" and session_id in watch["session_ids"]
                    and (kind is None or watch["pending"]["outcome"] == kind))

        if not any(cancelled(watch) for watch in self._load()["watches"]):
            return 0
        with self._locked() as state:
            kept = [watch for watch in state["watches"] if not cancelled(watch)]
            removed = len(state["watches"]) - len(kept)
            state["watches"] = kept
            return removed

    def alert_now(self, session_id: Any, kind: str, reason: Any, *, seed: str = "") -> dict[str, Any] | None:
        """A failed turn with alerts on: the result ``fire`` would give for a
        watch on it (``call`` or ``limited``), counted against the
        limits. None when alerts are off: nothing is written then. ``seed``
        (the turn) fixes the call's id, so a replayed hook is the same call
        to the relay, which rings it once, and gets the first hook's
        decision without counting again."""
        ids = _session_ids([session_id])
        settings = self.settings()
        if not settings["enabled"] or not settings["alerts"]:
            return None
        with self._locked() as state:
            now = self.clock()
            watch = _new_watch(ids, now, origin="alert", reason=_reason(reason))
            if not seed:
                return _decide(state, watch, kind, now, self.place_blocked)
            watch["id"] = hashlib.sha256("\0".join([kind, *ids, seed]).encode()).hexdigest()[:24]
            earlier = next((entry for entry in state["seeded"] if entry["id"] == watch["id"]), None)
            if earlier is not None:
                # Not held back by the gap the first call opened: that would
                # send the failure notification beside its ring.
                return {"watch": watch, "outcome": kind, "status": earlier["status"]}
            result = _decide(state, watch, kind, now, self.place_blocked)
            state["seeded"] = [*state["seeded"], {"id": watch["id"], "status": result["status"], "at": now}][-MAX_SEEDED:]
            return result

    # --- Outcomes -----------------------------------------------------------

    def record_outcome(self, call_id: Any, session_ids: Any, outcome: Any, *, kind: Any = None, title: Any = "",
                       reason: Any = "", age_s: Any = 0) -> dict[str, Any]:
        """The user declined call ``call_id`` or didn't answer it, ``age_s``
        seconds ago (on the phone's clock, so the host's never matters):
        kept until the next turn of one of ``session_ids`` takes it. A
        retried report keeps the first. ``{"status": "recorded"}``; raises
        ValueError for anything malformed."""
        if not isinstance(call_id, str) or not _WATCH_ID.fullmatch(call_id):
            raise ValueError("call_id must be the call's id")
        ids = _session_ids(session_ids)
        if outcome not in CALL_OUTCOMES:
            raise ValueError("outcome must be declined or missed")
        # Below the TTL: one exactly a day old would be dropped unheard.
        age = _seconds(age_s, "age_s", OUTCOME_TTL_S - 1)
        with self._locked() as state:
            known = {entry["id"] for entry in state["outcomes"]} | {entry["id"] for entry in state["told"]}
            if call_id not in known:
                state["outcomes"] = [*state["outcomes"], {
                    "id": call_id,
                    "session_ids": ids,
                    "outcome": outcome,
                    # A kind a newer Talaria knows reads as none.
                    "kind": kind if kind in CALL_KINDS else None,
                    "title": _title(title),
                    "reason": _reason(reason),
                    "at": self.clock() - age,
                }][-MAX_OUTCOMES:]
            return {"status": "recorded"}

    def take_outcomes(self, session_id: Any) -> list[dict[str, Any]]:
        """The outcomes waiting for a turn of ``session_id``, oldest first:
        taken, so the next turn doesn't hear them again, nor a late retry of
        their report."""
        if not isinstance(session_id, str) or not session_id.strip() or not self.path.exists():
            return []
        session_id = session_id.strip()
        # Every turn asks: only one with something to take writes.
        if not any(session_id in entry["session_ids"] for entry in self._load()["outcomes"]):
            return []
        with self._locked() as state:
            taken = sorted((entry for entry in state["outcomes"] if session_id in entry["session_ids"]),
                           key=lambda entry: entry["at"])
            state["outcomes"] = [entry for entry in state["outcomes"] if session_id not in entry["session_ids"]]
            now = self.clock()
            state["told"] = [*state["told"], *({"id": entry["id"], "at": now} for entry in taken)][-MAX_TOLD:]
            return taken

    def watch_count(self) -> int:
        now = self.clock()
        return sum(1 for watch in self._load()["watches"] if watch["expires_at"] > now)

    # --- Firing -------------------------------------------------------------

    def fire(self, session_id: Any, outcome: str) -> dict[str, Any] | None:
        """A turn of ``session_id`` ended with ``outcome``.

        None when no watch covers the session (its waiting alerts are
        dropped: the turn no longer waits on the user). Otherwise the watch
        is consumed and the result says what to do: ``call`` (counted
        against the limits), ``limited`` (with ``reason`` place, gap, hour or
        day) or ``off`` (calls, or this kind of call, were turned off after
        the watch was set).
        """
        if outcome not in OUTCOMES:
            raise ValueError(f"unknown outcome {outcome}")
        if not isinstance(session_id, str) or not session_id.strip() or not self.path.exists():
            return None
        session_id = session_id.strip()
        with self._locked() as state:
            now = self.clock()
            # The turn moved on: nothing it waited on calls any more.
            state["watches"] = [watch for watch in state["watches"]
                                if not (watch["origin"] == "alert" and session_id in watch["session_ids"])]
            index = next((i for i, watch in enumerate(state["watches"]) if session_id in watch["session_ids"]), None)
            if index is None:
                return None
            watch = state["watches"].pop(index)
            return _decide(state, watch, outcome, now, self.place_blocked)

    def fire_due(self) -> list[dict[str, Any]]:
        """Watches that have come due (alerts left unanswered past their
        delay): consumed, each with the result ``fire`` would give, plus the
        ``session_id`` the call opens."""
        if not self.path.exists():
            return []
        with self._locked() as state:
            now = self.clock()
            due = [watch for watch in state["watches"] if watch["pending"] is not None and watch["hold_until"] <= now]
            if not due:
                return []
            due_ids = {watch["id"] for watch in due}
            state["watches"] = [watch for watch in state["watches"] if watch["id"] not in due_ids]
            return [{**_decide(state, watch, watch["pending"]["outcome"], now, self.place_blocked),
                     "session_id": watch["pending"]["session_id"]} for watch in due]

    def next_due(self) -> float | None:
        """When the earliest watch comes due (an alert's delay runs out), if
        any."""
        due = [watch["hold_until"] for watch in self._load()["watches"] if watch["pending"] is not None]
        return min(due) if due else None

    # --- Storage ------------------------------------------------------------

    def _load(self, *, for_write: bool = False) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, RecursionError):
            raw = None
        except OSError:
            # Unreadable for now (not missing, not corrupt): a write would
            # replace the watches it couldn't read, so it fails instead.
            if for_write:
                raise
            raw = None
        return _normalized(raw, self.clock())

    @contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        """Load, let the caller change, then save: all under the file lock."""
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        with _thread_lock, open(lock_path, "a+b") as handle, _exclusive(handle):
            state = self._load(for_write=True)
            before = _encoded(state)
            yield state
            # Most turn ends change nothing: no write then.
            after = _encoded(state)
            if after != before or not self.path.exists():
                self._save(after)

    def _save(self, encoded: str) -> None:
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        # Private from the first byte: it holds call reasons and session ids.
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encoded)
        temporary.chmod(0o600)
        temporary.replace(self.path)
        self.path.chmod(0o600)


def _encoded(state: dict[str, Any]) -> str:
    return json.dumps(state, separators=(",", ":")) + "\n"


def _new_watch(ids: list[str], now: float, *, origin: str, reason: str = "", asked: bool = False,
               hold_until: float = 0.0) -> dict[str, Any]:
    return {
        "id": secrets.token_hex(12),
        "session_ids": ids,
        # Only a watch an older plugin kept names its job, so its call still
        # says what it was about.
        "title": "",
        "created_at": now,
        "expires_at": now + WATCH_TTL_S,
        # When a pending watch comes due (an alert's delay). The stored name
        # is older than alerts: kept so older files still load.
        "hold_until": hold_until,
        "pending": None,
        "origin": origin,
        "reason": reason,
        "asked": asked,
    }


def _allowed(settings: dict[str, Any], watch: dict[str, Any]) -> bool:
    """Whether the user's settings still allow this kind of call."""
    if not settings["enabled"]:
        return False
    if watch["origin"] == "alert":
        return settings["alerts"]
    return settings["when_asked"] if watch["asked"] else settings["decides"]


def _decide(state: dict[str, Any], watch: dict[str, Any], outcome: str, now: float,
            place_blocked: Callable[[dict[str, Any]], bool] | None = None) -> dict[str, Any]:
    """Whether a consumed watch calls, within the user's settings and limits.
    A call counts against them once decided, even if its delivery later fails
    (the usual push goes out then): erring towards fewer calls, never more."""
    settings = state["settings"]
    result: dict[str, Any] = {"watch": watch, "outcome": outcome}
    if not _allowed(settings, watch):
        return {**result, "status": "off"}
    if place_blocked is not None and (settings["avoid_places"] or settings["avoid_unknown_place"]):
        try:
            blocked = place_blocked(settings)
        except Exception:  # noqa: BLE001 — a broken location store never stops a call
            blocked = False
        if blocked:
            # Not counted against the limits: nothing rang.
            return {**result, "status": "limited", "reason": "place"}
    history = state["history"]
    if history and now - max(history) < settings["min_gap_s"]:
        return {**result, "status": "limited", "reason": "gap"}
    if sum(1 for at in history if now - at < HOUR_S) >= settings["per_hour"]:
        return {**result, "status": "limited", "reason": "hour"}
    if sum(1 for at in history if now - at < DAY_S) >= settings["per_day"]:
        return {**result, "status": "limited", "reason": "day"}
    history.append(now)
    return {**result, "status": "call"}


def _normalized(raw: Any, now: float) -> dict[str, Any]:
    """A well-formed state from whatever is on disk, with expired entries
    dropped. Anything malformed reads as its default."""
    raw = raw if isinstance(raw, dict) else {}
    settings = dict(DEFAULT_SETTINGS)
    stored = raw.get("settings") if isinstance(raw.get("settings"), dict) else {}
    for key in BOOL_SETTINGS:
        if isinstance(stored.get(key), bool):
            settings[key] = stored[key]
    for key, (low, high) in INT_BOUNDS.items():
        value = stored.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high:
            settings[key] = value
    settings["rules"] = _rules(stored.get("rules")) or ""
    settings["avoid_places"] = _place_ids(stored.get("avoid_places")) or []
    watches = []
    for watch in raw.get("watches") if isinstance(raw.get("watches"), list) else []:
        try:
            if watch["expires_at"] > now and _WATCH_ID.match(str(watch["id"])):
                origin = watch.get("origin")
                asked = watch.get("asked") is True
                if origin not in ORIGINS:
                    # An older plugin's watch, which the phone registered in a
                    # Live Voice call (origin "talaria", or none before origins
                    # were kept): the user asked for that call, so it still
                    # rings with "call when I ask" on. Any other origin is one
                    # this plugin doesn't know: a watch Hermes set, asked only
                    # if it says so.
                    asked = asked or origin in (None, "talaria")
                    origin = "tool"
                pending = watch.get("pending")
                kinds = ALERT_KINDS if origin == "alert" else OUTCOMES
                if not (isinstance(pending, dict) and pending.get("outcome") in kinds
                        and isinstance(pending.get("session_id"), str)):
                    pending = None
                if origin == "alert" and pending is None:
                    continue
                hold_until = watch.get("hold_until", 0.0)
                watches.append({
                    "id": str(watch["id"]),
                    "session_ids": _session_ids(watch["session_ids"]),
                    "title": _title(watch.get("title")),
                    "created_at": float(watch["created_at"]),
                    "expires_at": float(watch["expires_at"]),
                    "hold_until": float(hold_until) if isinstance(hold_until, (int, float)) and not isinstance(hold_until, bool) else 0.0,
                    "pending": {"outcome": pending["outcome"], "session_id": pending["session_id"]} if pending else None,
                    "origin": origin,
                    "reason": _reason(watch.get("reason")),
                    "asked": asked,
                })
        except (KeyError, TypeError, ValueError):
            continue
    stored_history = raw.get("history") if isinstance(raw.get("history"), list) else []
    history = sorted(float(at) for at in stored_history
                     if isinstance(at, (int, float)) and not isinstance(at, bool) and now - at < DAY_S)
    history = history[-MAX_HISTORY:]
    seeded = []
    for entry in raw.get("seeded") if isinstance(raw.get("seeded"), list) else []:
        if (isinstance(entry, dict) and isinstance(entry.get("id"), str) and _WATCH_ID.match(entry["id"])
                and isinstance(entry.get("status"), str) and isinstance(entry.get("at"), (int, float))
                and not isinstance(entry.get("at"), bool) and now - entry["at"] < DAY_S):
            seeded.append({"id": entry["id"], "status": entry["status"], "at": float(entry["at"])})
    outcomes = []
    for entry in raw.get("outcomes") if isinstance(raw.get("outcomes"), list) else []:
        try:
            at = entry["at"]
            if (isinstance(at, (int, float)) and not isinstance(at, bool) and now - at < OUTCOME_TTL_S
                    and _WATCH_ID.match(str(entry["id"])) and entry["outcome"] in CALL_OUTCOMES):
                outcomes.append({
                    "id": str(entry["id"]),
                    "session_ids": _session_ids(entry["session_ids"]),
                    "outcome": entry["outcome"],
                    "kind": entry.get("kind") if entry.get("kind") in CALL_KINDS else None,
                    "title": _title(entry.get("title")),
                    "reason": _reason(entry.get("reason")),
                    "at": float(at),
                })
        except (KeyError, TypeError, ValueError):
            continue
    told = []
    for entry in raw.get("told") if isinstance(raw.get("told"), list) else []:
        if (isinstance(entry, dict) and isinstance(entry.get("id"), str) and _WATCH_ID.fullmatch(entry["id"])
                and isinstance(entry.get("at"), (int, float)) and not isinstance(entry.get("at"), bool)
                and now - entry["at"] < OUTCOME_TTL_S):
            told.append({"id": entry["id"], "at": float(entry["at"])})
    # An older plugin's "ends" (recent turn ends, for watches the phone
    # registered) isn't read: the next write drops it.
    return {"v": VERSION, "settings": settings, "watches": watches, "history": history,
            "seeded": seeded[-MAX_SEEDED:], "outcomes": outcomes[-MAX_OUTCOMES:], "told": told[-MAX_TOLD:]}


def _session_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("session_ids must be a list")
    ids: list[str] = []
    for item in value:
        if not isinstance(item, str) or not _session_key(item.strip()):
            raise ValueError("session_ids must be session ids")
        if item.strip() not in ids:
            ids.append(item.strip())
    if not ids:
        raise ValueError("session_ids must name the job's session")
    return ids[:MAX_WATCH_SESSIONS]


def _session_key(value: Any) -> str:
    """``value`` unchanged when it is a session key, else "" (events.session_key)."""
    if not isinstance(value, str) or not value or _SESSION_KEY_CONTROL.search(value):
        return ""
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:  # a lone surrogate
        return ""
    return value if size <= SESSION_KEY_MAX_BYTES else ""


def _seconds(value: Any, name: str, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= maximum:
        raise ValueError(f"{name} must be a whole number of seconds from 0 to {maximum}")
    return value


def _title(value: Any) -> str:
    text = " ".join(value.split()) if isinstance(value, str) else ""
    return text[:MAX_TITLE_CHARS]


def _reason(value: Any) -> str:
    text = " ".join(value.split()) if isinstance(value, str) else ""
    return text[:MAX_REASON_CHARS]


def _rules(value: Any) -> str | None:
    """The user's call rules as kept: lines trimmed, blank lines and control
    characters dropped. None when ``value`` isn't text or is too long."""
    if not isinstance(value, str):
        return None
    lines = ("".join(char for char in line if char.isprintable()).strip() for line in value.splitlines())
    text = "\n".join(line for line in lines if line)
    return text if len(text) <= MAX_RULES_CHARS else None


def _place_ids(value: Any) -> list[str] | None:
    """Saved place ids, deduplicated in order. None when malformed."""
    if not isinstance(value, list) or len(value) > MAX_AVOID_PLACES:
        return None
    if any(not isinstance(item, str) or not _PLACE_ID.match(item) for item in value):
        return None
    return list(dict.fromkeys(value))


@contextmanager
def _exclusive(handle: Any) -> Iterator[None]:
    # The same locking as client.state_file_lock: flock where there is one,
    # a first-byte lock on Windows.
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    elif msvcrt is not None:
        handle.seek(0)
        for attempt in range(6):  # about a minute
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                break
            except OSError:
                if attempt == 5:
                    raise
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        yield
