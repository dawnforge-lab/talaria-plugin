"""Pushes from group room turns.

A hosted room runs each member's turn in a hidden session of its own (source
``bot_room``, titled ``Group: <room id>``; room_memory.py). The room is where
the user follows the conversation, so a member's reply or failed turn doesn't
notify: a pass ("(pass)") is no news either. What the turn waits on, an
approval or a question, still does, titled by the room's name and the member,
never by the hidden session's title (Hermes titles the session only after its
first turn, so the push showed the room's prompt instead).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Optional

from . import room_memory

logger = logging.getLogger(__name__)

# The room store sits in the root Hermes home, which every profile's rooms share.
SHARED_STATE = "shared-state.db"
TITLE_PREFIX = "Group: "
# A room the store can't name: one hosted elsewhere, or an unreadable store.
UNNAMED = "Group chat"
READ_TIMEOUT = room_memory.READ_TIMEOUT


def is_room(session_id: Any, home: Any) -> bool:
    """Whether ``session_id`` is a room member's hidden session. Unreadable counts as no."""
    try:
        return room_memory.is_room_session(session_id, home)
    except Exception:  # noqa: BLE001 - a push must never break the turn
        logger.debug("talaria_push: couldn't tell whether %s is a room's session", session_id, exc_info=True)
        return False


def label(session_id: Any, home: Any) -> str:
    """``<room name> · <member>`` for a room session's push, else ""."""
    if home is None or not is_room(session_id, home):
        return ""
    home = Path(home)
    member = _profile_of(home)
    return f"{_room_name(str(session_id), home, member) or UNNAMED} · {member}"


def _room_name(session_id: str, home: Path, member: str) -> Optional[str]:
    try:
        room_id = _titled_room(home, session_id) or _running_room(home, member)
        return _name(home, room_id) if room_id else None
    except Exception:  # noqa: BLE001 - the push goes with the generic name
        logger.debug("talaria_push: couldn't name the room of session %s", session_id, exc_info=True)
        return None


def _titled_room(home: Path, session_id: str) -> Optional[str]:
    """The room in the session's title, once Hermes has titled it."""
    row = _one(home / room_memory.STATE_DB, "SELECT title FROM sessions WHERE id = ?", (session_id,))
    title = str(row[0] or "") if row else ""
    if not title.startswith(TITLE_PREFIX):
        return None
    return title[len(TITLE_PREFIX):].strip() or None


def _running_room(home: Path, member: str) -> Optional[str]:
    """Before then, the room whose turn for this member is running (the newest, if several)."""
    path = _root(home) / SHARED_STATE
    if not path.exists():
        return None
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=READ_TIMEOUT)
    try:
        rows = conn.execute(
            "SELECT room_id, payload_json FROM hosted_room_driver_tasks WHERE status = 'running' "
            "ORDER BY started_at DESC"
        ).fetchall()
    finally:
        conn.close()
    for room_id, payload in rows:
        try:
            if json.loads(payload or "{}").get("target_profile") == member:
                return str(room_id)
        except ValueError:
            continue
    return None


def _name(home: Path, room_id: str) -> Optional[str]:
    row = _one(_root(home) / SHARED_STATE, "SELECT name FROM hosted_rooms WHERE room_id = ?", (room_id,))
    name = str(row[0] or "").strip() if row else ""
    return name or None


def _one(path: Path, query: str, args: tuple[Any, ...]) -> Optional[tuple[Any, ...]]:
    if not path.exists():
        return None
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=READ_TIMEOUT)
    try:
        return conn.execute(query, args).fetchone()
    finally:
        conn.close()


def _profile_of(home: Path) -> str:
    """The profile a Hermes home belongs to: profiles/<name>, else the default."""
    return home.name if home.parent.name == "profiles" and home.name else "default"


def _root(home: Path) -> Path:
    """The root Hermes home: a profile's home is <root>/profiles/<name>."""
    return home.parent.parent if home.parent.name == "profiles" else home
