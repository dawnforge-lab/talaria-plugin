import importlib.util
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name, path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


calls_store = _load("talaria_calls_store_test", ROOT / "calls_store.py")
CallStore = calls_store.CallStore


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def _store(tmp_path, clock=None, **settings):
    store = CallStore(tmp_path, clock=clock or Clock())
    if settings:
        store.update_settings(settings)
    return store


def test_calls_start_off_with_the_agreed_defaults(tmp_path):
    assert _store(tmp_path).settings() == {
        "enabled": False,
        "when_asked": True,
        "decides": False,
        "alerts": False,
        "min_gap_s": 120,
        "per_hour": 6,
        "per_day": 20,
        "rules": "",
        "avoid_places": [],
        "avoid_unknown_place": False,
    }


def test_settings_change_within_bounds_and_persist(tmp_path):
    store = _store(tmp_path)
    assert store.update_settings({"enabled": True, "min_gap_s": 30, "per_hour": 30, "per_day": 60})["per_day"] == 60
    assert CallStore(tmp_path).settings()["enabled"] is True


@pytest.mark.parametrize("changes", [
    {"min_gap_s": 29},
    {"min_gap_s": 3601},
    {"per_hour": 0},
    {"per_hour": 31},
    {"per_day": 61},
    {"per_day": 2.5},
    {"per_day": True},
    {"enabled": "yes"},
    {"surprise": 1},
])
def test_settings_outside_the_bounds_are_refused(tmp_path, changes):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.update_settings(changes)
    assert store.settings()["per_day"] == 20


def test_the_users_call_rules_are_kept_as_plain_lines(tmp_path):
    store = _store(tmp_path)
    saved = store.update_settings({"rules": "  Only if production is down.\r\n\n\tNot before 9 am.\x07  "})
    assert saved["rules"] == "Only if production is down.\nNot before 9 am."
    assert CallStore(tmp_path).settings()["rules"] == "Only if production is down.\nNot before 9 am."
    assert store.update_settings({"rules": "x" * 500})["rules"] == "x" * 500
    assert store.update_settings({"rules": "   "})["rules"] == ""


@pytest.mark.parametrize("rules", ["x" * 501, 7, None, ["Only outages"]])
def test_call_rules_that_arent_short_text_are_refused(tmp_path, rules):
    store = _store(tmp_path, rules="Only outages.")
    with pytest.raises(ValueError, match="rules"):
        store.update_settings({"rules": rules})
    assert store.settings()["rules"] == "Only outages."


def test_call_rules_unreadable_on_disk_read_as_none(tmp_path):
    (tmp_path / "talaria-calls.json").write_text(json.dumps({"version": 1, "settings": {"enabled": True, "rules": 3}}))
    assert CallStore(tmp_path).settings()["rules"] == ""
    (tmp_path / "talaria-calls.json").write_text(json.dumps({"version": 1, "settings": {"rules": "y" * 501}}))
    assert CallStore(tmp_path).settings()["rules"] == ""


def _watch(store, session_id, reason="Check the server", asked=True):
    """Hermes asked to call when this turn of ``session_id`` ends."""
    return store.add_tool_watch(session_id, reason, asked)


def test_a_watch_the_user_asked_for_needs_calls_on_and_call_when_asked(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="calls_off"):
        _watch(store, "rt-1")
    store.update_settings({"enabled": True, "when_asked": False})
    with pytest.raises(ValueError, match="calls_off"):
        _watch(store, "rt-1")


def test_a_watch_fires_exactly_once(tmp_path):
    store = _store(tmp_path, enabled=True)
    added = _watch(store, "st-1")
    assert added["status"] == "watching"
    fired = store.fire("st-1", "done")
    assert fired["status"] == "call"
    assert fired["outcome"] == "done"
    assert fired["watch"]["id"] == added["id"]
    assert fired["watch"]["reason"] == "Check the server"
    assert fired["watch"]["session_ids"] == ["st-1"]
    # A replayed hook finds nothing left.
    assert store.fire("st-1", "done") is None
    assert store.fire("st-1", "failed") is None


def test_an_unwatched_session_is_left_alone(tmp_path):
    store = _store(tmp_path, enabled=True)
    _watch(store, "rt-1")
    assert store.fire("rt-2", "done") is None
    assert store.watch_count() == 1


