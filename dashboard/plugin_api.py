"""Talaria routes on the Hermes dashboard, mounted at /api/plugins/talaria_push/.

Pairing, end-to-end encrypted notifications, chat takeover and Desktop
views, notification and cron settings, calls from Hermes, the confirm
prompts and settings Hermes shows on the phone, the health, location and
phone data Talaria shares, group room files and pictures, artifacts, and
the dashboard's own restart.

Routes sit behind the dashboard's own auth, the same as /api/audio/*.
Credentials and keys are never returned, logged, or written anywhere else.
"""

from __future__ import annotations

import asyncio
import atexit
import base64
import errno
import functools
import hashlib
import http.client
import importlib
import importlib.util
import inspect
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import uuid
import types
import urllib.error
import urllib.parse
import urllib.request
import weakref
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import contextlib
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

from fastapi import APIRouter, HTTPException, Query, Request, Response

logger = logging.getLogger(__name__)

router = APIRouter()

# --- Shared by the routes ---------------------------------------------------

# Keys one limiter tracks at once. Keys are profile names a scope accepted,
# so this only bites a host with that many profiles busy in one window.
LIMITER_MAX_KEYS = 256


class TokenError(Exception):
    """A token request failed. ``status`` is the HTTP status to return to Talaria;
    ``code``, when set, is the store's short machine word for the refusal."""

    def __init__(self, status: int, message: str, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class _MintLimiter:
    """Sliding-window cap per profile."""

    def __init__(
        self,
        limit: int,
        window_s: float,
        clock: Callable[[], float] = time.monotonic,
        message: str = "Too many requests; try again shortly",
        max_keys: int = LIMITER_MAX_KEYS,
    ) -> None:
        self.limit = limit
        self.message = message
        self.window_s = window_s
        self.clock = clock
        self.max_keys = max_keys
        self._mints: Dict[str, deque] = {}
        self._lock = threading.Lock()

    def acquire(self, key: str, limit: Optional[int] = None) -> float:
        """Takes a slot and returns its mint (for ``release``). ``limit`` overrides
        the cap for this call (a cap read from the profile's config)."""
        limit = self.limit if limit is None else limit
        now = self.clock()
        with self._lock:
            # Drop buckets whose newest mint has aged out (or that are empty), so idle keys don't pile up.
            for stale in [k for k, q in self._mints.items() if not q or now - q[-1] >= self.window_s]:
                del self._mints[stale]
            if limit <= 0:
                raise TokenError(429, self.message)
            # A full map turns new keys away, so it stays bounded whatever
            # callers send; the dashboard's own profile ("") always fits.
            if key and key not in self._mints and len(self._mints) >= self.max_keys:
                raise TokenError(429, self.message)
            mints = self._mints.setdefault(key, deque())
            while mints and now - mints[0] >= self.window_s:
                mints.popleft()
            if len(mints) >= limit:
                raise TokenError(429, self.message)
            mints.append(now)
            return now

    def release(self, key: str, mint: float) -> None:
        """Gives back the slot ``acquire`` returned, for a request that failed
        before it cost anything. The other mints keep their own times."""
        with self._lock:
            mints = self._mints.get(key)
            if not mints:
                return
            try:
                mints.remove(mint)
            except ValueError:
                return  # already aged out
            if not mints:
                del self._mints[key]


def _limiter_key(profile: Optional[str]) -> str:
    # Same notion of "the dashboard's own profile" as Hermes' scope, so
    # ?profile=current / " " / "Current" can't each get a fresh window.
    name = (profile or "").strip().lower()
    return "" if name in ("", "current") else name


def _profile_scope(profile: Optional[str]):
    """Resolve .env/config for ``profile`` the way /api/audio/* does.

    Always enters Hermes' scope, even with no profile: once the dashboard has
    served any ``?profile=`` request it hosts several profiles, and an unscoped
    secret read then raises instead of reading the dashboard's own .env.
    Fails closed: a requested profile that can't be scoped must never fall back
    to the default profile's key.
    """
    try:
        from hermes_cli.web_server_profiles import _config_profile_scope
    except ImportError as exc:
        # Only a missing Hermes or profile module (tests, or a Hermes without
        # profiles, including a renamed private scope helper) falls back; a broken
        # import deeper inside Hermes must not silently read unscoped.
        if exc.name not in ("hermes_cli", "hermes_cli.web_server_profiles"):
            raise
        if not profile:
            return nullcontext()
        logger.warning("Cannot scope Talaria request to profile %r: profile scoping unavailable", profile)
        raise TokenError(503, "This Hermes version can't resolve per-profile keys")
    return _config_profile_scope(profile or None)


_Scoped = TypeVar("_Scoped")


async def _run_scoped(
    profile: Optional[str],
    fn: Callable[[], _Scoped],
    executor: Optional[ThreadPoolExecutor] = None,
) -> _Scoped:
    def scoped() -> _Scoped:
        with _profile_scope(profile):
            return fn()

    return await asyncio.get_running_loop().run_in_executor(executor, scoped)


def _limited(limiter: "_MintLimiter", profile: Optional[str], fn: Callable[[], _Scoped]) -> Callable[[], _Scoped]:
    """``fn`` behind a slot of ``limiter``, for ``_run_scoped``: the slot is
    taken inside the scope, so a ?profile= the scope refuses gets no bucket."""
    def run() -> _Scoped:
        limiter.acquire(_limiter_key(profile))
        return fn()

    return run


async def _scoped_acquire(limiter: "_MintLimiter", profile: Optional[str]) -> None:
    """A slot of ``limiter`` taken before a route reads its body, once
    Hermes' scope has accepted ``profile`` (an unknown one is refused there
    and never gets a bucket)."""
    try:
        await _run_scoped(profile, lambda: limiter.acquire(_limiter_key(profile)))
    except TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        # A broken scope is the host's failure, named like the routes' own.
        raise _unexpected("profile check", exc, feature="Talaria")


def _unexpected(route: str, exc: Exception, feature: str = "Talaria") -> HTTPException:
    # Name the failure so Talaria shows something more useful than a bare 500;
    # the message itself stays in the log since it can carry host details.
    logger.exception("%s %s route failed", feature, route)
    return HTTPException(status_code=500, detail=f"{feature} {route} failed on the host ({type(exc).__name__})",
                         headers={"Cache-Control": "no-store"})


async def _read_json_body(request: Request, max_bytes: int = 16 * 1024) -> Any:
    no_store = {"Cache-Control": "no-store"}
    # Read with a hard cap, chunked bodies included, so an oversized body is
    # refused without buffering it.
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > max_bytes:
        raise HTTPException(status_code=413, detail="Request body is too large", headers=no_store)
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > max_bytes:
            raise HTTPException(status_code=413, detail="Request body is too large", headers=no_store)
    try:
        if len(raw) > 1_000_000:
            # A file upload's body: parse it off the event loop.
            return await asyncio.to_thread(json.loads, bytes(raw))
        return json.loads(bytes(raw) or b"{}")
    except (ValueError, RecursionError):
        raise HTTPException(status_code=400, detail="Request body is not JSON", headers=no_store)



# --- Chat takeover -----------------------------------------------------------
#
# Hermes lets one live surface own a chat at a time (hermes_cli.active_sessions,
# the runtime/active_sessions.json registry). Hermes Desktop claims a chat on
# its first turn and keeps the claim until the chat is closed there, so the
# same chat refuses Talaria's next send with SESSION_NOT_OWNED. This route is
# the "Take over" Talaria offers on that refusal (#304): it drops the other
# surface's claim, under the registry's own lock, so the next send claims the
# chat for Talaria. A turn the other surface is running is never cut off: its
# turn marker (tui_gateway.turn_marker) answers "busy" and Talaria asks again.
#
# Only another process's claim is dropped. A claim held by this dashboard
# process (Talaria on another device, or the web chat) is left alone: Hermes
# already hands those over itself once their client is gone. "This process"
# is its pid: Hermes serves the dashboard as one process (its chat runtimes
# and turn leases live in that process's memory).
#
# The owner writes its turn marker without the registry lock, so a turn it
# starts in the instant between the marker check and the write can still lose
# its claim. That turn keeps running in the owner (nothing is interrupted);
# it is the same exposure as the known limit that the owner isn't told.
#
# Only Desktop and TUI turns write a turn marker. A live claim from any other
# surface (a CLI or kanban worker, a gateway, or a blank one) has no marker
# to read, so it could look idle mid-turn: it is never dropped and answers
# "busy" (#97).
#
# A timed-out request is abandoned: the worker thread can't be cancelled, but
# it checks the abandon flag once it holds the lock and again right before it
# writes, so a 504 almost never drops a claim after the fact (only a write
# already under way when the timeout fires still lands). Talaria retries a
# 504, and asking again reports the real state.

TAKEOVER_MAX_BODY_BYTES = 4096
TAKEOVER_MAX_IDS = 4
TAKEOVER_MAX_ID_CHARS = 200
TAKEOVER_LIMIT = 60
TAKEOVER_WINDOW_S = 60.0
TAKEOVER_TIMEOUT_S = 20.0
_takeover_limiter = _MintLimiter(TAKEOVER_LIMIT, TAKEOVER_WINDOW_S,
                                 message="Too many takeover requests; try again shortly")
_takeover_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="talaria-takeover")


def _takeover_ids(body: Any) -> list:
    raw = body.get("session_ids") if isinstance(body, dict) else None
    if not isinstance(raw, list) or not raw or len(raw) > TAKEOVER_MAX_IDS:
        raise TokenError(400, f"session_ids must list 1 to {TAKEOVER_MAX_IDS} chat ids")
    ids = []
    for value in raw:
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > TAKEOVER_MAX_ID_CHARS:
            raise TokenError(400, f"session_ids must be non-empty strings of at most {TAKEOVER_MAX_ID_CHARS} characters")
        if value.strip() not in ids:
            ids.append(value.strip())
    return ids


def _takeover() -> Any:
    return _plugin_package_module("chat_takeover")


def _takeover_call(call: Callable[[Any], Any]) -> Any:
    module = _takeover()
    try:
        return call(module)
    except module.TakeoverError as exc:
        raise TokenError(exc.status, str(exc)) from None


def _takeover_modules() -> Tuple[Any, Any]:
    return _takeover_call(lambda module: module.modules())


def _takeover_home() -> Any:
    return _takeover_call(lambda module: module.home())


def _owner_pid(entry: Dict[str, Any]) -> int:
    return _takeover()._owner_pid(entry)


def take_over_session(session_ids: list, *, registry: Any, turn_marker: Any, home: Any,
                      own_pid: Optional[int] = None,
                      abandoned: Optional[threading.Event] = None) -> Dict[str, Any]:
    """chat_takeover.take_over_session, its refusals as TokenError."""
    return _takeover_call(lambda module: module.take_over_session(
        session_ids, registry=registry, turn_marker=turn_marker, home=home, own_pid=own_pid, abandoned=abandoned))


def _take_over_scoped(session_ids: list, abandoned: Optional[threading.Event] = None) -> Dict[str, Any]:
    registry, turn_marker = _takeover_modules()
    return take_over_session(session_ids, registry=registry, turn_marker=turn_marker, home=_takeover_home(),
                             abandoned=abandoned)


@router.post("/sessions/takeover")
async def post_session_takeover(request: Request, response: Response,
                                profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    no_store = {"Cache-Control": "no-store"}
    try:
        body = await _read_json_body(request, TAKEOVER_MAX_BODY_BYTES)
        ids = _takeover_ids(body)
        _ensure_claims_sweeper()
        # One budget for the whole dashboard: the profile isn't resolved
        # yet, so a made-up name can't mint a window.
        _takeover_limiter.acquire("")
        abandoned = threading.Event()
        try:
            result = await asyncio.wait_for(
                _run_scoped(profile, lambda: _take_over_scoped(ids, abandoned), _takeover_executor),
                timeout=TAKEOVER_TIMEOUT_S)
        except asyncio.TimeoutError:
            abandoned.set()
            raise
    except asyncio.TimeoutError:
        logger.warning("Chat takeover timed out after %ss", TAKEOVER_TIMEOUT_S)
        raise HTTPException(status_code=504, detail="Chat takeover timed out", headers=no_store)
    except TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers=no_store)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), **no_store}
        raise
    except Exception as exc:
        raise _unexpected("takeover", exc, feature="Chat takeover")
    return {"ok": True, **result}


def _hermes_session_api() -> Any:
    """Hermes' session API module when it serves this process's chats, else None.

    Only a module the dashboard itself loaded counts: importing it here (a
    plugin host process, an older Hermes) would make a second, private
    registry whose chats the app never sees.
    """
    server = sys.modules.get("tui_gateway.server")
    if server is None or not callable(getattr(server, "dispatch", None)):
        return None
    sessions = getattr(server, "_sessions", None)
    return server if sessions is not None and callable(getattr(sessions, "get", None)) else None


# The reverse direction: a chat Talaria opened here keeps
# its claim while Talaria stays connected, so Desktop's own process is refused
# it long after Talaria's turn ended. A sweep lets go of Talaria's claims on
# chats that sit idle; talaria_claims.py explains when. One sweeper per
# process: a reloaded copy of this module takes over from the old one.
_CLAIMS_SWEEPER = "_talaria_idle_claims_sweeper"


def _claims_home() -> Any:
    from hermes_constants import get_hermes_home

    return get_hermes_home()


def _sweep_idle_claims(token: object) -> None:
    while sys.__dict__.get(_CLAIMS_SWEEPER) is token:
        try:
            claims = _plugin_package_module("talaria_claims")
            time.sleep(claims.SWEEP_S)
            if sys.__dict__.get(_CLAIMS_SWEEPER) is token:
                claims.sweep(_hermes_session_api(), _claims_home)
        except Exception:  # noqa: BLE001 — keep sweeping
            logger.debug("Talaria: idle claim sweep failed", exc_info=True)
            time.sleep(60.0)


def _start_claims_sweeper() -> None:
    token = object()
    sys.__dict__[_CLAIMS_SWEEPER] = token
    try:
        threading.Thread(target=_sweep_idle_claims, args=(token,), name="talaria-idle-claims", daemon=True).start()
    except RuntimeError:
        sys.__dict__.pop(_CLAIMS_SWEEPER, None)  # the next takeover tries again
        logger.warning("Talaria: no thread to let go of idle chat claims")


def _ensure_claims_sweeper() -> None:
    """Starts the sweeper unless one runs: a takeover means Talaria chats here."""
    if sys.__dict__.get(_CLAIMS_SWEEPER) is None:
        _start_claims_sweeper()


if "hermes_cli.web_server" in sys.modules:
    _start_claims_sweeper()


# --- End-to-end encrypted notifications (#431) -------------------------------
#
# Talaria creates a per-pairing secret on the phone and hands it to this
# profile here, over the dashboard connection, so it never passes through the
# push relay. The hooks (client.py) read it from the profile's
# talaria-push.json, seal every notification with it and only accept clarify
# answers sealed by the phone. GET never returns the secret.

E2E_LIMIT = 10
E2E_WINDOW_S = 60.0
E2E_MAX_BODY_BYTES = 4 * 1024
_E2E_KID = re.compile(r"^[0-9a-f]{32}\Z")
_E2E_B64URL = re.compile(r"^[A-Za-z0-9_-]{43}\Z")  # 32 bytes, unpadded
_e2e_limiter = _MintLimiter(E2E_LIMIT, E2E_WINDOW_S, message="Too many encryption key requests; try again shortly")
_e2e_lock = threading.Lock()


