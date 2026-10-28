"""Group room turns: the room shows a member's reply, so it doesn't push; an
approval or a question it waits on still does, titled by the room and the
member, never by the hidden session's title (room_pushes.py)."""
import importlib.util
import json
import pathlib
import sqlite3
import sys
import tempfile
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

if "hermes_constants" not in sys.modules:
    _hermes_constants = types.ModuleType("hermes_constants")
    _hermes_constants.get_hermes_home = lambda: pathlib.Path(tempfile.gettempdir())
    sys.modules["hermes_constants"] = _hermes_constants
if "tools.clarify_tool" not in sys.modules:
    _tools = sys.modules.setdefault("tools", types.ModuleType("tools"))
    _tools.__path__ = getattr(_tools, "__path__", [])
    _clarify_tool = types.ModuleType("tools.clarify_tool")

    def _strip_recommended(text):
        # Same semantics as test_clarify_loop's stub, whichever module loads first.
        stripped = str(text).strip()
        suffix = "(Recommended)"
        if stripped.casefold().endswith(suffix.casefold()):
            return stripped[: -len(suffix)].strip()
        return stripped

    _clarify_tool.strip_recommended = _strip_recommended
    sys.modules["tools.clarify_tool"] = _clarify_tool
if "talaria_push" not in sys.modules:
    _pkg = types.ModuleType("talaria_push")
    _pkg.__path__ = [str(ROOT)]
    sys.modules["talaria_push"] = _pkg


def _load(name, path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


plugin = _load("talaria_push.__init__", ROOT / "__init__.py")
rooms = sys.modules["talaria_push.room_pushes"]

ROOM = "talaria-c2a8078d-9a73-47ce-9c4f-fe73c9d2e1ef"


def _hermes(root, sessions, rooms_named=(), running=()):
    """A root Hermes home with the echo profile's sessions, and the room store."""
    home = root / "profiles" / "echo"
    home.mkdir(parents=True)
    conn = sqlite3.connect(home / "state.db")
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, parent_session_id TEXT, end_reason TEXT,"
                 " title TEXT)")
    conn.executemany("INSERT INTO sessions VALUES (?, ?, NULL, NULL, ?)", sessions)
    conn.commit()
    conn.close()
    conn = sqlite3.connect(root / "shared-state.db")
    conn.execute("CREATE TABLE hosted_rooms (room_id TEXT PRIMARY KEY, name TEXT NOT NULL)")
    conn.execute("CREATE TABLE hosted_room_driver_tasks (room_id TEXT, payload_json TEXT, status TEXT,"
                 " started_at REAL)")
    conn.executemany("INSERT INTO hosted_rooms VALUES (?, ?)", rooms_named)
    conn.executemany("INSERT INTO hosted_room_driver_tasks VALUES (?, ?, ?, ?)", running)
    conn.commit()
    conn.close()
    return home


def test_a_titled_room_session_is_named_by_its_room_and_member(tmp_path):
    home = _hermes(tmp_path, [("s1", "bot_room", f"Group: {ROOM}")], rooms_named=[(ROOM, "Coffee slogans")])
    assert rooms.label("s1", home) == "Coffee slogans · echo"


def test_before_hermes_titles_it_the_running_turn_names_the_room(tmp_path):
    # On the emulator (2026-10-26): the approval came on the session's first
    # turn, before its title, and the push showed the room's prompt instead.
    home = _hermes(
        tmp_path,
        [("s1", "bot_room", None)],
        rooms_named=[(ROOM, "App test room"), ("other", "Elsewhere")],
        running=[
            ("other", json.dumps({"target_profile": "writer"}), "running", 2.0),
            (ROOM, json.dumps({"target_profile": "echo"}), "running", 1.0),
            ("other", json.dumps({"target_profile": "echo"}), "settled", 3.0),
        ],
    )
    assert rooms.label("s1", home) == "App test room · echo"


def test_a_room_it_cannot_name_reads_as_a_group_chat(tmp_path):
    home = _hermes(tmp_path, [("s1", "bot_room", None)])
    assert rooms.label("s1", home) == "Group chat · echo"


def test_an_ordinary_chat_has_no_room_label(tmp_path):
    home = _hermes(tmp_path, [("s1", "tui", "Deciding what to do today")])
    assert rooms.label("s1", home) == ""
    assert not rooms.is_room("s1", home)


@pytest.fixture
def pushed(tmp_path, monkeypatch):
    home = _hermes(tmp_path, [("room-s", "bot_room", f"Group: {ROOM}"), ("chat-s", "tui", "Plans")],
                   rooms_named=[(ROOM, "Coffee slogans")])
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: home)
    monkeypatch.setattr(plugin.calls, "get_hermes_home", lambda: home)
    monkeypatch.setattr(plugin.calls, "failed_turn", lambda *args, **kwargs: False)
    monkeypatch.setattr(plugin.calls, "alert", lambda *args, **kwargs: None)
    monkeypatch.setattr(plugin, "_desktop_watched", lambda session_id, started: False)
    monkeypatch.setattr(plugin, "_session_title", lambda session_id: "Plans")
    monkeypatch.setattr(plugin, "_titled", lambda title, session_id: f"{title} · Plans")
    monkeypatch.setattr(plugin, "_later", lambda delay, work: None)
    monkeypatch.setattr(plugin, "_pending_ready", {})
    monkeypatch.setattr(plugin, "_parent_turns", {})
    sent = []
    monkeypatch.setattr(plugin, "enqueue", sent.append)
    return sent


def _turn(session_id, **end):
    plugin._post_llm_call(session_id=session_id, turn_id="t", assistant_response="(pass)")
    plugin._on_session_end(session_id=session_id, turn_id="t", **end)


def test_a_room_members_reply_is_not_pushed(pushed):
    _turn("room-s", completed=True, failed=False, interrupted=False)
    assert pushed == []


def test_a_room_members_failed_turn_is_not_pushed(pushed):
    _turn("room-s", completed=False, failed=True, interrupted=False)
    assert pushed == []


def test_an_ordinary_chats_reply_still_is(pushed):
    _turn("chat-s", completed=True, failed=False, interrupted=False)
    assert [event["type"] for event in pushed] == ["response.ready"]


def test_an_approval_in_a_room_is_titled_by_the_room_and_member(pushed):
    plugin._pre_approval_request(session_id="room-s", session_key="room-s", turn_id="t",
                                 description="delete in root path", command="rm -rf x", pattern_key="rm")
    [event] = pushed
    assert event["type"] == "approval.needed"
    assert event["title"] == "Approval needed · Coffee slogans · echo"
    assert event["session_title"] == "Coffee slogans · echo"