def test_no_file_means_nothing_to_fire(tmp_path):
    store = _store(tmp_path)
    assert store.fire("rt-1", "done") is None
    assert not (tmp_path / "talaria-calls.json").exists()


def test_calls_turned_off_later_consume_the_watch_without_calling(tmp_path):
    store = _store(tmp_path, enabled=True)
    _watch(store, "rt-1")
    store.update_settings({"enabled": False})
    assert store.fire("rt-1", "done")["status"] == "off"
    assert store.watch_count() == 0


def test_the_gap_between_calls_holds_back_a_second_call(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock=clock, enabled=True, min_gap_s=120)
    _watch(store, "a", "A")
    _watch(store, "b", "B")
    _watch(store, "c", "C")
    assert store.fire("a", "done")["status"] == "call"
    clock.now += 119
    limited = store.fire("b", "done")
    assert limited["status"] == "limited"
    assert limited["reason"] == "gap"
    clock.now += 2
    assert store.fire("c", "done")["status"] == "call"


def test_the_hourly_and_daily_limits_hold(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock=clock, enabled=True, min_gap_s=30, per_hour=2, per_day=3)
    outcomes = []
    for index in range(5):
        _watch(store, f"s{index}", f"Job {index}")
    for index in range(3):
        outcomes.append(store.fire(f"s{index}", "done"))
        clock.now += 31
    assert [o["status"] for o in outcomes] == ["call", "call", "limited"]
    assert outcomes[2]["reason"] == "hour"
    clock.now += 3600
    assert store.fire("s3", "done")["status"] == "call"
    clock.now += 3600
    day = store.fire("s4", "done")
    assert day["status"] == "limited"
    assert day["reason"] == "day"


def test_a_due_alert_still_obeys_the_limits(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock=clock, enabled=True, alerts=True)
    store.add_alert("b", "approval", "Push?")
    _watch(store, "a", "A")
    assert store.fire("a", "done")["status"] == "call"
    clock.now += calls_store.ALERT_DELAY_S + 1
    [due] = store.fire_due()
    assert due["status"] == "limited"
    assert due["reason"] == "gap"


def test_a_stopped_job_still_calls(tmp_path):
    store = _store(tmp_path, enabled=True)
    _watch(store, "rt-1")
    result = store.fire("rt-1", "stopped")
    assert result["status"] == "call"
    assert result["outcome"] == "stopped"


@pytest.mark.parametrize("delay_s", [-1, 601, "60", True, 1.5])
def test_an_alerts_delay_is_whole_seconds_within_bounds(tmp_path, delay_s):
    store = _store(tmp_path, enabled=True, alerts=True)
    with pytest.raises(ValueError, match="delay_s"):
        store.add_alert("st-1", "approval", "Push?", delay_s=delay_s)


def test_watches_expire_after_a_day(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock=clock, enabled=True)
    _watch(store, "rt-1")
    clock.now += 24 * 3600 + 1
    assert store.fire("rt-1", "done") is None
    assert store.watch_count() == 0


def test_a_profile_holds_at_most_twenty_watches(tmp_path):
    store = _store(tmp_path, enabled=True)
    for index in range(20):
        _watch(store, f"s{index}", "Job")
    with pytest.raises(ValueError, match="too_many"):
        _watch(store, "s20", "Job")


def test_an_alert_watch_is_bounded_and_cleaned(tmp_path):
    store = _store(tmp_path, enabled=True, alerts=True)
    store.add_alert(["a", "a", " b ", "c", "d", "e"], "approval", "  Check\nthe server  " + "x" * 300)
    [watch] = store._load()["watches"]
    assert watch["session_ids"] == ["a", "b", "c", "d"]
    assert watch["reason"].startswith("Check the server")
    assert len(watch["reason"]) == 200


@pytest.mark.parametrize("session_id", ["", " ", "bad\nid", 3, None, ["rt-1"], "x" * 513])
def test_a_watch_needs_a_valid_session_id(tmp_path, session_id):
    store = _store(tmp_path, enabled=True)
    with pytest.raises(ValueError):
        _watch(store, session_id)