def _pairing_state_path() -> Any:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "talaria-push.json"


def _load_pairing_state(path: Any) -> Optional[Dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return None
    if not isinstance(value, dict) or not value.get("credential"):
        return None
    return value


def _pairing_state_lock_path(path: Any) -> Any:
    return path.with_name(f".{path.name}.lock")


@contextlib.contextmanager
def _pairing_state_lock(path: Any):
    # The same lock file client.state_file_lock takes in the agent process,
    # so a hook rewriting the state can't drop a key stored here (or the
    # other way round).
    with open(_pairing_state_lock_path(path), "a+b") as handle, _locked_handle(handle):
        yield


def _open_private_lock_file(lock_path: str) -> Any:
    """A lock file opened owner-only and never through a symlink."""
    flags = os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(lock_path, flags, 0o600)
    try:
        return os.fdopen(fd, "a+b")
    except BaseException:
        os.close(fd)
        raise


@contextlib.contextmanager
def _locked_handle(handle: Any):
    """flock on ``handle``, or a first-byte lock on Windows."""
    try:
        import fcntl
    except ImportError:
        fcntl = None
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    try:
        import msvcrt
    except ImportError:
        yield
        return
    # Windows: the same first-byte lock client.state_file_lock takes.
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


def _save_pairing_state(path: Any, value: Dict[str, Any]) -> None:
    # Same atomic, owner-only write as client.save_state.
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    # Owner-only from creation: the file holds the relay credential and the
    # pairing secret, so it's never readable by others, even for a moment
    # or if the process dies before the rename.
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(value, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.chmod(0o600)
    temporary.replace(path)
    path.chmod(0o600)


def _e2e_crypto_available() -> bool:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305  # noqa: F401
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF  # noqa: F401
    except Exception:
        return False
    return True


def e2e_status(path: Any = None) -> Dict[str, Any]:
    """This profile's pairing and key state. Never includes the secret."""
    state = _load_pairing_state(path if path is not None else _pairing_state_path())
    crypto = _e2e_crypto_available()
    if not state:
        return {"paired": False, "crypto": crypto}
    record = state.get("e2e")
    kid = record.get("kid") if isinstance(record, dict) else None
    secret = record.get("secret") if isinstance(record, dict) else None
    # A key whose secret is unusable reads as none, so the phone replaces it.
    if not isinstance(secret, str) or not _E2E_B64URL.match(secret):
        kid = None
    return {
        "paired": True,
        "installation_id": state.get("installation_id"),
        "gateway_id": state.get("gateway_id"),
        "e2e": {"kid": kid} if isinstance(kid, str) and _E2E_KID.match(kid) else None,
        "crypto": crypto,
        # The relay answered 401 to this pairing's last delivery (client.py):
        # it no longer knows this gateway, so the phone should pair again.
        "relay_rejected": bool(state.get("relay_rejected_at")),
        # Something in front of the relay answered a bare 403 (client.py):
        # this host is blocked there, and pairing again won't get through.
        "relay_blocked": bool(state.get("relay_blocked_at")),
    }


def provision_e2e(body: Any, path: Any = None) -> Dict[str, Any]:
    """Stores the phone's pairing secret for this profile.

    Only for the pairing the phone owns: the installation and gateway ids
    must match this profile's pairing, so a key for another phone (or an old
    pairing) is refused. Replaces an earlier key for the same pairing.
    """
    if not isinstance(body, dict):
        raise TokenError(400, "Expected a JSON object")
    installation_id = body.get("installation_id")
    gateway_id = body.get("gateway_id")
    kid = body.get("kid")
    secret = body.get("secret")
    if not isinstance(installation_id, str) or not installation_id or not isinstance(gateway_id, str) or not gateway_id:
        raise TokenError(400, "installation_id and gateway_id are required")
    if not isinstance(kid, str) or not _E2E_KID.match(kid):
        raise TokenError(400, "kid must be 32 lowercase hex characters")
    if not isinstance(secret, str) or not _E2E_B64URL.match(secret):
        raise TokenError(400, "secret must be 32 bytes, base64url without padding")
    if not _e2e_crypto_available():
        raise TokenError(501, "Encrypted notifications need the cryptography package on this host")
    path = path if path is not None else _pairing_state_path()
    with _e2e_lock, _pairing_state_lock(path):
        state = _load_pairing_state(path)
        if not state:
            raise TokenError(409, "This Hermes profile isn't paired with Talaria")
        if not state.get("gateway_id"):
            raise TokenError(409, "Pair this Hermes profile with Talaria again to turn on encryption")
        if state.get("installation_id") != installation_id or state.get("gateway_id") != gateway_id:
            raise TokenError(409, "This Hermes profile is paired with a different device")
        state["e2e"] = {
            "kid": kid,
            "secret": secret,
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        _save_pairing_state(path, state)
    return {"kid": kid}


@router.get("/e2e")
async def get_e2e(response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        return {"ok": True, **(await _run_scoped(profile, e2e_status))}
    except TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise _unexpected("status", exc, feature="Encrypted notifications")


@router.post("/e2e")
async def post_e2e(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    # The body is a secret: never cached on the way back either.
    response.headers["Cache-Control"] = "no-store"
    try:
        await _scoped_acquire(_e2e_limiter, profile)
        body = await _read_json_body(request, E2E_MAX_BODY_BYTES)
        return {"ok": True, **(await _run_scoped(profile, lambda: provision_e2e(body)))}
    except TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise _unexpected("key", exc, feature="Encrypted notifications")


# --- Pairing from Talaria ----------------------------------------------------
#
# The same claim `hermes talaria pair CODE` makes, with the code handed
# over the signed-in dashboard connection instead of typed on the host. The
# phone creates the one-time code at its relay and posts it here with that
# relay's URL; this route claims it and saves the credential in the requested
# profile's talaria-push.json, which the hooks re-read on every event, so no
# restart is needed. A re-pair keeps the profile's privacy choice, and its
# encryption key when the same phone and gateway pair again; a new pairing
# drops the key (the phone provisions one for it).

PAIR_LIMIT = 10
PAIR_WINDOW_S = 60.0
PAIR_MAX_BODY_BYTES = 2 * 1024
PAIR_TIMEOUT_S = 15.0
_PAIRING_CODE = re.compile(r"^[A-HJ-NP-Z2-9]{5}-[A-HJ-NP-Z2-9]{5}$")
_pair_limiter = _MintLimiter(PAIR_LIMIT, PAIR_WINDOW_S, message="Too many pairing requests; try again shortly")
# The claim waits on the relay; it gets its own threads, like the other network routes.
_pair_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="talaria-pair")


def _pair_relay_url(raw: Any, state: Optional[Dict[str, Any]]) -> str:
    """The relay to claim at: the phone's, else this profile's current one.

    Only a bare http(s) origin with an optional path: no credentials, query or
    fragment, so the claim can't carry anything but the code to that host.
    """
    if raw is None or raw == "":
        stored = (state or {}).get("relay_url")
        if stored:
            # A stored relay gets the same check; one that fails it isn't used.
            with contextlib.suppress(TokenError):
                return _pair_relay_url(stored, None)
        raise TokenError(400, "relay_url is required")
    if not isinstance(raw, str) or len(raw) > 512 or any(not 0x21 <= ord(c) <= 0x7E for c in raw.strip()):
        raise TokenError(400, "relay_url must be an http(s) URL")
    try:
        parsed = urllib.parse.urlsplit(raw.strip())
        hostname = parsed.hostname
    except ValueError:  # an unclosed IPv6 bracket, a bad port
        raise TokenError(400, "relay_url must be an http(s) URL")
    if parsed.scheme not in ("https", "http") or not hostname or parsed.username or parsed.password \
            or parsed.query or parsed.fragment:
        raise TokenError(400, "relay_url must be an http(s) URL")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")).rstrip("/")


_relay_urls_module: Any = None
_relay_urls_lock = threading.Lock()


def _relay_urls() -> Any:
    """relay_urls.py beside this folder, loaded once: the URL rules both pairing paths share."""
    global _relay_urls_module
    with _relay_urls_lock:
        if _relay_urls_module is None:
            path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "relay_urls.py")
            spec = importlib.util.spec_from_file_location("talaria_push_dashboard_relay_urls", path)
            if spec is None or spec.loader is None:
                raise TokenError(501, "This plugin install is missing relay_urls.py; update the plugin")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _relay_urls_module = module
        return _relay_urls_module


class _NoRedirectRelay(urllib.request.HTTPRedirectHandler):
    # The relay credential travels in a header: never to another host.
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_relay_opener = urllib.request.build_opener(_NoRedirectRelay)
_direct_relay_opener = urllib.request.build_opener(_NoRedirectRelay, urllib.request.ProxyHandler({}))


def _relay_request(url: str, method: str, credential: str, payload: Optional[Dict[str, Any]],
                   timeout: float) -> Tuple[int, Dict[str, Any]]:
    """One relay request: (status, JSON body). Network failures raise."""
    data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {"Accept": "application/json", "Authorization": f"Bearer {credential}",
               "User-Agent": f"Talaria-Plugin/{_plugin_version() or 'unknown'}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        # Plain HTTP (only ever to this machine) skips any proxy, which would see the credential in clear.
        opener = _relay_opener if _relay_urls().is_https(url) else _direct_relay_opener
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(64 * 1024)
            status = response.status
    except urllib.error.HTTPError as error:
        raw = error.read(4 * 1024)
        status = error.code
    try:
        body = json.loads(raw) if raw else {}
    except ValueError:
        body = {}
    return status, body if isinstance(body, dict) else {}


def _pair_gateway_name(raw: Any, profile: Optional[str]) -> str:
    cleaned = "".join(c for c in raw if c.isprintable()).strip() if isinstance(raw, str) else ""
    if cleaned:
        return cleaned[:80]
    import socket

    name = f"{socket.gethostname()} Hermes"
    scoped = (profile or "").strip()
    return (name if _limiter_key(scoped) in ("", "default") else f"{name} ({scoped})")[:80]


def _looks_like_json_object(raw: bytes) -> bool:
    # As client.looks_like_json_object: the relay's answer, not a firewall's.
    return raw.lstrip().removeprefix(b"\xef\xbb\xbf").lstrip().startswith(b"{")


def _claim_pairing(relay_url: str, code: str, gateway_name: str) -> Dict[str, Any]:
    """POST /v1/pairings/claim without credentials or redirects: the relay's answer, else TokenError."""
    data = json.dumps({"pairing_code": code, "gateway_name": gateway_name}, separators=(",", ":")).encode("utf-8")
    unreadable = False
    request = urllib.request.Request(f"{relay_url}/v1/pairings/claim", data=data, method="POST", headers={
        "Accept": "application/json", "Content-Type": "application/json",
        "User-Agent": f"Talaria-Plugin/{_plugin_version() or 'unknown'}"})
    try:
        # A plain-HTTP claim (LAN or Tailscale) skips any proxy, which would see the code in clear.
        opener = _relay_opener if _relay_urls().is_https(relay_url) else _direct_relay_opener
        with opener.open(request, timeout=PAIR_TIMEOUT_S) as response:
            raw, status = response.read(64 * 1024), response.status
    except urllib.error.HTTPError as error:
        status = error.code
        try:
            raw = error.read(4 * 1024)
        except (OSError, http.client.HTTPException):
            raw, unreadable = b"", True
    except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
        raise TokenError(502, f"Couldn't reach the push relay from this host ({type(error).__name__})")
    try:
        body = json.loads(raw) if raw else {}
    except ValueError:
        body = {}
    if status == 404:
        raise TokenError(410, "This pairing code expired or was already used. Create a new one.")
    if status == 429:
        if isinstance(body, dict) and body.get("error") == "gateway_limit_reached":
            # Every pairing slot this phone has at the relay is taken: waiting won't free one.
            raise TokenError(409, "The push relay holds too many pairings for this phone. Turn notifications off "
                                  "and on again in Talaria's Settings > Notifications to start fresh, then pair again.")
        raise TokenError(429, "The push relay is rate limiting pairing; try again shortly")
    if status == 403 and not unreadable and not _looks_like_json_object(raw):
        # The relay always answers with a JSON error: a bare 403 is something
        # in front of it (a firewall, a rate limiter) blocking this host.
        logger.warning("Talaria plugin: something in front of the push relay answered the pairing claim "
                       "with an empty 403: this host's address is blocked there")
        raise TokenError(502, "Something in front of the push relay is blocking this host, so pairing can't get "
                              "through. Pairing again won't help until the relay's operator lifts the block.")
    if not 200 <= status < 300 or not isinstance(body, dict):
        # The relay's own status stays in the log: the answer doesn't probe hosts.
        logger.warning("Talaria plugin: the push relay answered the pairing claim with HTTP %s", status)
        raise TokenError(502, "The push relay refused the pairing")
    credential, installation_id = body.get("credential"), body.get("installation_id")
    if not isinstance(credential, str) or not credential or not isinstance(installation_id, str) or not installation_id:
        raise TokenError(502, "The push relay sent an unexpected pairing answer")
    return body


def _revoke_replaced_pairing(relay_url: str, credential: str) -> None:
    """Revokes a credential that a new pairing replaced, off the pairing path.

    The relay mints a new gateway per claim, so without this every re-pair
    leaves a live gateway behind until the phone's slots run out (#250).
    Best effort: the new pairing already works either way.
    """
    try:
        url = _relay_urls().check_request_url(f"{relay_url.rstrip('/')}/v1/gateways/current")
    except ValueError:
        # Plain http:// off this machine (a pairing saved before HTTPS was
        # required): the credential isn't sent there.
        return

    def revoke() -> None:
        try:
            status, _body = _relay_request(url, "DELETE", credential, None, PAIR_TIMEOUT_S)
            if not 200 <= status < 300 and status != 401:
                logger.info("Talaria plugin: the relay answered revoking the replaced pairing with HTTP %s", status)
        except Exception as exc:
            logger.info("Talaria plugin: the replaced pairing wasn't revoked at the relay: %s", type(exc).__name__)

    try:
        _start_revoke_thread(revoke)
    except RuntimeError as exc:
        # Out of threads. The pairing is saved, and this doesn't undo it.
        logger.warning("Talaria plugin: the replaced pairing wasn't revoked at the relay: %s", exc)


def _start_revoke_thread(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="talaria-pair-revoke", daemon=True).start()


def pair_profile(body: Any, profile: Optional[str] = None, path: Any = None,
                 claim: Optional[Callable[[str, str, str], Dict[str, Any]]] = None,
                 limit: Optional[Callable[[], None]] = None) -> Dict[str, Any]:
    """Claims the phone's pairing code for this profile. Never returns the credential.

    `limit` runs once the request is well formed, so a mistyped code costs no
    rate-limit slot. The credential this pairing replaces is revoked at its
    relay afterwards.
    """
    if not isinstance(body, dict):
        raise TokenError(400, "Expected a JSON object")
    code = body.get("pairing_code")
    code = code.strip().upper() if isinstance(code, str) else ""
    if not _PAIRING_CODE.match(code):
        raise TokenError(400, "pairing_code must look like ABCDE-FGHJK")
    path = path if path is not None else _pairing_state_path()
    relay_url = _pair_relay_url(body.get("relay_url"), _load_pairing_state(path))
    relay_urls = _relay_urls()
    try:
        # Plain http:// only to a LAN or Tailscale address, before the code is spent.
        relay_urls.check_claim_base(relay_url)
    except relay_urls.RelayURLError as exc:
        raise TokenError(400, str(exc))
    gateway_name = _pair_gateway_name(body.get("gateway_name"), profile)
    if limit is not None:
        limit()
    claimed = (claim or _claim_pairing)(relay_url, code, gateway_name)
    state: Dict[str, Any] = {
        "credential": claimed["credential"],
        "gateway_id": claimed.get("gateway_id"),
        "gateway_name": gateway_name,
        "installation_id": claimed["installation_id"],
        "relay_url": relay_url,
    }
    # The relay may name its canonical URL: kept when it is HTTPS on the same
    # origin, or on any origin after a claim over a LAN or Tailscale address.
    try:
        state["relay_url"] = relay_urls.paired_relay_url(claimed.get("relay_url"), relay_url)
    except relay_urls.RelayURLError as exc:
        raise TokenError(502, str(exc))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _e2e_lock, _pairing_state_lock(path):
        # Same carry-over as client.claim_pairing: the privacy choice stays.
        previous = _load_pairing_state(path) or {}
        if previous.get("redact_content"):
            state["redact_content"] = True
        if previous.get("redact_key"):
            state["redact_key"] = previous["redact_key"]
        # Re-pairing the same phone and gateway keeps its encryption key.
        if previous.get("e2e") is not None and previous.get("installation_id") == state["installation_id"] \
                and previous.get("gateway_id") == state["gateway_id"]:
            state["e2e"] = previous["e2e"]
        _save_pairing_state(path, state)
    old_credential, old_relay = previous.get("credential"), previous.get("relay_url")
    if isinstance(old_credential, str) and old_credential and old_credential != state["credential"] \
            and isinstance(old_relay, str) and old_relay:
        _revoke_replaced_pairing(old_relay, old_credential)
    return {"paired": True, "installation_id": state["installation_id"], "gateway_id": state["gateway_id"],
            "gateway_name": gateway_name, "relay_url": state["relay_url"]}


@router.post("/pair")
async def post_pair(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        body = await _read_json_body(request, PAIR_MAX_BODY_BYTES)
        # The dashboard's own profile has one window, named or not.
        key = "" if _limiter_key(profile) in ("", "default") else _limiter_key(profile)
        limit = lambda: _pair_limiter.acquire(key)  # noqa: E731
        return {"ok": True, **(await _run_scoped(profile, lambda: pair_profile(body, profile, limit=limit),
                                                 _pair_executor))}
    except TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException:
        raise
    except Exception as exc:
        raise _unexpected("pairing", exc, feature="Pairing")


# --- Cron notifications ------------------------------------------------------
#
# The user turns one cron job's notifications off in Talaria's Cron tab. The
# muted job ids live in the profile's talaria-cron.json, shared with the
# plugin's turn-end hooks (agent process), which skip the push for that job's
# runs, through cron_store.py (loaded by path: this module can't import the
# plugin package). A run that answers [SILENT] never notifies either way.

CRON_LIMIT = 60
CRON_WINDOW_S = 60.0
CRON_MAX_BODY_BYTES = 1024
_cron_limiter = _MintLimiter(CRON_LIMIT, CRON_WINDOW_S, message="Too many cron requests; try again shortly")
_cron_store_module: Any = None
_cron_store_lock = threading.Lock()


def _cron_store() -> Any:
    """cron_store.py beside this folder, loaded once."""
    global _cron_store_module
    with _cron_store_lock:
        if _cron_store_module is None:
            path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cron_store.py")
            spec = importlib.util.spec_from_file_location("talaria_push_dashboard_cron_store", path)
            if spec is None or spec.loader is None or not os.path.isfile(path):
                raise TokenError(501, "This plugin install is missing cron_store.py; update the plugin")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _cron_store_module = module
        return _cron_store_module


def _cron_home() -> Any:
    from hermes_constants import get_hermes_home

    return get_hermes_home()


def cron_notifications() -> Dict[str, Any]:
    return {"muted": _cron_store().CronStore(_cron_home()).muted()}


def update_cron_notifications(body: Any) -> Dict[str, Any]:
    if not isinstance(body, dict):
        raise TokenError(400, "Expected a JSON object with job_id and notify")
    try:
        muted = _cron_store().CronStore(_cron_home()).set_notify(body.get("job_id"), body.get("notify"))
    except ValueError as exc:
        raise TokenError(400, str(exc))
    return {"muted": muted}


async def _cron_route(profile: Optional[str], response: Response, fn: Callable[[], Dict[str, Any]],
                      route: str, feature: str = "Cron notifications") -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        return {"ok": True, **(await _run_scoped(profile, _limited(_cron_limiter, profile, fn)))}
    except TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise _unexpected(route, exc, feature=feature)


@router.get("/cron/notifications")
async def get_cron_notifications(response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    return await _cron_route(profile, response, cron_notifications, "status")


@router.put("/cron/notifications")
async def put_cron_notifications(request: Request, response: Response,
                                 profile: Optional[str] = None) -> Dict[str, Any]:
    body = await _read_json_body(request, CRON_MAX_BODY_BYTES)
    return await _cron_route(profile, response, lambda: update_cron_notifications(body), "settings")


# --- Reply notifications by source (#115) ------------------------------------
#
# The user picks in Talaria which sources' replies push "reply ready": Talaria,
# Hermes Desktop and the web dashboard, messaging apps, the terminal. The
# choice lives in the profile's talaria-notifications.json, read by the
# plugin's turn-end hook (agent process) through reply_sources.py (loaded by
# path: this module can't import the plugin package).

_reply_sources_module: Any = None
_reply_sources_lock = threading.Lock()


def _reply_sources() -> Any:
    """reply_sources.py beside this folder, loaded once."""
    global _reply_sources_module
    with _reply_sources_lock:
        if _reply_sources_module is None:
            path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reply_sources.py")
            spec = importlib.util.spec_from_file_location("talaria_push_dashboard_reply_sources", path)
            if spec is None or spec.loader is None or not os.path.isfile(path):
                raise TokenError(501, "This plugin install is missing reply_sources.py; update the plugin")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _reply_sources_module = module
        return _reply_sources_module


def reply_source_settings() -> Dict[str, Any]:
    return {"sources": _reply_sources().ReplySourcesStore(_cron_home()).settings()}


def update_reply_source_settings(body: Any) -> Dict[str, Any]:
    if not isinstance(body, dict):
        raise TokenError(400, "Expected a JSON object with source and notify")
    try:
        sources = _reply_sources().ReplySourcesStore(_cron_home()).set_notify(body.get("source"), body.get("notify"))
    except ValueError as exc:
        raise TokenError(400, str(exc))
    return {"sources": sources}


@router.get("/notifications/reply-sources")
async def get_reply_sources(response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    return await _cron_route(profile, response, reply_source_settings, "status", feature="Reply notifications")


@router.put("/notifications/reply-sources")
async def put_reply_sources(request: Request, response: Response,
                            profile: Optional[str] = None) -> Dict[str, Any]:
    body = await _read_json_body(request, CRON_MAX_BODY_BYTES)
    return await _cron_route(profile, response, lambda: update_reply_source_settings(body), "settings",
                             feature="Reply notifications")


# --- A chat Talaria has on screen (chat_on_screen.py) ------------------------
#
# While a turn runs in a chat open in Talaria, Talaria holds it here as on
# screen, renewing the hold while the turn runs and dropping it (hold 0) when
# the user leaves the chat or the app. A reply that lands in it then sends no
# "reply ready" push: the user watched it arrive. The holds live in the
# profile's home, read by the plugin's turn-end hook (agent process).

ON_SCREEN_LIMIT = 60
ON_SCREEN_WINDOW_S = 60.0
ON_SCREEN_MAX_BODY_BYTES = 4096
_on_screen_limiter = _MintLimiter(ON_SCREEN_LIMIT, ON_SCREEN_WINDOW_S,
                                  message="Too many on-screen reports; try again shortly")
_chat_on_screen_module: Any = None
_chat_on_screen_lock = threading.Lock()


def _chat_on_screen() -> Any:
    """chat_on_screen.py beside this folder, loaded once."""
    global _chat_on_screen_module
    with _chat_on_screen_lock:
        if _chat_on_screen_module is None:
            path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "chat_on_screen.py")
            spec = importlib.util.spec_from_file_location("talaria_push_dashboard_chat_on_screen", path)
            if spec is None or spec.loader is None or not os.path.isfile(path):
                raise TokenError(501, "This plugin install is missing chat_on_screen.py; update the plugin")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _chat_on_screen_module = module
        return _chat_on_screen_module


def set_chat_on_screen(body: Any) -> Dict[str, Any]:
    module = _chat_on_screen()
    try:
        device, session_ids, hold_s = module.parse(body)
    except ValueError as exc:
        raise TokenError(400, str(exc))
    return {"on_screen": module.hold(_cron_home(), device, session_ids, hold_s)}


@router.put("/notifications/on-screen")
async def put_chat_on_screen(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    no_store = {"Cache-Control": "no-store"}
    try:
        body = await _read_json_body(request, ON_SCREEN_MAX_BODY_BYTES)
        return {"ok": True, **(await _run_scoped(
            profile, _limited(_on_screen_limiter, profile, lambda: set_chat_on_screen(body))))}
    except TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers=no_store)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), **no_store}
        raise
    except Exception as exc:
        raise _unexpected("on-screen", exc, feature="Reply notifications")


# --- Hermes calls you (#449) -------------------------------------------------
#
# Talaria reads and changes the per-profile call settings here. The plugin's
# turn-end hooks (agent process) and Hermes' own watches send the call
# requests through the push relay. The watches and the settings live in the
# profile's talaria-calls.json, shared with the hooks through calls_store.py
# (loaded by path: this module can't import the plugin package).
#
# A call the user declines or doesn't answer comes back here from Talaria as
# an outcome; the next turn of its chat tells Hermes (calls.outcome_note).

CALLS_LIMIT = 30
CALLS_WINDOW_S = 60.0
CALLS_MAX_BODY_BYTES = 4096
_calls_limiter = _MintLimiter(CALLS_LIMIT, CALLS_WINDOW_S, message="Too many call requests; try again shortly")
_calls_store_module: Any = None
_calls_store_lock = threading.Lock()


def _calls_store() -> Any:
    """calls_store.py beside this folder, loaded once."""
    global _calls_store_module
    with _calls_store_lock:
        if _calls_store_module is None:
            path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "calls_store.py")
            spec = importlib.util.spec_from_file_location("talaria_push_dashboard_calls_store", path)
            if spec is None or spec.loader is None:
                raise TokenError(501, "This plugin install is missing calls_store.py; update the plugin")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _calls_store_module = module
        return _calls_store_module


def _calls_home() -> Any:
    from hermes_constants import get_hermes_home

    return get_hermes_home()


def calls_status() -> Dict[str, Any]:
    module = _calls_store()
    store = module.CallStore(_calls_home())
    return {
        "paired": _load_pairing_state(_pairing_state_path()) is not None,
        "settings": store.settings(),
        "bounds": {key: {"min": low, "max": high} for key, (low, high) in module.INT_BOUNDS.items()},
        # The longest "what's worth a call" note the host keeps.
        "rules_max": module.MAX_RULES_CHARS,
        "watches": store.watch_count(),
    }


def update_call_settings(body: Any) -> Dict[str, Any]:
    if not isinstance(body, dict) or not isinstance(body.get("settings"), dict):
        raise TokenError(400, "Expected a JSON object with settings")
    store = _calls_store().CallStore(_calls_home())
    try:
        return {"settings": store.update_settings(body["settings"])}
    except ValueError as exc:
        raise TokenError(400, str(exc))


def record_call_outcome(body: Any) -> Dict[str, Any]:
    """The user declined a call from Hermes, or didn't answer it: the next
    turn of its chat hears so."""
    if not isinstance(body, dict):
        raise TokenError(400, "Expected a JSON object")
    if _load_pairing_state(_pairing_state_path()) is None:
        raise TokenError(409, "This Hermes profile isn't paired with Talaria")
    try:
        return _calls_store().CallStore(_calls_home()).record_outcome(
            body.get("call_id"), body.get("session_ids"), body.get("outcome"), kind=body.get("kind"),
            title=body.get("title", ""), reason=body.get("reason", ""), age_s=body.get("age_s", 0))
    except ValueError as exc:
        raise TokenError(400, str(exc))


async def _calls_route(profile: Optional[str], response: Response, fn: Callable[[], Dict[str, Any]],
                       route: str) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        return {"ok": True, **(await _run_scoped(profile, _limited(_calls_limiter, profile, fn)))}
    except TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise _unexpected(route, exc, feature="Calls")


@router.get("/calls")
async def get_calls(response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    return await _calls_route(profile, response, calls_status, "status")


@router.put("/calls")
async def put_calls(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    body = await _read_json_body(request, CALLS_MAX_BODY_BYTES)
    return await _calls_route(profile, response, lambda: update_call_settings(body), "settings")


@router.post("/calls/outcomes")
async def post_call_outcome(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    body = await _read_json_body(request, CALLS_MAX_BODY_BYTES)
    return await _calls_route(profile, response, lambda: record_call_outcome(body), "outcome")


# --- Talaria settings Hermes may change --------------------------------------
#
# Each Talaria uploads the settings it lets Hermes see (and the user's "Let
# Hermes change settings" mode), reports what became of the requests it took,
# and gets back the ones still waiting for it (settings_store.py). Talaria
# stays the owner: it re-checks every request and asks the user as its mode
# says.

TALARIA_SETTINGS_LIMIT = 30
TALARIA_SETTINGS_WINDOW_S = 60.0
# Room for the store's worst case: 80 settings, each with a description and
# up to 40 choices.
TALARIA_SETTINGS_MAX_BODY_BYTES = 640 * 1024

_talaria_settings_limiter = _MintLimiter(TALARIA_SETTINGS_LIMIT, TALARIA_SETTINGS_WINDOW_S,
                                         message="Too many settings syncs; try again shortly")
_settings_store_module: Any = None


def _settings_store() -> Any:
    """settings_store.py beside this folder, loaded once."""
    global _settings_store_module
    with _calls_store_lock:
        if _settings_store_module is None:
            path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "settings_store.py")
            spec = importlib.util.spec_from_file_location("talaria_push_dashboard_settings_store", path)
            if spec is None or spec.loader is None:
                raise TokenError(501, "This plugin install is missing settings_store.py; update the plugin")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _settings_store_module = module
        return _settings_store_module


def sync_talaria_settings(body: Any) -> Dict[str, Any]:
    """The installation id names a device; it doesn't authenticate one. The
    dashboard's auth is the boundary, as on every route here."""
    if _load_pairing_state(_pairing_state_path()) is None:
        raise TokenError(409, "This Hermes profile isn't paired with Talaria")
    module = _settings_store()
    try:
        return module.SettingsStore(_calls_home()).sync(body)
    except module.SettingsError as exc:
        raise TokenError(400, str(exc))


@router.put("/talaria-settings")
async def put_talaria_settings(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    body = await _read_json_body(request, TALARIA_SETTINGS_MAX_BODY_BYTES)
    try:
        return {"ok": True, **(await _run_scoped(
            profile, _limited(_talaria_settings_limiter, profile, lambda: sync_talaria_settings(body))))}
    except TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise _unexpected("settings", exc, feature="Talaria settings")


# --- Confirm prompts Hermes shows on the phone ---------------------------------
#
# Before an action the user should OK, talaria_present keeps a confirm prompt
# (present_store.py) and wakes the phone with phone.changes. Talaria reports
# what the user answered (tap or voice) and gets back the prompts still
# waiting, with the same identity fields as /talaria-settings.

PRESENT_EXCHANGE_LIMIT = 60
PRESENT_EXCHANGE_WINDOW_S = 60.0
PRESENT_MAX_BODY_BYTES = 512 * 1024  # room for 40 results with 2,000-character edits in any script, even \u-escaped

_present_exchange_limiter = _MintLimiter(PRESENT_EXCHANGE_LIMIT, PRESENT_EXCHANGE_WINDOW_S,
                                         message="Too many confirm prompt checks; try again shortly")
_present_store_module: Any = None


def _present_store() -> Any:
    """present_store.py beside this folder, loaded once."""
    global _present_store_module
    with _calls_store_lock:
        if _present_store_module is None:
            path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "present_store.py")
            spec = importlib.util.spec_from_file_location("talaria_push_dashboard_present_store", path)
            if spec is None or spec.loader is None or not os.path.exists(path):
                raise TokenError(501, "This plugin install is missing present_store.py; update the plugin")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _present_store_module = module
        return _present_store_module


def exchange_present(body: Any) -> Dict[str, Any]:
    """The installation id names a device; it doesn't authenticate one. The
    dashboard's auth is the boundary, as on every route here."""
    if _load_pairing_state(_pairing_state_path()) is None:
        raise TokenError(409, "This Hermes profile isn't paired with Talaria")
    module = _present_store()
    try:
        return module.PresentStore(_calls_home()).exchange(body)
    except module.PresentError as exc:
        raise TokenError(400, str(exc))
    except OSError:
        # The store couldn't be read or written right now; the phone keeps
        # its answers and exchanges again.
        raise TokenError(503, "Confirm prompts are unavailable right now")


@router.post("/present/exchange")
async def post_present_exchange(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    body = await _read_json_body(request, PRESENT_MAX_BODY_BYTES)
    try:
        return {"ok": True, **(await _run_scoped(
            profile, _limited(_present_exchange_limiter, profile, lambda: exchange_present(body))))}
    except TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise _unexpected("present", exc, feature="Confirm prompts")


# --- Health data (shared from Talaria) ---------------------------------------
#
# The user turns on health sharing in Talaria, per category. The phone agrees
# a key with this profile once (/health/key, X25519: only public keys cross
# the wire) and then posts sealed batches of health totals and events
# (/health/ingest) straight here, never through the push relay. The store
# (health_store.py) keeps them in the profile's talaria-health.sqlite3 for the
# talaria_health tool. Status reports counts and dates only and wipe
# deletes; no route here returns health data.

HEALTH_KEY_LIMIT = 10
HEALTH_KEY_WINDOW_S = 60.0
HEALTH_WIPE_LIMIT = 5
HEALTH_WIPE_WINDOW_S = 60.0
# A backfill sends many batches in a row.
HEALTH_INGEST_LIMIT = 120
HEALTH_INGEST_WINDOW_S = 60.0
HEALTH_MAX_KEY_BODY_BYTES = 4 * 1024
# A sealed batch is base64url of at most ~3 MiB of JSON.
HEALTH_MAX_INGEST_BODY_BYTES = 4 * 1024 * 1024

_health_key_limiter = _MintLimiter(HEALTH_KEY_LIMIT, HEALTH_KEY_WINDOW_S,
                                   message="Too many health key requests; try again shortly")
_health_ingest_limiter = _MintLimiter(HEALTH_INGEST_LIMIT, HEALTH_INGEST_WINDOW_S,
                                      message="Too many health uploads; try again shortly")
_health_wipe_limiter = _MintLimiter(HEALTH_WIPE_LIMIT, HEALTH_WIPE_WINDOW_S,
                                    message="Too many health delete requests; try again shortly")
_health_store_module: Any = None


# Plugin modules beside this folder (health_store.py, phone_store.py, …),
# imported as one private package (its __init__ never runs, so nothing
# registers with Hermes here) so their relative imports stay inside it.
DASHBOARD_PACKAGE = "talaria_push_dashboard_pkg"


def _plugin_package_module(name: str) -> Any:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with _calls_store_lock:
        if DASHBOARD_PACKAGE not in sys.modules:
            package = types.ModuleType(DASHBOARD_PACKAGE)
            package.__path__ = [root]
            sys.modules[DASHBOARD_PACKAGE] = package
    try:
        return importlib.import_module(f"{DASHBOARD_PACKAGE}.{name}")
    except ModuleNotFoundError as exc:
        if exc.name != f"{DASHBOARD_PACKAGE}.{name}":
            raise
        raise TokenError(501, f"This plugin install is missing {name}.py; update the plugin") from None


def _health_store() -> Any:
    """health_store.py beside this folder, loaded once."""
    global _health_store_module
    if _health_store_module is None:
        _health_store_module = _plugin_package_module("health_store")
    return _health_store_module


def _health_call(fn: Callable[[Any], Dict[str, Any]]) -> Dict[str, Any]:
    module = _health_store()
    try:
        return fn(module.HealthStore(_calls_home()))
    except module.HealthError as exc:
        raise TokenError(exc.status, str(exc), exc.code)


async def _health_route(response: Response, profile: Optional[str], fn: Callable[[], Dict[str, Any]],
                        name: str) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        return {"ok": True, **(await _run_scoped(profile, fn))}
    except TokenError as exc:
        return _store_refusal(exc)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise _unexpected(name, exc, feature="Health sync")


def _store_refusal(exc: TokenError) -> Any:
    """A health or location store's refusal: ``detail`` is the message as
    always, and ``code`` the store's word for it (``unknown_key``,
    ``bad_seal``, ``too_many_devices``, ``schema``…), so Talaria can tell a
    lost key from a full host without reading the message."""
    if not exc.code:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    from fastapi.responses import JSONResponse

    return JSONResponse({"detail": str(exc), "code": exc.code}, status_code=exc.status,
                        headers={"Cache-Control": "no-store"})


@router.get("/health/status")
async def get_health_status(response: Response, profile: Optional[str] = None,
                            device_id: Optional[str] = None) -> Dict[str, Any]:
    return await _health_route(response, profile, lambda: _health_call(lambda store: {
        **store.status(device_id), "crypto": _health_store().crypto_available()}), "status")


@router.post("/health/key")
async def post_health_key(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    await _scoped_acquire(_health_key_limiter, profile)
    body = await _read_json_body(request, HEALTH_MAX_KEY_BODY_BYTES)
    return await _health_route(response, profile, lambda: _health_call(lambda store: store.register_key(body)), "key")


@router.post("/health/ingest")
async def post_health_ingest(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    await _scoped_acquire(_health_ingest_limiter, profile)
    body = await _read_json_body(request, HEALTH_MAX_INGEST_BODY_BYTES)
    return await _health_route(response, profile, lambda: _health_call(lambda store: store.ingest(body)), "upload")


@router.post("/health/wipe")
async def post_health_wipe(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    await _scoped_acquire(_health_wipe_limiter, profile)
    body = await _read_json_body(request, HEALTH_MAX_KEY_BODY_BYTES)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object", headers={"Cache-Control": "no-store"})
    # Everything only when asked for by name; a device id alone deletes that device's data.
    if body.get("all") is True and body.get("device_id") is not None:
        raise HTTPException(status_code=400, detail="Name a device_id or all: true, not both",
                            headers={"Cache-Control": "no-store"})
    device_id = None if body.get("all") is True else body.get("device_id")
    if device_id is None and body.get("all") is not True:
        raise HTTPException(status_code=400, detail="Name a device_id, or all: true",
                            headers={"Cache-Control": "no-store"})
    return await _health_route(response, profile, lambda: _health_call(lambda store: store.wipe(device_id)), "delete")


# --- Location (shared from Talaria) ---------------------------------------
#
# The user turns on location sharing in Talaria: while they talk to Hermes,
# and, separately, in the background. Like health, the phone agrees a key
# with this profile once (/location/key) and then posts sealed batches
# (/location/ingest) straight here, never through the push relay. The store
# (location_store.py) keeps the latest fix, arrivals and departures for the
# user's chosen history, place rules and the agent's read log. No route
# returns a location: status and the read log say when and how much, rules
# are the user's own, and wipe deletes. An arrival or departure that matches
# a place rule starts that rule's scheduled job (Hermes's cron.jobs, which
# runs it on the gateway's next tick).

LOCATION_KEY_LIMIT = 10
LOCATION_KEY_WINDOW_S = 60.0
LOCATION_INGEST_LIMIT = 60
LOCATION_INGEST_WINDOW_S = 60.0
LOCATION_WIPE_LIMIT = 5
LOCATION_WIPE_WINDOW_S = 60.0
LOCATION_MAX_KEY_BODY_BYTES = 4 * 1024
LOCATION_MAX_INGEST_BODY_BYTES = 256 * 1024

_location_key_limiter = _MintLimiter(LOCATION_KEY_LIMIT, LOCATION_KEY_WINDOW_S,
                                     message="Too many location key requests; try again shortly")
_location_ingest_limiter = _MintLimiter(LOCATION_INGEST_LIMIT, LOCATION_INGEST_WINDOW_S,
                                        message="Too many location uploads; try again shortly")
_location_wipe_limiter = _MintLimiter(LOCATION_WIPE_LIMIT, LOCATION_WIPE_WINDOW_S,
                                      message="Too many location delete requests; try again shortly")
_location_store_module: Any = None


def _location_store() -> Any:
    """location_store.py beside this folder, loaded once."""
    global _location_store_module
    with _calls_store_lock:
        if _location_store_module is None:
            path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "location_store.py")
            spec = importlib.util.spec_from_file_location("talaria_push_dashboard_location_store", path)
            if spec is None or spec.loader is None:
                raise TokenError(501, "This plugin install is missing location_store.py; update the plugin")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _location_store_module = module
        return _location_store_module


def _location_call(fn: Callable[[Any], Dict[str, Any]]) -> Dict[str, Any]:
    module = _location_store()
    try:
        return fn(module.LocationStore(_calls_home()))
    except module.LocationError as exc:
        raise TokenError(exc.status, str(exc), exc.code)
    except sqlite3.DatabaseError:
        logger.warning("talaria_push: location store unreadable", exc_info=True)
        raise TokenError(503, "The location store on this host can't be read")


async def _location_route(response: Response, profile: Optional[str], fn: Callable[[], Dict[str, Any]],
                          name: str) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        return {"ok": True, **(await _run_scoped(profile, fn))}
    except TokenError as exc:
        return _store_refusal(exc)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise _unexpected(name, exc, feature="Location sharing")


def _start_rule_jobs(fired: Any) -> list:
    """Starts the scheduled job of each place rule an ingest set off; runs
    inside the profile's scope. The rule already counts as run today, so a
    job that can't start is logged, not retried."""
    started = []
    for rule in fired if isinstance(fired, list) else []:
        job_id = rule.get("job_id") if isinstance(rule, dict) else None
        if not job_id:
            continue
        try:
            from cron.jobs import trigger_job

            if trigger_job(job_id, extra_prompt=rule.get("prompt")):
                started.append(rule.get("rule_id"))
            else:
                logger.warning("talaria_push: place rule %s has no job %s", rule.get("rule_id"), job_id)
        except Exception:  # noqa: BLE001 — the upload itself succeeded
            logger.warning("talaria_push: could not start place rule %s", rule.get("rule_id"), exc_info=True)
    return started


def _clear_rule_jobs() -> None:
    """Removes the jobs of deleted place rules still on the store's list
    (#227); runs inside the profile's scope. The store listed each one as it
    deleted the rule, so a job that can't go now (or a crash before this) is
    retried on the next rule change here, in the tool, or at the next start.
    A job counts as gone once remove_job removes it, or answers False and
    get_job confirms the scheduler doesn't have it; anything else keeps it
    listed."""
    try:
        store = _location_store().LocationStore(_calls_home())
        pending = store.pending_rule_jobs()
    except Exception:  # noqa: BLE001 — the change itself succeeded
        logger.warning("talaria_push: place rule jobs not read", exc_info=True)
        return
    gone = []
    for job_id in pending:
        try:
            from cron import jobs as cron_jobs

            if cron_jobs.remove_job(job_id) or cron_jobs.get_job(job_id) is None:
                gone.append(job_id)
            else:
                logger.warning("talaria_push: place rule job %s not removed yet", job_id)
        except Exception:  # noqa: BLE001 — the rule is gone; its job's prompt does nothing without an event
            logger.warning("talaria_push: could not remove place rule job %s", job_id, exc_info=True)
    try:
        store.rule_jobs_gone(gone)
    except Exception:  # noqa: BLE001 — still listed: removed again next time, which is harmless
        logger.warning("talaria_push: removed place rule jobs not taken off the list", exc_info=True)


def _location_ingest(body: Any) -> Dict[str, Any]:
    result = _location_call(lambda store: store.ingest(body))
    fired = result.pop("rules_fired", [])
    result["rules_fired"] = len(_start_rule_jobs(fired))
    return result


def _location_wipe(device_id: Any) -> Dict[str, Any]:
    result = _location_call(lambda store: store.wipe(device_id))
    result.pop("rule_jobs", None)
    _clear_rule_jobs()
    return result


def _location_remove_rule(rule_id: str) -> Dict[str, Any]:
    job_id = _location_call(lambda store: {"job": store.remove_rule(rule_id)})["job"]
    if job_id is None:
        raise TokenError(404, "No place rule has that id")
    _clear_rule_jobs()
    return {"removed": True}


@router.get("/location/status")
async def get_location_status(response: Response, profile: Optional[str] = None,
                              device_id: Optional[str] = None) -> Dict[str, Any]:
    # `locate` tells Talaria whether talaria_location can ask the phone for a
    # fresh fix: the same pairing gate as the tool's own (not_paired).
    return await _location_route(response, profile, lambda: _location_call(lambda store: {
        **store.status(device_id), "crypto": _location_store().crypto_available(),
        "locate": {"available": _load_pairing_state(_pairing_state_path()) is not None}}), "status")


@router.post("/location/key")
async def post_location_key(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    await _scoped_acquire(_location_key_limiter, profile)
    body = await _read_json_body(request, LOCATION_MAX_KEY_BODY_BYTES)
    return await _location_route(response, profile,
                                 lambda: _location_call(lambda store: store.register_key(body)), "key")


@router.post("/location/ingest")
async def post_location_ingest(request: Request, response: Response,
                               profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    await _scoped_acquire(_location_ingest_limiter, profile)
    body = await _read_json_body(request, LOCATION_MAX_INGEST_BODY_BYTES)
    return await _location_route(response, profile, lambda: _location_ingest(body), "upload")


@router.get("/location/reads")
async def get_location_reads(response: Response, profile: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
    return await _location_route(response, profile,
                                 lambda: _location_call(lambda store: {"reads": store.reads(limit)}), "reads")


@router.get("/location/rules")
async def get_location_rules(response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    def rules(store: Any) -> Dict[str, Any]:
        keys = ("id", "what", "place_id", "place_name", "event", "days", "hours", "call", "task", "repeat", "last_fired")
        return {"rules": [{key: rule[key] for key in keys} for rule in store.rules()]}

    return await _location_route(response, profile, lambda: _location_call(rules), "rules")


@router.delete("/location/rules/{rule_id}")
async def delete_location_rule(rule_id: str, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    await _scoped_acquire(_location_wipe_limiter, profile)
    return await _location_route(response, profile, lambda: _location_remove_rule(rule_id), "rule delete")


@router.post("/location/wipe")
async def post_location_wipe(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    await _scoped_acquire(_location_wipe_limiter, profile)
    body = await _read_json_body(request, LOCATION_MAX_KEY_BODY_BYTES)
    no_store = {"Cache-Control": "no-store"}
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object", headers=no_store)
    # Everything only when asked for by name; a device id alone deletes that device's data.
    if body.get("all") is True and body.get("device_id") is not None:
        raise HTTPException(status_code=400, detail="Name a device_id or all: true, not both", headers=no_store)
    device_id = None if body.get("all") is True else body.get("device_id")
    if device_id is None and body.get("all") is not True:
        raise HTTPException(status_code=400, detail="Name a device_id, or all: true", headers=no_store)
    return await _location_route(response, profile, lambda: _location_wipe(device_id), "delete")


# --- Calendar, Reminders, Focus and phone status (shared from Talaria) ------
#
# The user turns on each kind in Talaria. The phone agrees a key with this
# profile (/phone/key, X25519, its own key: wiping this never touches health
# or location), posts sealed batches of events, reminders, its calendars,
# Focus and status (/phone/ingest) straight here, never through the push
# relay, and runs a sealed exchange (/phone/exchange) to report how the
# changes Hermes asked for went and to pick up the ones still waiting. The
# store (phone_store.py) keeps it all in the profile's talaria-phone.sqlite3
# for the talaria_calendar and talaria_phone tools. Status reports counts and
# dates only, wipe deletes, and the exchange answers only the device that
# asks, sealed with that device's own key.

PHONE_KEY_LIMIT = 10
PHONE_KEY_WINDOW_S = 60.0
PHONE_WIPE_LIMIT = 5
PHONE_WIPE_WINDOW_S = 60.0
# A first sync or a window change sends many batches in a row.
PHONE_INGEST_LIMIT = 120
PHONE_INGEST_WINDOW_S = 60.0
PHONE_EXCHANGE_LIMIT = 60
PHONE_EXCHANGE_WINDOW_S = 60.0
PHONE_MAX_KEY_BODY_BYTES = 4 * 1024
PHONE_MAX_INGEST_BODY_BYTES = 4 * 1024 * 1024
PHONE_MAX_EXCHANGE_BODY_BYTES = 64 * 1024

_phone_key_limiter = _MintLimiter(PHONE_KEY_LIMIT, PHONE_KEY_WINDOW_S,
                                  message="Too many calendar key requests; try again shortly")
_phone_ingest_limiter = _MintLimiter(PHONE_INGEST_LIMIT, PHONE_INGEST_WINDOW_S,
                                     message="Too many calendar uploads; try again shortly")
_phone_exchange_limiter = _MintLimiter(PHONE_EXCHANGE_LIMIT, PHONE_EXCHANGE_WINDOW_S,
                                       message="Too many calendar change checks; try again shortly")
_phone_wipe_limiter = _MintLimiter(PHONE_WIPE_LIMIT, PHONE_WIPE_WINDOW_S,
                                   message="Too many calendar delete requests; try again shortly")
_phone_store_module: Any = None


def _phone_store() -> Any:
    """phone_store.py beside this folder, loaded once (as part of the
    private package, like health_store)."""
    global _phone_store_module
    if _phone_store_module is None:
        _phone_store_module = _plugin_package_module("phone_store")
    return _phone_store_module


def _phone_call(fn: Callable[[Any], Dict[str, Any]]) -> Dict[str, Any]:
    module = _phone_store()
    try:
        return fn(module.PhoneStore(_calls_home()))
    except module.PhoneError as exc:
        raise TokenError(exc.status, str(exc), exc.code)


async def _phone_route(response: Response, profile: Optional[str], fn: Callable[[], Dict[str, Any]],
                       name: str) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        return {"ok": True, **(await _run_scoped(profile, fn))}
    except TokenError as exc:
        return _store_refusal(exc)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise _unexpected(name, exc, feature="Calendar sharing")


@router.get("/phone/status")
async def get_phone_status(response: Response, profile: Optional[str] = None,
                           device_id: Optional[str] = None) -> Dict[str, Any]:
    return await _phone_route(response, profile, lambda: _phone_call(lambda store: {
        **store.status(device_id), "crypto": _phone_store().crypto_available()}), "status")


@router.post("/phone/key")
async def post_phone_key(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    await _scoped_acquire(_phone_key_limiter, profile)
    body = await _read_json_body(request, PHONE_MAX_KEY_BODY_BYTES)
    return await _phone_route(response, profile, lambda: _phone_call(lambda store: store.register_key(body)), "key")


@router.post("/phone/ingest")
async def post_phone_ingest(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    await _scoped_acquire(_phone_ingest_limiter, profile)
    body = await _read_json_body(request, PHONE_MAX_INGEST_BODY_BYTES)
    return await _phone_route(response, profile, lambda: _phone_call(lambda store: store.ingest(body)), "upload")


@router.post("/phone/exchange")
async def post_phone_exchange(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    await _scoped_acquire(_phone_exchange_limiter, profile)
    body = await _read_json_body(request, PHONE_MAX_EXCHANGE_BODY_BYTES)
    return await _phone_route(response, profile, lambda: _phone_call(lambda store: store.exchange(body)), "exchange")


@router.post("/phone/wipe")
async def post_phone_wipe(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    await _scoped_acquire(_phone_wipe_limiter, profile)
    body = await _read_json_body(request, PHONE_MAX_KEY_BODY_BYTES)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object", headers={"Cache-Control": "no-store"})
    # Everything only when asked for by name; a device id alone deletes that device's data.
    if body.get("all") is True and body.get("device_id") is not None:
        raise HTTPException(status_code=400, detail="Name a device_id or all: true, not both",
                            headers={"Cache-Control": "no-store"})
    device_id = None if body.get("all") is True else body.get("device_id")
    if device_id is None and body.get("all") is not True:
        raise HTTPException(status_code=400, detail="Name a device_id, or all: true",
                            headers={"Cache-Control": "no-store"})
    return await _phone_route(response, profile, lambda: _phone_call(lambda store: store.wipe(device_id)), "delete")


# --- Desktop views (#454) ----------------------------------------------------
#
# Which chats Hermes Desktop (or the web dashboard) has open on this host, so
# Talaria can count them as read without writing Hermes' own read flag (that
# flag would light Desktop's unread dot on the chat it has open). Hermes has
# no hook for "a client opened a chat", so this wraps the gateway's
# session.activate / session.resume handlers in this process. That is an
# unofficial seam: every attribute is feature-detected, the wrapper never
# raises and always returns the handler's own response, and when something
# is missing (a Hermes refactor, plugins.isolation: host) the route says
# observing: false and Talaria keeps its own read tracking.
#
# Per chat it keeps ``opened_at`` (the newest open) and ``seen_through``: how
# long the chat then stayed the one that Desktop connection had selected. A
# connection's selection moves with its next open and ends when its socket
# closes; the host can't tell whether anyone is looking at the window.

DESKTOP_VIEW_METHODS = ("session.activate", "session.resume")
DESKTOP_VIEWS_FILE = "talaria-desktop-views.json"
DESKTOP_VIEWS_MAX = 2000
DESKTOP_VIEWS_FLUSH_DELAY_S = 2.0
DESKTOP_VIEWS_INSTALL_POLL_S = 1.0
DESKTOP_VIEWS_INSTALL_WINDOW_S = 600.0
# How often a selected chat's connection is checked: a chat stops counting as
# seen at most this long after its Desktop disconnects.
DESKTOP_VIEWS_SWEEP_S = 5.0
# Set on each wrapper (to the handler it wraps), so a second load of this
# module in the same process never wraps a wrapper.
_DESKTOP_VIEW_MARKER = "__talaria_desktop_view__"
# The hook a wrapper reports to: the newest load of this module takes over
# the wrappers an earlier load installed.
_DESKTOP_VIEW_OWNER = "__talaria_desktop_view_owner__"


def desktop_view_client(transport: Any, socket_type: Optional[type] = None) -> Optional[str]:
    """Returns "desktop" or "browser" for a browser engine's gateway socket, else None.

    Desktop's renderer and the web dashboard open /api/ws from a browser
    engine, whose User-Agent starts with Mozilla/ (Desktop's adds Electron/).
    Talaria's socket comes from URLSession and also names itself in
    X-Talaria-Client. Stdio, hosted rooms and this plugin's own job transport
    aren't WebSocket transports, so they never count: a socket is the
    gateway's when it is an instance of ``socket_type`` (Hermes'
    tui_gateway.ws.WSTransport, subclasses included), or else when its class
    is named WSTransport. The User-Agent is the client's own
    claim: this is best-effort sorting among clients that already hold a
    dashboard login, not proof of which app is on the other end.
    """
    if not _is_gateway_socket(transport, socket_type):
        return None
    return _client_from_headers(_transport_headers(transport))


def _client_from_headers(headers: Optional[Dict[str, Any]]) -> Optional[str]:
    if headers is None or headers.get("x-talaria-client") is not None:
        return None
    agent = str(headers.get("user-agent") or "")
    if not agent.startswith("Mozilla/"):
        return None
    return "desktop" if "Electron/" in agent else "browser"


def _is_gateway_socket(transport: Any, socket_type: Optional[type]) -> bool:
    """Hermes' WSTransport: an instance of the gateway's class (``socket_type``,
    a single class; subclasses included), or a class named WSTransport, which
    also covers sockets opened before the gateway's socket module was
    reloaded."""
    if transport is None:
        return False
    if isinstance(socket_type, type) and isinstance(transport, socket_type):
        return True
    return type(transport).__name__ == "WSTransport"


def _transport_headers(transport: Any) -> Optional[Dict[str, Any]]:
    """The WebSocket upgrade's headers with lower-cased names, or None."""
    try:
        headers = getattr(getattr(transport, "_ws", None), "headers", None)
        items = getattr(headers, "items", None)
        if not callable(items):
            return None
        lowered: Dict[str, Any] = {}
        for name, value in items():
            # Raw ASGI headers are latin-1 bytes pairs.
            if isinstance(name, bytes):
                name = name.decode("latin-1")
            if isinstance(value, bytes):
                value = value.decode("latin-1")
            # The first of repeated headers, as Starlette's own get() reads.
            lowered.setdefault(str(name).lower(), value)
        return lowered
    except Exception:  # noqa: BLE001
        return None


def _desktop_views_key(home: Any) -> str:
    return os.path.realpath(str(home))


def _desktop_view_time(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:  # an int too large for a float
        return False


def _transport_closed(transport: Any) -> bool:
    """Hermes' WSTransport.closed; anything else counts as closed, so a
    selection is never held open on a socket whose state can't be read."""
    try:
        closed = getattr(transport, "closed", None)
    except Exception:  # noqa: BLE001 — a property that fails reads as closed
        return True
    return closed if isinstance(closed, bool) else True


def _merge_desktop_view(current: Optional[Dict[str, Any]], newer: Dict[str, Any]) -> Dict[str, Any]:
    """Each time keeps its newest value; the client follows the newest open."""
    if current is None:
        return dict(newer)
    merged = dict(current)
    if "opened_at" in newer and newer["opened_at"] >= merged.get("opened_at", 0.0):
        merged["opened_at"] = newer["opened_at"]
        merged["client"] = newer.get("client", merged.get("client"))
    merged["seen_through"] = max(merged.get("seen_through", 0.0), newer.get("seen_through", 0.0))
    return merged


class _DesktopViewStore:
    """Per Hermes home, in ``<home>/talaria-desktop-views.json``. Records land
    in memory at once and reach disk in one debounced write, so a chat switch
    never waits on I/O."""

    def __init__(self, flush_delay: float = DESKTOP_VIEWS_FLUSH_DELAY_S) -> None:
        self._lock = threading.Lock()
        self._pending: Dict[str, Dict[str, Dict[str, Any]]] = {}
        # Batches taken by a flush and not yet on disk; reads still see them.
        self._inflight: List[Dict[str, Dict[str, Dict[str, Any]]]] = []
        self._timer: Optional[threading.Timer] = None
        self._flush_delay = flush_delay

    def record(self, home: Any, stored_id: str, client: str, seen_through: float,
               opened_at: Optional[float] = None) -> None:
        entry: Dict[str, Any] = {"client": client, "seen_through": seen_through}
        if opened_at is not None:
            entry["opened_at"] = opened_at
        key = _desktop_views_key(home)
        with self._lock:
            views = self._pending.setdefault(key, {})
            views[stored_id] = _merge_desktop_view(views.get(stored_id), entry)
            if self._timer is None:
                self._timer = threading.Timer(self._flush_delay, self.flush)
                self._timer.daemon = True
                self._timer.start()

    def flush(self) -> None:
        with self._lock:
            pending, self._pending = self._pending, {}
            self._inflight.append(pending)
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        try:
            for key, views in pending.items():
                try:
                    self._merge_to_disk(key, views)
                except Exception:  # noqa: BLE001 — a lost write only leaves a chat unread
                    logger.warning("Talaria: could not save Desktop views under %s", key, exc_info=True)
        finally:
            with self._lock:
                self._inflight = [batch for batch in self._inflight if batch is not pending]

    def read(self, home: Any) -> Dict[str, Dict[str, Any]]:
        key = _desktop_views_key(home)
        # Memory first, then the file: a flush that lands in between is then
        # in the file, and merging it twice changes nothing.
        with self._lock:
            batches = [dict(batch.get(key, {})) for batch in self._inflight]
            batches.append(dict(self._pending.get(key, {})))
        views = self._load(self._path(key))
        for batch in batches:
            views = self._merged(views, batch)
        return views

    @staticmethod
    def _path(key: str) -> str:
        return os.path.join(key, DESKTOP_VIEWS_FILE)

    @staticmethod
    def _load(path: str, *, for_write: bool = False) -> Dict[str, Dict[str, Any]]:
        try:
            with open(path, encoding="utf-8") as handle:
                value = json.load(handle)
        except (FileNotFoundError, ValueError, RecursionError):
            return {}
        except OSError:
            if for_write:
                raise
            return {}
        views = value.get("views") if isinstance(value, dict) else None
        if not isinstance(views, dict):
            return {}
        clean: Dict[str, Dict[str, Any]] = {}
        for stored_id, entry in views.items():
            if not isinstance(stored_id, str) or not isinstance(entry, dict):
                continue
            times = [entry.get("opened_at"), entry.get("seen_through")]
            if not all(_desktop_view_time(stamp) for stamp in times):
                continue
            clean[stored_id] = {"opened_at": float(times[0]), "seen_through": float(times[1]),
                                "client": "browser" if entry.get("client") == "browser" else "desktop"}
        return clean

    @staticmethod
    def _merged(base: Dict[str, Dict[str, Any]], newer: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        merged = dict(base)
        for stored_id, entry in newer.items():
            merged[stored_id] = _merge_desktop_view(merged.get(stored_id), entry)
        # A pending focus change for a chat this file no longer has (pruned)
        # carries no open time; it is still at least seen until then.
        for entry in merged.values():
            entry.setdefault("opened_at", entry["seen_through"])
        if len(merged) > DESKTOP_VIEWS_MAX:
            newest = sorted(merged.items(), key=lambda item: item[1]["seen_through"], reverse=True)
            merged = dict(newest[:DESKTOP_VIEWS_MAX])
        return merged

    def _merge_to_disk(self, key: str, views: Dict[str, Dict[str, Any]]) -> None:
        path = self._path(key)
        # Path-generic: locks the sibling ".talaria-desktop-views.json.lock".
        with _pairing_state_lock(Path(path)):
            merged = self._merged(self._load(path, for_write=True), views)
            # A fresh, exclusively created 0600 name, so nothing planted in
            # the home can redirect the write.
            fd, temp = tempfile.mkstemp(dir=key, prefix=f".{DESKTOP_VIEWS_FILE}.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump({"version": 1, "views": merged}, handle, separators=(",", ":"))
                os.replace(temp, path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(temp)
                raise


_desktop_selected_module: Any = None
_desktop_selected_lock = threading.Lock()


def _desktop_selected() -> Any:
    """desktop_selected.py beside this folder, loaded once."""
    global _desktop_selected_module
    with _desktop_selected_lock:
        if _desktop_selected_module is None:
            path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "desktop_selected.py")
            spec = importlib.util.spec_from_file_location("talaria_push_dashboard_desktop_selected", path)
            if spec is None or spec.loader is None:
                raise RuntimeError("This plugin install is missing desktop_selected.py; update the plugin")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _desktop_selected_module = module
        return _desktop_selected_module


class _DesktopViewHook:
    """Wraps the gateway's open handlers once ``tui_gateway.server`` is loaded,
    and follows which chat each Desktop connection has selected.

    The plugin loads before the gateway module (the dashboard imports it in
    its lifespan), so installation waits for it on a daemon thread, and every
    route call retries too. Never imports the gateway itself: a module this
    plugin imported would be a private copy whose chats no client uses.
    """

    def __init__(self, store: _DesktopViewStore) -> None:
        self.store = store
        self._lock = threading.Lock()
        self._focus_lock = threading.Lock()
        # id(transport) -> the chat that connection has selected.
        self._focus: Dict[int, Dict[str, Any]] = {}
        # Home key -> (selected stored ids, when written) last published for
        # the reply-ready push (#87, desktop_selected.py).
        self._published: Dict[str, Tuple[frozenset, float]] = {}
        self._publish_lock = threading.Lock()
        # Wakes the sweep early after an open, so a new selection is published
        # within moments rather than at the next 5 s sweep.
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.reason: Optional[str] = "waiting"
        # Set when a gateway socket's upgrade headers can't be read, which
        # leaves every open unsortable: reported, not silently ignored.
        self.headers_unreadable = False
        # tui_gateway.ws.WSTransport, found at install. Like headers_unreadable
        # it is read from handler threads without a lock: one attribute
        # read is atomic, and a stale value only affects that one open.
        self.socket_type: Optional[type] = None

    @property
    def installed(self) -> bool:
        return self.reason is None

    @property
    def observing(self) -> bool:
        return self.installed and not self.headers_unreadable

    def status(self) -> Tuple[bool, Optional[str]]:
        if self.installed and self.headers_unreadable:
            return False, "gateway-unsupported"
        return self.observing, self.reason

    def ensure_started(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            thread = threading.Thread(target=self._run, name="talaria-desktop-views", daemon=True)
            try:
                thread.start()
            except Exception:  # noqa: BLE001 — the next route call tries again
                logger.debug("Talaria: Desktop view watcher not started", exc_info=True)
                return
            self._thread = thread

    def _run(self) -> None:
        try:
            self._watch()
        finally:
            with self._lock:
                if self._thread is threading.current_thread():
                    # The next route call starts a fresh watcher.
                    self._thread = None

    def try_install_quietly(self) -> bool:
        try:
            return self.try_install()
        except Exception:  # noqa: BLE001 — keep waiting for the gateway
            logger.debug("Talaria: Desktop view install failed", exc_info=True)
            return False

    def _watch(self) -> None:
        deadline = time.monotonic() + DESKTOP_VIEWS_INSTALL_WINDOW_S
        while not self.try_install_quietly():
            if time.monotonic() >= deadline:
                logger.info("Talaria: not marking chats Desktop opens as read here (%s)", self.reason)
                return
            time.sleep(DESKTOP_VIEWS_INSTALL_POLL_S)
        while True:
            self._wake.wait(DESKTOP_VIEWS_SWEEP_S)
            self._wake.clear()
            try:
                if self.installed:
                    self.verify()
                else:
                    self.try_install()  # a re-install that failed earlier
                self.sweep()
            except Exception:  # noqa: BLE001 — keep watching
                logger.debug("Talaria: Desktop view sweep failed", exc_info=True)

    def verify(self) -> None:
        """Re-installs if the gateway's handlers were replaced (a reload), and
        says so meanwhile instead of claiming to observe."""
        with self._lock:
            if not self.installed:
                return
            server = sys.modules.get("tui_gateway.server")
            methods = getattr(server, "_methods", None)
            intact = isinstance(methods, dict) and all(
                getattr(methods.get(name), _DESKTOP_VIEW_MARKER, None) is not None for name in DESKTOP_VIEW_METHODS)
            if intact:
                # The wrappers stayed; follow a reloaded socket module anyway.
                ws_transport = getattr(sys.modules.get("tui_gateway.ws"), "WSTransport", None)
                if isinstance(ws_transport, type):
                    self.socket_type = ws_transport
                return
            self.reason = "waiting"
        self.try_install()

    def try_install(self) -> bool:
        with self._lock:
            if self.installed:
                return True
            server = sys.modules.get("tui_gateway.server")
            if server is None:
                # Under plugins.isolation: host this process never gets one.
                self.reason = "gateway-not-in-process"
                return False
            methods = getattr(server, "_methods", None)
            sessions = getattr(server, "_sessions", None)
            transports = sys.modules.get("tui_gateway.transport")
            current_transport = getattr(transports, "current_transport", None)
            # Opens are sorted by the WebSocket transport's class name.
            ws_transport = getattr(sys.modules.get("tui_gateway.ws"), "WSTransport", None)
            if (not isinstance(methods, dict) or not callable(getattr(sessions, "get", None))
                    or not callable(getattr(server, "_session_home", None))
                    or not callable(current_transport) or not isinstance(ws_transport, type)
                    or not all(callable(methods.get(name)) for name in DESKTOP_VIEW_METHODS)
                    # The wrapper reads the handler's return value: a
                    # coroutine handler would hand it an awaitable instead.
                    or any(inspect.iscoroutinefunction(methods[name]) for name in DESKTOP_VIEW_METHODS)):
                # Mid-import (the table fills as the gateway loads), or a
                # Hermes that moved these.
                self.reason = "gateway-unsupported"
                return False
            self.socket_type = ws_transport
            for name in DESKTOP_VIEW_METHODS:
                handler = methods[name]
                if getattr(handler, _DESKTOP_VIEW_MARKER, None) is None:
                    # In place: the dispatcher looks handlers up at call time.
                    methods[name] = self._wrap(handler, server, current_transport)
                else:
                    # An earlier load of this module wrapped it: opens now
                    # come here, where the route reads.
                    setattr(handler, _DESKTOP_VIEW_OWNER, self)
            self.reason = None
            logger.info("Talaria: marking chats Desktop opens as read")
            return True

    def _wrap(self, handler: Callable[..., Any], server: Any, current_transport: Callable[[], Any]) -> Callable[..., Any]:
        @functools.wraps(handler)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            response = handler(*args, **kwargs)
            try:
                owner = getattr(wrapper, _DESKTOP_VIEW_OWNER, self)
                owner._observe(response, server, current_transport())
            except Exception:  # noqa: BLE001 — never fails the client's call
                logger.debug("Talaria: Desktop view not recorded", exc_info=True)
            return response

        setattr(wrapper, _DESKTOP_VIEW_MARKER, handler)
        setattr(wrapper, _DESKTOP_VIEW_OWNER, self)
        return wrapper

    def _observe(self, response: Any, server: Any, transport: Any) -> None:
        result = response.get("result") if isinstance(response, dict) else None
        if not isinstance(result, dict):
            return
        if not _is_gateway_socket(transport, self.socket_type):
            return
        headers = _transport_headers(transport)
        readable = headers is not None
        if readable == self.headers_unreadable:
            logger.warning("Talaria: gateway socket headers %s",
                           "readable again" if readable else "unreadable; not marking Desktop's chats read")
        self.headers_unreadable = not readable
        if not readable:
            return
        client = _client_from_headers(headers)
        stored_id = str(result.get("session_key") or "").strip()
        if client is None or not stored_id:
            return
        session = server._sessions.get(str(result.get("session_id") or ""))
        home_of = getattr(server, "_session_home", None)
        if not isinstance(session, dict) or not callable(home_of):
            return  # whose profile it is is unknown: a guess could mark another profile's chat
        home = home_of(session)
        if home is None:
            return
        now = time.time()
        self.store.record(home, stored_id, client, now, opened_at=now)
        try:
            ref = weakref.ref(transport)
        except TypeError:
            return  # can't follow this socket's life: the open alone counts
        with self._focus_lock:
            previous = self._focus.get(id(transport))
            if previous is not None:
                # The connection moved on: its last chat was seen until now
                # (or, for a closed socket whose id was reused, its last check).
                seen_until = now if previous["ref"]() is transport else previous["alive_at"]
                self.store.record(previous["home"], previous["stored_id"], previous["client"], seen_until)
            self._focus[id(transport)] = {"ref": ref, "home": _desktop_views_key(home), "stored_id": stored_id,
                                          "client": client, "alive_at": now}
        self._wake.set()

    def sweep(self, now: Optional[float] = None) -> None:
        """Ends the selection of connections that closed, at their last check."""
        now = time.time() if now is None else now
        with self._focus_lock:
            for key, focus in list(self._focus.items()):
                transport = focus["ref"]()
                if transport is None or _transport_closed(transport):
                    self.store.record(focus["home"], focus["stored_id"], focus["client"], focus["alive_at"])
                    del self._focus[key]
                else:
                    focus["alive_at"] = now
            selection: Dict[str, set] = {}
            for focus in self._focus.values():
                selection.setdefault(focus["home"], set()).add(focus["stored_id"])
        self._publish_selection(selection, now)

    def _publish_selection(self, selection: Dict[str, set], now: float) -> None:
        """Writes each home's selected chats for the turn hooks (#87) when they
        change, and renews a selection before readers stop trusting it."""
        try:
            module = _desktop_selected()
        except Exception:  # noqa: BLE001 — a broken install publishes nothing; readers then push
            logger.debug("Talaria: desktop_selected.py unavailable", exc_info=True)
            return
        with self._publish_lock:
            for key in set(selection) | set(self._published):
                ids = frozenset(selection.get(key, ()))
                last = self._published.get(key)
                if last is not None and last[0] == ids and (not ids or now - last[1] < module.RENEW_S):
                    continue
                try:
                    module.publish(key, ids, now=now)
                except Exception:  # noqa: BLE001 — readers then see nothing selected; the next sweep retries
                    logger.debug("Talaria: could not publish Desktop's selected chats under %s", key, exc_info=True)
                    continue
                if ids:
                    self._published[key] = (ids, now)
                else:
                    self._published.pop(key, None)

    def read(self, home: Any) -> Dict[str, Dict[str, Any]]:
        """Saved views plus the chats Desktop has selected right now."""
        self.sweep()
        views = self.store.read(home)
        key = _desktop_views_key(home)
        with self._focus_lock:
            selected = [dict(focus) for focus in self._focus.values() if focus["home"] == key]
        for focus in selected:
            entry = _merge_desktop_view(views.get(focus["stored_id"]),
                                        {"client": focus["client"], "seen_through": focus["alive_at"]})
            entry.setdefault("opened_at", focus["alive_at"])
            entry["open"] = True
            views[focus["stored_id"]] = entry
        return views


_desktop_view_store = _DesktopViewStore()
_desktop_view_hook = _DesktopViewHook(_desktop_view_store)
# Talaria reads every 15 s per profile while its chat list is on screen.
_desktop_views_limiter = _MintLimiter(60, 60.0, message="Too many Desktop view reads; try again shortly")
atexit.register(_desktop_view_store.flush)
if "hermes_cli.web_server" in sys.modules:
    # Loaded by the dashboard (not a test or the CLI): start watching for the
    # gateway module now rather than at Talaria's first read.
    _desktop_view_hook.ensure_started()


@router.get("/sessions/desktop-views")
async def get_desktop_views(response: Response, profile: Optional[str] = None,
                            since: Optional[float] = None) -> Dict[str, Any]:
    """Chats Desktop or the web dashboard opened in ``profile``: per stored id,
    the newest open and how long it then stayed selected (seconds since the
    epoch), optionally only those seen after ``since``."""
    response.headers["Cache-Control"] = "no-store"
    _desktop_view_hook.ensure_started()
    _desktop_view_hook.try_install_quietly()  # a half-loaded gateway never fails the read

    def read() -> Dict[str, Dict[str, Any]]:
        _desktop_views_limiter.acquire(_limiter_key(profile))
        from hermes_constants import get_hermes_home

        return _desktop_view_hook.read(get_hermes_home())

    try:
        views = await _run_scoped(profile, read)
    except TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        # Hermes' own 400/404 for a bad or unknown profile, kept out of caches too.
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise _unexpected("read", exc, feature="Desktop views")
    if since is not None and math.isfinite(since):
        views = {stored_id: entry for stored_id, entry in views.items() if entry["seen_through"] > since}
    observing, reason = _desktop_view_hook.status()
    return {"ok": True, "observing": observing, "reason": reason, "views": views}


# --- Room files ----------------------------------------------------------------
#
# A group chat room isn't a session, so Hermes' attach RPCs (file.attach and
# friends), which stage into a session's workspace, can't take a file for it.
# Talaria uploads the file here instead and puts an `@file:` reference to the
# stored path in the room message; the room's members run on this host and read
# it from there. Files live under <hermes home>/talaria/room-files/<id>/ and are
# swept a month after their upload.

ROOM_FILE_MAX_BYTES = 50 * 1024 * 1024
# base64 grows a file by a third; the rest is the JSON around it.
ROOM_FILE_MAX_BODY_BYTES = ROOM_FILE_MAX_BYTES * 4 // 3 + 64 * 1024
ROOM_FILE_RETENTION_S = 30 * 24 * 3600
# All uploads together; the oldest go first past it.
ROOM_FILE_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
# Never fill the disk: an upload leaves at least this much free.
ROOM_FILE_FREE_FLOOR_BYTES = 1024 * 1024 * 1024
ROOM_FILE_SWEEP_INTERVAL_S = 3600.0
ROOM_FILE_LIMIT = 60
ROOM_FILE_WINDOW_S = 600.0
_room_file_limiter = _MintLimiter(ROOM_FILE_LIMIT, ROOM_FILE_WINDOW_S,
                                  message="Too many file uploads; try again shortly")
_ROOM_FILE_UNSAFE = re.compile(r"[\\/\x00-\x1f\x7f\x85\u2028\u2029:*?\"'`<>|\u200b-\u200f\u202a-\u202e\u2066-\u2069]+")
# Filesystems cap one path component at 255 bytes; leave room for UTF-8.
ROOM_FILE_NAME_MAX_BYTES = 180


def _room_file_name(raw: Any) -> str:
    """The upload's own name, made safe to use as one path component."""
    # Only the first part of an absurd name is worth looking at.
    raw = raw if isinstance(raw, str) else ""
    # Lone surrogates (valid JSON escapes) can't be encoded: drop them.
    raw = raw[:1024].encode("utf-8", "ignore").decode("utf-8")
    name = _ROOM_FILE_UNSAFE.sub("_", raw.strip()).strip(" .")
    if not name:
        return "file"
    if len(name.encode("utf-8")) <= ROOM_FILE_NAME_MAX_BYTES:
        return name
    stem, dot, ext = name.rpartition(".")
    if not (dot and stem and 0 < len(ext.encode("utf-8")) <= 16):
        stem, ext = name, ""
    suffix = "." + ext if ext else ""
    budget = ROOM_FILE_NAME_MAX_BYTES - len(suffix.encode("utf-8"))
    # Cut whole characters, never a UTF-8 sequence in half.
    stem = stem.encode("utf-8")[:budget].decode("utf-8", errors="ignore")
    return stem + suffix


def _room_files_root() -> str:
    from hermes_constants import get_hermes_home

    return os.path.join(str(get_hermes_home()), "talaria", "room-files")


_room_file_last_sweep: Dict[str, float] = {}
# Uploads wait on one another (the lock below), so they get their own
# threads rather than the default pool other routes share.
_room_file_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="talaria-room-files")
# Sweeps, the free-space check and the write run one at a time, so two
# uploads can't both pass the check and then fill the disk together.
_room_file_lock = threading.Lock()


def _sweep_room_files(root: str, now: float, incoming: int = 0) -> None:
    """Deletes uploads past retention, then the oldest until the rest and
    ``incoming`` fit the total cap. Runs at most hourly per root, and again
    whenever the free-disk floor would be passed, so the cap can be overrun
    for up to an hour; the floor guards the disk at all times."""
    try:
        with os.scandir(root) as it:
            entries = [entry for entry in it if entry.is_dir(follow_symlinks=False)]
    except OSError:
        return
    folders = []
    for entry in entries:
        try:
            mtime = entry.stat(follow_symlinks=False).st_mtime
            if now - mtime > ROOM_FILE_RETENTION_S:
                shutil.rmtree(entry.path, ignore_errors=True)
                continue
            with os.scandir(entry.path) as files:
                size = sum(f.stat(follow_symlinks=False).st_size for f in files
                           if f.is_file(follow_symlinks=False))
            folders.append((mtime, size, entry.path))
        except OSError:
            continue
    total = sum(size for _, size, _ in folders) + incoming
    for _, size, path in sorted(folders):
        if total <= ROOM_FILE_TOTAL_BYTES:
            break
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.lexists(path):
            total -= size
    _room_file_last_sweep[root] = now


def _room_file_free(root: str) -> int:
    """Free bytes on the room-files disk; an unreadable disk counts as full."""
    try:
        return shutil.disk_usage(root).free
    except OSError:
        return 0


def store_room_file(root: str, body: Any, now: Optional[float] = None) -> Dict[str, Any]:
    """Writes one uploaded file under ``root``; returns where it went and the
    `@file:` reference Hermes reads."""
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object", headers={"Cache-Control": "no-store"})
    encoded = body.get("data_base64")
    if not isinstance(encoded, str) or not encoded:
        raise HTTPException(status_code=400, detail="data_base64 is required", headers={"Cache-Control": "no-store"})
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="data_base64 is not base64", headers={"Cache-Control": "no-store"})
    if len(data) > ROOM_FILE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="The file is too large", headers={"Cache-Control": "no-store"})
    now = time.time() if now is None else now
    name = _room_file_name(body.get("name"))
    with _room_file_lock:
        if now - _room_file_last_sweep.get(root, float("-inf")) >= ROOM_FILE_SWEEP_INTERVAL_S:
            _sweep_room_files(root, now, len(data))
        try:
            os.makedirs(root, mode=0o700, exist_ok=True)
        except OSError as exc:
            if exc.errno in (errno.ENOSPC, errno.EDQUOT):
                raise HTTPException(status_code=507, detail="The host is low on disk space",
                                    headers={"Cache-Control": "no-store"})
            raise
        try:
            # makedirs leaves an existing folder's mode alone.
            os.chmod(root, 0o700)
        except OSError:
            pass
        if _room_file_free(root) - len(data) < ROOM_FILE_FREE_FLOOR_BYTES:
            # The cap may be what's full: make room once more before
            # refusing, unless the hourly sweep just did.
            if _room_file_last_sweep.get(root) != now:
                _sweep_room_files(root, now, len(data))
            if _room_file_free(root) - len(data) < ROOM_FILE_FREE_FLOOR_BYTES:
                raise HTTPException(status_code=507, detail="The host is low on disk space",
                                    headers={"Cache-Control": "no-store"})
        folder = os.path.join(root, time.strftime("%Y%m%d", time.gmtime(now)) + "-" + uuid.uuid4().hex)
        path = os.path.join(folder, name)
        try:
            os.makedirs(folder, mode=0o700, exist_ok=False)
            # Readable by the host's own user only, whatever the umask.
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
                handle.write(data)
        except OSError as exc:
            # A folder that was already there is another upload's: keep it.
            if exc.errno != errno.EEXIST:
                shutil.rmtree(folder, ignore_errors=True)
            if exc.errno in (errno.ENOSPC, errno.EDQUOT):
                raise HTTPException(status_code=507, detail="The host is low on disk space",
                                    headers={"Cache-Control": "no-store"})
            raise
    return {"path": path, "name": name, "size": len(data), "ref_text": "@file:" + _room_file_reference(path)}


def _room_file_reference(path: str) -> str:
    """Quotes a path holding whitespace, brackets or quotes, as Hermes'
    format_reference_value does, so the reference reads back whole."""
    if not re.search(r"[\s\[\](){}'\"`]", path):
        return path
    for quote in ("`", '"', "'"):
        if quote not in path:
            return quote + path + quote
    return path


@router.post("/room-files")
async def post_room_file(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    key = _limiter_key(profile)
    try:
        mint = _room_file_limiter.acquire(key)
    except TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    try:
        body = await _read_json_body(request, ROOM_FILE_MAX_BODY_BYTES)
        stored = await _run_scoped(profile, lambda: store_room_file(_room_files_root(), body), _room_file_executor)
    except TokenError as exc:
        # The profile couldn't be scoped; nothing was stored.
        _room_file_limiter.release(key, mint)
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        # Refused before anything was stored (507 included): no slot spent.
        if exc.status_code < 500 or exc.status_code == 507:
            _room_file_limiter.release(key, mint)
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        # Nothing was stored: a failed upload costs no slot.
        _room_file_limiter.release(key, mint)
        raise _unexpected("upload", exc, feature="Room files")
    return {"ok": True, **stored}


# --- Room pictures -------------------------------------------------------------
#
# Hermes' gateway-hosted rooms (groups.*) carry no picture, so Talaria keeps a
# room's custom picture here, under <hermes home>/talaria/room-images/: per
# room, a small `<hash>.json` record (room id, revision, type) and the
# picture's bytes in `<hash>.img`, named by a hash of the room id. GET
# /room-images lists each room's revision from the records alone, so Talaria
# fetches only pictures it hasn't cached; GET /room-images/{room_id} returns
# one; PUT sets it and DELETE clears it. Talaria sends a downscaled JPEG or
# PNG. Anyone who can reach these routes is signed in to the dashboard, the
# same trust every other route here assumes.

ROOM_IMAGE_MAX_BYTES = 512 * 1024
ROOM_IMAGE_MAX_BODY_BYTES = ROOM_IMAGE_MAX_BYTES * 4 // 3 + 16 * 1024
# Records beyond this many are never listed; the oldest go first on a write.
ROOM_IMAGE_MAX_ROOMS = 500
# A record is a few hundred bytes; anything larger isn't one.
ROOM_IMAGE_RECORD_MAX_BYTES = 4096
# Temp files a crashed write left behind go after this long.
ROOM_IMAGE_STALE_TEMP_S = 3600.0
# Taken by every process that reads or writes the folder's pictures.
ROOM_IMAGE_LOCK_FILE = ".room-images.lock"
ROOM_IMAGE_LIMIT = 60
ROOM_IMAGE_WINDOW_S = 600.0
_room_image_limiter = _MintLimiter(ROOM_IMAGE_LIMIT, ROOM_IMAGE_WINDOW_S,
                                   message="Too many picture changes; try again shortly")
_ROOM_IMAGE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
_room_image_lock = threading.Lock()
# Writes wait on one another (the locks below), so they get their own threads
# rather than the default pool other routes share.
_room_image_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="talaria-room-images")


def _room_images_root() -> str:
    from hermes_constants import get_hermes_home

    return os.path.join(str(get_hermes_home()), "talaria", "room-images")


def _room_image_id(raw: Any) -> str:
    if not isinstance(raw, str) or not _ROOM_IMAGE_ID.match(raw):
        raise HTTPException(status_code=400, detail="Invalid room id", headers={"Cache-Control": "no-store"})
    return raw


def _room_image_stem(root: str, room_id: str) -> str:
    return os.path.join(root, hashlib.sha256(room_id.encode("utf-8")).hexdigest())


def _room_image_mime(data: bytes) -> Optional[str]:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    return None


def _read_no_follow(path: str, limit: int) -> Optional[bytes]:
    """A regular file's bytes up to ``limit``, never through a symlink
    (where the platform has O_NOFOLLOW); None when missing, larger, or not
    a plain file."""
    try:
        # Non-blocking, so a FIFO planted in its place can't hang the read.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    with os.fdopen(fd, "rb") as handle:
        try:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return None
        except OSError:
            return None
        data = handle.read(limit + 1)
    return data if len(data) <= limit else None


def _read_room_record(path: str) -> Optional[Dict[str, Any]]:
    raw = _read_no_follow(path, ROOM_IMAGE_RECORD_MAX_BYTES)
    if raw is None:
        return None
    try:
        record = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(record, dict) or not isinstance(record.get("room_id"), str) \
            or not isinstance(record.get("rev"), str) \
            or record.get("mime", "image/jpeg") not in ("image/jpeg", "image/png"):
        return None
    return record


@contextlib.contextmanager
def _room_images_locked(root: str, *, create: bool = False):
    """A picture's bytes and its record change and are read as a pair: under
    a lock file in the folder, which every process and thread takes through
    its own handle, then this process's lock. Without ``create``, a folder
    that isn't there (or a lock file that can't be opened) has nothing to
    pair, so only the thread lock is taken."""
    if create:
        os.makedirs(root, mode=0o700, exist_ok=True)
        with contextlib.suppress(OSError):
            # makedirs leaves an existing folder's mode alone.
            os.chmod(root, 0o700)
    lock_path = os.path.join(root, ROOM_IMAGE_LOCK_FILE)
    try:
        handle = _open_private_lock_file(lock_path)
    except OSError:
        if create:
            raise
        with _room_image_lock:
            yield
        return
    with handle, _locked_handle(handle), _room_image_lock:
        yield


def _write_atomic(root: str, path: str, data: bytes) -> None:
    # A fresh, exclusively created 0600 name, so nothing planted in the
    # folder can redirect the write.
    fd, temp = tempfile.mkstemp(dir=root, prefix=".room-image.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise


def list_room_images(root: str) -> Dict[str, str]:
    """Each stored room's picture revision, keyed by room id."""
    try:
        with os.scandir(root) as it:
            names = sorted(entry.path for entry in it
                           if entry.name.endswith(".json") and entry.is_file(follow_symlinks=False))
    except OSError:
        return {}
    revisions: Dict[str, str] = {}
    for path in names[:ROOM_IMAGE_MAX_ROOMS]:
        record = _read_room_record(path)
        if record is not None:
            revisions[record["room_id"]] = record["rev"]
    return revisions


def read_room_image(root: str, room_id: str) -> Optional[Dict[str, Any]]:
    stem = _room_image_stem(root, room_id)
    # Under the write lock, so a record and its bytes are read as a pair.
    with _room_images_locked(root):
        record = _read_room_record(stem + ".json")
        if record is None or record["room_id"] != room_id:
            return None
        data = _read_no_follow(stem + ".img", ROOM_IMAGE_MAX_BYTES)
    if not data:
        return None
    return {"room_id": room_id, "rev": record["rev"], "mime": record.get("mime") or "image/jpeg",
            "data_base64": base64.b64encode(data).decode("ascii")}


def _remove_room_image_files(stem: str) -> None:
    for suffix in (".json", ".img"):
        with contextlib.suppress(OSError):
            os.unlink(stem + suffix)


def _prune_room_images(root: str, keep: str, now: float) -> None:
    """Drops the oldest rooms past the room cap (never ``keep``), and temp
    files a crashed write left behind."""
    records = []
    try:
        with os.scandir(root) as it:
            entries = list(it)
    except OSError:
        return
    names = {entry.name for entry in entries}
    for entry in entries:
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
            mtime = entry.stat(follow_symlinks=False).st_mtime
        except OSError:
            continue
        # Leftovers of a crashed write: a temp file, or bytes whose record
        # was never written.
        leftover = (entry.name.startswith(".room-image.") and entry.name.endswith(".tmp")) \
            or (entry.name.endswith(".img") and entry.name[:-len(".img")] + ".json" not in names)
        if leftover:
            if now - mtime > ROOM_IMAGE_STALE_TEMP_S:
                with contextlib.suppress(OSError):
                    os.unlink(entry.path)
        elif entry.name.endswith(".json"):
            records.append((mtime, entry.path[:-len(".json")]))
    excess = len(records) - ROOM_IMAGE_MAX_ROOMS
    for _, stem in sorted(records):
        if excess <= 0:
            break
        if stem == keep:
            continue
        _remove_room_image_files(stem)
        excess -= 1


def store_room_image(root: str, room_id: str, body: Any, now: Optional[float] = None) -> Dict[str, Any]:
    """Sets ``room_id``'s picture from ``body["data_base64"]``; returns its revision."""
    no_store = {"Cache-Control": "no-store"}
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object", headers=no_store)
    encoded = body.get("data_base64")
    if not isinstance(encoded, str) or not encoded:
        raise HTTPException(status_code=400, detail="data_base64 is required", headers=no_store)
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="data_base64 is not base64", headers=no_store)
    if len(data) > ROOM_IMAGE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="The picture is too large", headers=no_store)
    mime = _room_image_mime(data)
    if mime is None:
        raise HTTPException(status_code=415, detail="The picture must be a JPEG or PNG", headers=no_store)
    now = time.time() if now is None else now
    rev = f"{int(now * 1000)}-{hashlib.sha256(data).hexdigest()[:12]}"
    record = json.dumps({"version": 1, "room_id": room_id, "rev": rev, "mime": mime},
                        separators=(",", ":")).encode("utf-8")
    stem = _room_image_stem(root, room_id)
    try:
        with _room_images_locked(root, create=True):
            # Bytes first: a record always names a picture that's there.
            _write_atomic(root, stem + ".img", data)
            try:
                _write_atomic(root, stem + ".json", record)
            except OSError:
                # The old record would now label the new bytes with its
                # revision: drop the room instead, so clients refetch.
                _remove_room_image_files(stem)
                raise
            _prune_room_images(root, stem, now)
    except OSError as exc:
        if exc.errno in (errno.ENOSPC, errno.EDQUOT):
            raise HTTPException(status_code=507, detail="The host is low on disk space", headers=no_store)
        raise
    return {"room_id": room_id, "rev": rev, "mime": mime}


def clear_room_image(root: str, room_id: str) -> bool:
    stem = _room_image_stem(root, room_id)
    with _room_images_locked(root):
        record = _read_room_record(stem + ".json")
        if record is None or record["room_id"] != room_id:
            return False
        _remove_room_image_files(stem)
    return True


@router.get("/room-images")
async def get_room_images(response: Response) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        revisions = await asyncio.get_running_loop().run_in_executor(
            None, lambda: list_room_images(_room_images_root()))
    except Exception as exc:
        raise _unexpected("list", exc, feature="Room pictures")
    return {"ok": True, "revisions": revisions}


@router.get("/room-images/{room_id}")
async def get_room_image(room_id: str, response: Response) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    room_id = _room_image_id(room_id)
    try:
        record = await asyncio.get_running_loop().run_in_executor(
            None, lambda: read_room_image(_room_images_root(), room_id))
    except Exception as exc:
        raise _unexpected("read", exc, feature="Room pictures")
    if record is None:
        raise HTTPException(status_code=404, detail="No picture for this room", headers={"Cache-Control": "no-store"})
    return {"ok": True, **record}


@router.put("/room-images/{room_id}")
async def put_room_image(room_id: str, request: Request, response: Response) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    room_id = _room_image_id(room_id)
    # Pictures belong to the host, not a profile: one window for everyone.
    try:
        mint = _room_image_limiter.acquire("")
    except TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    try:
        body = await _read_json_body(request, ROOM_IMAGE_MAX_BODY_BYTES)
        stored = await asyncio.get_running_loop().run_in_executor(
            _room_image_executor, lambda: store_room_image(_room_images_root(), room_id, body))
    except HTTPException as exc:
        if exc.status_code < 500 or exc.status_code == 507:
            _room_image_limiter.release("", mint)
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        _room_image_limiter.release("", mint)
        raise _unexpected("save", exc, feature="Room pictures")
    return {"ok": True, **stored}


@router.delete("/room-images/{room_id}")
async def delete_room_image(room_id: str, response: Response) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    room_id = _room_image_id(room_id)
    try:
        removed = await asyncio.get_running_loop().run_in_executor(
            None, lambda: clear_room_image(_room_images_root(), room_id))
    except Exception as exc:
        raise _unexpected("clear", exc, feature="Room pictures")
    return {"ok": True, "removed": removed}


# --- Artifacts ---------------------------------------------------------------
#
# GET /artifacts lists what a profile's chats produced (artifacts_index.py
# beside this folder): files Hermes sent or wrote, the HTML pages it drew,
# files the user attached and links, newest first. Talaria's Artifacts screen
# reads every chat (no session_id) and a chat's own sheet reads one (each id
# it's known by as a session_id, with the sessions it continues). Paged by
# message id: pass the previous page's next_before as before. missing=hide
# leaves out files no longer on the host (an older plugin ignores it).

ARTIFACTS_LIMIT = 60
ARTIFACTS_WINDOW_S = 60.0
# The ids one chat is known by (its runtime and stored ids).
ARTIFACTS_MAX_SESSIONS = 16
ARTIFACTS_MAX_ID_LENGTH = 200
# Each read is bounded (rows scanned, sqlite's busy wait); this backstops it.
ARTIFACTS_TIMEOUT_S = 25.0
_artifacts_limiter = _MintLimiter(ARTIFACTS_LIMIT, ARTIFACTS_WINDOW_S,
                                  message="Too many artifact requests; try again shortly")
_artifacts_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="talaria-artifacts")
# A timed-out read keeps its worker until it ends, so a request finding both
# busy is turned away rather than queued behind them.
_artifacts_slots = threading.BoundedSemaphore(2)
_artifacts_module: Any = None
_artifacts_module_lock = threading.Lock()


def _artifacts_index() -> Any:
    """artifacts_index.py beside this folder, loaded once."""
    global _artifacts_module
    with _artifacts_module_lock:
        if _artifacts_module is None:
            path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "artifacts_index.py")
            spec = importlib.util.spec_from_file_location("talaria_push_dashboard_artifacts_index", path)
            if spec is None or spec.loader is None or not os.path.isfile(path):
                raise TokenError(501, "This plugin install is missing artifacts_index.py; update the plugin")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _artifacts_module = module
        return _artifacts_module


def list_artifacts(before: Optional[int] = None, session_ids: Optional[List[str]] = None,
                   kind: Optional[str] = None, q: str = "", limit: Optional[int] = None,
                   missing: Optional[str] = None) -> Dict[str, Any]:
    """One page of the profile's artifacts (called inside the profile scope)."""
    from hermes_constants import get_hermes_home

    index = _artifacts_index()
    if kind and kind not in index.GROUPS:
        raise TokenError(400, "kind must be images, pages, files or links")
    if missing not in (None, "", "show", "hide"):
        raise TokenError(400, "missing must be show or hide")
    try:
        conn = index.open_db(get_hermes_home())
    except FileNotFoundError:
        return {"items": [], "next_before": None}
    try:
        ids = [sid.strip() for sid in session_ids or []
               if sid and sid.strip() and len(sid) <= ARTIFACTS_MAX_ID_LENGTH][:ARTIFACTS_MAX_SESSIONS]
        if session_ids and not ids:
            # A chat asked for by ids none of which can name one: never
            # every chat instead.
            return {"items": [], "next_before": None}
        return index.list_artifacts(conn, before=before, session_ids=ids, group=kind or None,
                                    query=q or "", limit=index.DEFAULT_LIMIT if limit is None else limit,
                                    include_missing=missing != "hide")
    finally:
        conn.close()


@router.get("/artifacts")
async def get_artifacts(response: Response, profile: Optional[str] = None, before: Optional[int] = None,
                        session_id: Optional[List[str]] = Query(None), kind: Optional[str] = None,
                        q: Optional[str] = None, limit: Optional[int] = None,
                        missing: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    # The slot is freed exactly once: by the read when it ran, else by this
    # request (a read it gave up on before it started never runs).
    state = {"started": False, "abandoned": False}
    state_lock = threading.Lock()

    def job() -> Dict[str, Any]:
        with state_lock:
            if state["abandoned"]:
                return {"items": [], "next_before": None}
            state["started"] = True
        try:
            return list_artifacts(before, session_id, kind, q or "", limit, missing)
        finally:
            _artifacts_slots.release()

    try:
        await _scoped_acquire(_artifacts_limiter, profile)
        if not _artifacts_slots.acquire(blocking=False):
            raise HTTPException(status_code=503, detail="Hermes is still reading artifacts; try again shortly",
                                headers={"Cache-Control": "no-store"})
        try:
            page = await asyncio.wait_for(_run_scoped(profile, job, _artifacts_executor),
                                          timeout=ARTIFACTS_TIMEOUT_S)
        except BaseException:
            with state_lock:
                if not state["started"]:
                    state["abandoned"] = True
                    _artifacts_slots.release()
            raise
    except HTTPException:
        raise
    except asyncio.TimeoutError:
        logger.warning("Talaria artifacts list timed out after %ss", ARTIFACTS_TIMEOUT_S)
        raise HTTPException(status_code=504, detail="Listing artifacts took too long; try again",
                            headers={"Cache-Control": "no-store"})
    except TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except sqlite3.Error as exc:
        logger.warning("Talaria artifacts couldn't read the session store: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Hermes' session store is busy; try again shortly",
                            headers={"Cache-Control": "no-store"})
    except Exception as exc:
        raise _unexpected("list", exc, feature="Artifacts")
    return {"ok": True, **page}


# --- Capabilities ------------------------------------------------------------
#
# Talaria reads this once per connection to tell which of its features this
# plugin serves, and nudges an update when one it uses is missing. A plugin
# without this route predates it, which Talaria reads as "update". Names are
# stable feature ids, one per route family; add one with every new route.

ROUTE_CAPABILITIES = (
    "session-takeover",
    "e2e-notifications",
    "desktop-views",
    "hermes-calls",
    "hermes-call-outcomes",
    "talaria-settings",
    "present-v1",
    "health-sync",
    "phone-sync",
    "phone-alarms",
    "phone-contacts",
    "location-sync",
    "location-locate",
    # sharing.other_profiles: the host's other profiles paired with the
    # same phone read the location (location_profiles).
    "location-profiles",
    "pairing",
    "self-restart",
    "cron-notifications",
    "reply-sources",
    "chat-on-screen",
    "room-files",
    "room-images",
    "artifacts",
)


def _plugin_version() -> Optional[str]:
    """The version in plugin.yaml beside this folder, or None if unreadable."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugin.yaml")
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                key, _, value = line.partition(":")
                if key.strip() == "version":
                    return value.strip().strip("\"'") or None
    except (OSError, UnicodeDecodeError):
        return None
    return None


# The version this process imported. Hermes imports this file once per
# dashboard process, so after `hermes plugins update` the version on disk
# moves ahead of the code that is serving until the dashboard restarts.
LOADED_VERSION = _plugin_version()
# Changes with every dashboard process, so Talaria can tell a restart landed.
BOOT_ID = os.urandom(8).hex()


@router.get("/capabilities")
async def get_capabilities(response: Response) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    installed = _plugin_version()
    return {"ok": True, "version": installed, "loaded_version": LOADED_VERSION,
            "restart_needed": installed != LOADED_VERSION, "boot_id": BOOT_ID,
            "capabilities": list(ROUTE_CAPABILITIES)}


# --- Self restart -------------------------------------------------------------
#
# After an update, Hermes' gateway restart reloads the plugin's hooks and tools
# for the gateway, but this dashboard keeps serving the routes (and the hooks of
# the chats it runs) it imported at start. Talaria calls this last: the process
# re-executes itself with the command line it started with, keeping its pid, so
# systemd, launchd and a terminal launch all carry on. Refused while a chat turn
# this process runs is still going, for the Desktop app's own backend (it
# supervises and re-ports its child), inside a plugin host, and on Windows,
# where exec spawns a new process instead of replacing this one.

SELF_RESTART_DELAY_S = 1.0
SELF_RESTART_RECHECK_S = 2.0
_self_restart_lock = threading.Lock()
_self_restart_scheduled = False


def _hermes_homes() -> List[Path]:
    """Every profile home this host has: the root and each profiles/<name>."""
    try:
        from hermes_constants import get_default_hermes_root
    except (ImportError, AttributeError):  # an older Hermes: one home
        from hermes_constants import get_hermes_home
        root = Path(get_hermes_home())
    else:
        # A root that fails to resolve raises: guessing could miss a turn.
        root = Path(get_default_hermes_root())
    homes = [root]
    with contextlib.suppress(OSError):
        homes.extend(sorted(p for p in (root / "profiles").iterdir() if p.is_dir()))
    return homes


def _turn_marker_file(home: Path) -> Path:
    try:
        from tui_gateway import turn_marker
    except ImportError:  # a Hermes without turn markers: the path they used
        return home / "desktop" / "interrupted_turns.json"
    # A Hermes that moved its markers raises here: guessing could miss a turn.
    return Path(turn_marker._marker_path(home))


def _own_start_time() -> Optional[float]:
    try:
        from hermes_cli.active_sessions import _own_start_time as own
        start = own()
        return None if start is None else float(start)
    except Exception:  # noqa: BLE001
        return None


def _descends_from(pid: int, ancestor: int) -> bool:
    """Whether `pid` is a child (at any depth) of `ancestor`: an isolated turn's compute host."""
    if pid <= 0:
        return False
    try:
        import psutil  # a Hermes dependency
        return any(parent.pid == ancestor for parent in psutil.Process(pid).parents())
    except Exception:  # noqa: BLE001 — gone, or no psutil: not a child we can see
        return False


def _running_turns(pid: int, homes: List[Path], start_time: Optional[float] = None) -> int:
    """Turns this process has started and not finished, from Hermes' turn markers.

    Every Desktop/TUI/dashboard chat turn writes a marker with its writer's pid
    (and start time, tui_gateway.turn_marker) at start and clears it at the
    end. A marker naming this pid, or a child of it (an isolated turn's
    compute host), is a turn still running here, unless its start time shows
    an earlier process that had this pid. A missing file is none; one that
    can't be read raises, and Hermes rewrites an unparseable one from scratch.
    """
    running = 0
    for home in homes:
        try:
            entries = json.loads(_turn_marker_file(home).read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            continue
        if not isinstance(entries, dict):
            continue
        for entry in entries.values():
            if not isinstance(entry, dict):
                continue
            writer = _owner_pid({"pid": entry.get("writer_pid")})
            if writer != pid:
                running += 1 if _descends_from(writer, pid) else 0
                continue
            written = entry.get("writer_start_time")
            if start_time is not None and isinstance(written, (int, float)) and not isinstance(written, bool) \
                    and abs(float(written) - start_time) > 1.0:
                continue
            running += 1
    return running


def _self_restart_refusal() -> Optional[Tuple[int, str]]:
    if sys.platform == "win32":
        return 501, "Restart the Hermes dashboard on this host to finish the update"
    if os.environ.get("HERMES_PLUGIN_HOST_PROCESS") == "1":
        return 501, "This plugin runs in a Hermes plugin host; restart Hermes to finish the update"
    try:
        from hermes_cli.anon_auth import on_local_desktop_backend
        desktop = on_local_desktop_backend()
    except Exception:
        desktop = bool(os.environ.get("HERMES_DESKTOP_CHILD_PID"))
    if desktop:
        return 501, "This dashboard belongs to Hermes Desktop; restart Hermes Desktop to finish the update"
    if not getattr(sys, "orig_argv", None):
        return 501, "Restart the Hermes dashboard on this host to finish the update"
    return None


def _reexec() -> None:
    logger.warning("Talaria plugin: restarting this dashboard to load plugin %s", _plugin_version())
    # exec skips atexit: save what this module would have saved on the way out.
    with contextlib.suppress(Exception):
        _desktop_view_store.flush()
    for handler in logging.getLogger().handlers:
        with contextlib.suppress(Exception):
            handler.flush()
    with contextlib.suppress(Exception):
        sys.stdout.flush()
        sys.stderr.flush()
    os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])


def _restart_when_idle(schedule: Callable[[float, Callable[[], None]], Any],
                       homes: Callable[[], List[Path]]) -> None:
    """Re-execs once no chat turn runs here: one may have started since the request."""
    global _self_restart_scheduled
    try:
        if _running_turns(os.getpid(), homes(), _own_start_time()):
            schedule(SELF_RESTART_RECHECK_S, lambda: _restart_when_idle(schedule, homes))
            return
        _reexec()
    except Exception:
        logger.exception("Talaria plugin: the dashboard restart failed; it can be asked again")
        with _self_restart_lock:
            _self_restart_scheduled = False


def self_restart(schedule: Callable[[float, Callable[[], None]], Any],
                 homes: Optional[Callable[[], List[Path]]] = None) -> Dict[str, Any]:
    global _self_restart_scheduled
    refusal = _self_restart_refusal()
    if refusal is not None:
        raise TokenError(*refusal)
    running = _running_turns(os.getpid(), (homes or _hermes_homes)(), _own_start_time())
    if running:
        raise TokenError(409, f"{running} chat turn{'s' if running != 1 else ''} still running on this dashboard")
    with _self_restart_lock:
        if not _self_restart_scheduled:
            _self_restart_scheduled = True
            try:
                schedule(SELF_RESTART_DELAY_S, lambda: _restart_when_idle(schedule, homes or _hermes_homes))
            except BaseException:
                _self_restart_scheduled = False
                raise
    return {"restarting": True, "boot_id": BOOT_ID, "loaded_version": LOADED_VERSION,
            "version": _plugin_version()}


@router.post("/self/restart")
async def post_self_restart(response: Response) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    loop = asyncio.get_running_loop()
    try:
        # The answer goes out first; the exec follows a moment later.
        return {"ok": True, **self_restart(loop.call_later)}
    except TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException:
        raise
    except Exception as exc:
        raise _unexpected("restart", exc, feature="Restart")