def test_two_stores_share_one_file(tmp_path):
    clock = Clock()
    dashboard = _store(tmp_path, clock=clock, enabled=True)
    agent = CallStore(tmp_path, clock=clock)
    _watch(dashboard, "rt-1", "Job")
    assert agent.fire("rt-1", "done")["status"] == "call"
    assert dashboard.fire("rt-1", "done") is None


def test_the_file_is_private(tmp_path):
    _store(tmp_path, enabled=True)
    path = tmp_path / "talaria-calls.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text())["v"] == 1


def test_a_corrupt_file_reads_as_defaults(tmp_path):
    (tmp_path / "talaria-calls.json").write_text("{not json")
    store = CallStore(tmp_path)
    assert store.settings()["enabled"] is False
    assert store.fire("rt-1", "done") is None


def test_an_unknown_outcome_is_refused(tmp_path):
    store = _store(tmp_path, enabled=True)
    with pytest.raises(ValueError):
        store.fire("rt-1", "maybe")


def test_a_turn_end_that_changes_nothing_writes_nothing(tmp_path):
    store = _store(tmp_path, enabled=True)
    _watch(store, "rt-1")
    before = store.path.stat()
    assert store.fire("other", "done") is None
    assert store.fire_due() == []
    after = store.path.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)  # saving replaces the file


def test_hermes_asking_again_for_the_same_turn_is_the_same_watch(tmp_path):
    store = _store(tmp_path, enabled=True)
    first = _watch(store, "st-1")["id"]
    assert _watch(store, "st-1") == {"status": "watching", "id": first}
    assert store.watch_count() == 1


def test_a_watch_with_a_malformed_id_is_dropped(tmp_path):
    store = _store(tmp_path, enabled=True)
    _watch(store, "rt-1")
    state = json.loads(store.path.read_text())
    state["watches"][0]["id"] = "bad id!"
    store.path.write_text(json.dumps(state))
    assert store.watch_count() == 0
    assert store.fire("rt-1", "done") is None


def test_call_history_on_disk_is_capped_to_the_newest_calls(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock=clock, enabled=True)
    state = json.loads(store.path.read_text())
    state["history"] = [clock.now - 10 - at for at in range(500)]
    normalized = calls_store._normalized(state, clock.now)
    assert len(normalized["history"]) == calls_store.MAX_HISTORY == 60
    assert normalized["history"][-1] == clock.now - 10
    assert normalized["history"] == sorted(normalized["history"])


def test_hermes_can_ask_for_a_call_only_as_the_settings_allow(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="calls_off"):
        store.add_tool_watch("st-1", "The deploy finished.", asked=True)
    store.update_settings({"enabled": True})
    with pytest.raises(ValueError, match="calls_off"):
        store.add_tool_watch("st-1", "The deploy finished.", asked=False)
    watch_id = store.add_tool_watch("st-1", "  The deploy\nfinished. ", asked=True)["id"]
    [watch] = store._load()["watches"]
    assert (watch["id"], watch["origin"], watch["reason"], watch["asked"]) == (watch_id, "tool", "The deploy finished.", True)
    store.update_settings({"decides": True})
    assert store.add_tool_watch("st-2", "x" * 300, asked=False)["id"] != watch_id
    assert len(store._load()["watches"][1]["reason"]) == 200


def test_a_session_already_watched_keeps_its_watch_and_takes_a_reason(tmp_path):
    store = _store(tmp_path, enabled=True)
    watch_id = _watch(store, "st-1", "")["id"]
    assert _watch(store, "st-1", "The server is back.") == {"status": "watching", "id": watch_id}
    assert _watch(store, "st-1", "Something else.") == {"status": "watching", "id": watch_id}
    [watch] = store._load()["watches"]
    assert (watch["origin"], watch["reason"]) == ("tool", "The server is back.")
    result = store.fire("st-1", "done")
    assert result["status"] == "call" and result["watch"]["reason"] == "The server is back."


def test_the_users_request_wins_over_a_watch_hermes_set_on_its_own(tmp_path):
    store = _store(tmp_path, enabled=True, decides=True)
    watch_id = _watch(store, "st-1", "Heads up.", asked=False)["id"]
    assert _watch(store, "st-1", "You asked.", asked=True) == {"status": "watching", "id": watch_id}
    [watch] = store._load()["watches"]
    assert (watch["asked"], watch["reason"]) == (True, "Heads up.")
    # "Hermes decides" off never refuses what the user asked for.
    store.update_settings({"decides": False})
    assert store.fire("st-1", "done")["status"] == "call"


def test_a_tool_watch_turned_off_before_its_turn_ends_does_not_call(tmp_path):
    store = _store(tmp_path, enabled=True, decides=True)
    store.add_tool_watch("st-1", "Heads up.", asked=False)
    store.update_settings({"decides": False})
    assert store.fire("st-1", "done")["status"] == "off"
    store.add_tool_watch("st-2", "You asked.", asked=True)
    assert store.fire("st-2", "done")["status"] == "call"


def test_an_unanswered_approval_calls_after_a_minute(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock=clock, enabled=True)
    assert store.add_alert("sk-1", "approval", "Run rm -rf build?") is None, "alerts are off"
    store.update_settings({"alerts": True})
    alert_id = store.add_alert("sk-1", "approval", "Run rm -rf build?")
    assert alert_id and store.add_alert("sk-1", "approval", "again") is None, "one at a time"
    assert store.next_due() == clock.now + calls_store.ALERT_DELAY_S
    clock.now += 59
    assert store.fire_due() == []
    clock.now += 2
    [due] = store.fire_due()
    assert (due["status"], due["outcome"], due["session_id"]) == ("call", "approval", "sk-1")
    assert (due["watch"]["id"], due["watch"]["reason"]) == (alert_id, "Run rm -rf build?")
    assert store.watch_count() == 0
    assert store.next_due() is None


def test_an_answered_question_or_a_finished_turn_calls_no_more(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock=clock, enabled=True, alerts=True)
    store.add_alert("st-1", "question", "Which branch?")
    store.add_alert("st-1", "approval", "Push?")
    assert store.cancel_alerts("st-1", "question") == 1
    assert store.cancel_alerts("st-1", "question") == 0
    assert store.fire("st-1", "done") is None, "an alert is not a job watch"
    clock.now += 120
    assert store.fire_due() == [], "the turn ending dropped the approval"
    assert store.watch_count() == 0


def test_an_alert_under_the_gateway_key_follows_the_turns_session_id(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock=clock, enabled=True, alerts=True)
    store.add_alert(["sk-1", "st-1"], "approval", "Push?")
    assert store.add_alert(["sk-1", "st-1"], "approval", "again") is None, "one at a time"
    assert store.add_alert(["st-1"], "approval", "replayed without the key") is None, "the same wait"
    # Answered in chat: the turn ends under its own id and drops the alert.
    assert store.fire("st-1", "done") is None
    clock.now += 120
    assert store.fire_due() == []
    store.add_alert(["sk-1", "st-1"], "approval", "Push?")
    clock.now += 61
    [due] = store.fire_due()
    assert (due["session_id"], due["watch"]["session_ids"]) == ("sk-1", ["sk-1", "st-1"])
    # The user declined: the next turn hears it, under either id.
    store.record_outcome(due["watch"]["id"], due["watch"]["session_ids"], "declined", kind="approval")
    assert [entry["outcome"] for entry in store.take_outcomes("st-1")] == ["declined"]
    # A key that isn't a session key leaves the turn's id to call.
    assert store.add_alert(["bad\x00key", "st-2"], "approval", "Push?")
    assert store._load()["watches"][-1]["session_ids"] == ["st-2"]


def test_an_alert_does_not_write_anything_while_alerts_are_off(tmp_path):
    store = CallStore(tmp_path, clock=Clock())
    assert store.add_alert("st-1", "question", "Which branch?") is None
    assert store.alert_now("st-1", "failed", "") is None
    assert store.cancel_alerts("st-1") == 0
    assert not store.path.exists()


def test_a_failed_turn_alert_counts_against_the_limits(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock=clock, enabled=True, alerts=True)
    first = store.alert_now("st-1", "failed", "")
    assert first["status"] == "call" and first["watch"]["origin"] == "alert"
    assert store.alert_now("st-2", "failed", "")["status"] == "limited"
    assert store.watch_count() == 0


# --- Files an older plugin wrote ---------------------------------------------


def _older_watch(watch_id, session_ids, **fields):
    """A watch as an older plugin stored it, before the store dropped the
    watches the phone registered in a Live Voice call."""
    return {"id": watch_id, "session_ids": session_ids, "title": "Check the server", "created_at": 1_000_000.0,
            "expires_at": 1_000_000.0 + 3600, "hold_until": 0.0, "pending": None, **fields}


def test_watches_the_phone_registered_read_as_calls_the_user_asked_for(tmp_path):
    clock = Clock()
    (tmp_path / "talaria-calls.json").write_text(json.dumps({"v": 1, "settings": {"enabled": True}, "watches": [
        _older_watch("a" * 24, ["rt-1", "st-1"], origin="talaria", reason="", asked=False),
        # Before origins were kept.
        _older_watch("b" * 24, ["rt-2"]),
        # An origin this plugin doesn't know keeps its own asked flag.
        _older_watch("c" * 24, ["rt-3"], origin="someday", reason="Heads up.", asked=False),
        # An alert with nothing to call about.
        _older_watch("d" * 24, ["rt-4"], origin="alert"),
    ]}))
    store = CallStore(tmp_path, clock=clock)
    watches = store._load()["watches"]
    assert [(watch["origin"], watch["asked"]) for watch in watches] == [("tool", True), ("tool", True), ("tool", False)]
    # They ring with "call when I ask" on, "Hermes decides" off, and still
    # name the job.
    fired = store.fire("st-1", "done")
    assert (fired["status"], fired["watch"]["title"]) == ("call", "Check the server")
    assert store.fire("rt-3", "done")["status"] == "off"


def test_a_watch_whose_job_ended_in_a_call_still_calls_when_due(tmp_path):
    clock = Clock()
    (tmp_path / "talaria-calls.json").write_text(json.dumps({"v": 1, "settings": {"enabled": True}, "watches": [
        _older_watch("a" * 24, ["rt-1"], origin="talaria", hold_until=clock.now + 30,
                     pending={"outcome": "done", "session_id": "rt-1"}),
    ]}))
    store = CallStore(tmp_path, clock=clock)
    assert store.next_due() == clock.now + 30
    clock.now += 31
    [due] = store.fire_due()
    assert (due["status"], due["outcome"], due["session_id"]) == ("call", "done", "rt-1")


def test_recent_turn_ends_an_older_plugin_kept_load_and_are_dropped_on_the_next_write(tmp_path):
    clock = Clock()
    path = tmp_path / "talaria-calls.json"
    path.write_text(json.dumps({"v": 1, "settings": {"enabled": True}, "watches": [],
                                "ends": [{"session_id": "rt-1", "outcome": "done", "at": clock.now - 60}]}))
    store = CallStore(tmp_path, clock=clock)
    assert "ends" not in store._load()
    _watch(store, "rt-1")
    assert "ends" not in json.loads(path.read_text())
    assert store.fire("rt-1", "done")["status"] == "call"


# --- Outcomes: calls the user declined or missed ---------------------------

CALL_ID = "a1b2c3d4e5f6a1b2c3d4e5f6"


def test_a_declined_call_waits_for_the_next_turn_of_its_chat_and_is_heard_once(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock)
    assert store.record_outcome(CALL_ID, ["rt-1", "st-1"], "declined", kind="done", title="Deploy",
                                reason="The deploy finished.", age_s=30) == {"status": "recorded"}
    assert store.take_outcomes("other") == []
    clock.now += 60
    assert CallStore(tmp_path, clock=clock).take_outcomes("st-1") == [{
        "id": CALL_ID, "session_ids": ["rt-1", "st-1"], "outcome": "declined", "kind": "done",
        "title": "Deploy", "reason": "The deploy finished.", "at": 1_000_000.0 - 30,
    }]
    assert store.take_outcomes("rt-1") == [], "the chat's other id doesn't hear it again"


def test_a_retried_report_keeps_the_first(tmp_path):
    store = _store(tmp_path)
    store.record_outcome(CALL_ID, ["rt-1"], "missed", age_s=10)
    store.record_outcome(CALL_ID, ["rt-1"], "declined", age_s=0)
    assert [entry["outcome"] for entry in store.take_outcomes("rt-1")] == ["missed"]


def test_a_retry_that_arrives_after_the_note_was_heard_is_not_heard_again(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock)
    store.record_outcome(CALL_ID, ["rt-1"], "missed")
    assert len(store.take_outcomes("rt-1")) == 1
    clock.now += 60
    assert store.record_outcome(CALL_ID, ["rt-1"], "missed") == {"status": "recorded"}
    assert store.take_outcomes("rt-1") == []
    # Long after an outcome could still arrive, the id is forgotten.
    clock.now += calls_store.OUTCOME_TTL_S
    assert CallStore(tmp_path, clock=clock)._load()["told"] == []


def test_outcomes_are_taken_oldest_first_and_capped(tmp_path):
    store = _store(tmp_path)
    for index in range(calls_store.MAX_OUTCOMES + 2):
        store.record_outcome(f"call{index:04d}", ["rt-1"], "missed", age_s=calls_store.MAX_OUTCOMES + 2 - index)
    store.record_outcome("call-late", ["rt-1"], "declined", age_s=500)
    taken = store.take_outcomes("rt-1")
    assert len(taken) == calls_store.MAX_OUTCOMES
    assert taken[0]["id"] == "call-late", "reported last, but placed earliest"
    assert "call0000" not in {entry["id"] for entry in taken}


def test_an_outcome_older_than_a_day_says_nothing(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock)
    store.record_outcome(CALL_ID, ["rt-1"], "missed", age_s=60)
    clock.now += calls_store.OUTCOME_TTL_S
    assert store.take_outcomes("rt-1") == []


def test_an_unknown_kind_reads_as_none(tmp_path):
    store = _store(tmp_path)
    store.record_outcome(CALL_ID, ["rt-1"], "missed", kind="celebration")
    assert store.take_outcomes("rt-1")[0]["kind"] is None


@pytest.mark.parametrize("args, kwargs", [
    (("short", ["rt-1"], "missed"), {}),
    ((None, ["rt-1"], "missed"), {}),
    ((CALL_ID, [], "missed"), {}),
    ((CALL_ID, "rt-1", "missed"), {}),
    ((CALL_ID, ["rt-1"], "answered"), {}),
    ((CALL_ID, ["rt-1"], "missed"), {"age_s": -1}),
    ((CALL_ID, ["rt-1"], "missed"), {"age_s": 24 * 3600}),
    ((CALL_ID + "\n", ["rt-1"], "missed"), {}),
    ((CALL_ID, ["rt-1"], "missed"), {"age_s": "30"}),
])
def test_a_malformed_outcome_is_refused(tmp_path, args, kwargs):
    with pytest.raises(ValueError):
        _store(tmp_path).record_outcome(*args, **kwargs)


def test_a_turn_with_no_outcome_writes_nothing(tmp_path):
    store = _store(tmp_path)
    assert store.take_outcomes("rt-1") == []
    assert not store.path.exists()
    store.record_outcome(CALL_ID, ["rt-1"], "missed")
    before = store.path.stat().st_mtime_ns
    assert store.take_outcomes("rt-2") == []
    assert store.path.stat().st_mtime_ns == before


def test_malformed_outcomes_on_disk_are_dropped(tmp_path):
    store = _store(tmp_path)
    store.record_outcome(CALL_ID, ["rt-1"], "missed")
    state = json.loads(store.path.read_text())
    state["outcomes"] += [{"id": "bad id!", "session_ids": ["rt-1"], "outcome": "missed", "at": 1_000_000.0},
                          {"id": "b" * 24, "session_ids": ["rt-1"], "outcome": "missed", "at": True},
                          {"id": "c" * 24, "session_ids": "rt-1", "outcome": "missed", "at": 1_000_000.0},
                          "not an entry"]
    store.path.write_text(json.dumps(state))
    assert [entry["id"] for entry in store.take_outcomes("rt-1")] == [CALL_ID]


def test_an_unreadable_store_is_not_overwritten(tmp_path, monkeypatch):
    store = _store(tmp_path, enabled=True, rules="Only outages.")
    saved = store.path.read_text(encoding="utf-8")
    real = type(store.path).read_text

    def unreadable(path, *args, **kwargs):
        if path == store.path:
            raise PermissionError("busy")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(type(store.path), "read_text", unreadable)
    with pytest.raises(OSError):
        store.update_settings({"when_asked": False})
    monkeypatch.undo()
    assert store.path.read_text(encoding="utf-8") == saved
