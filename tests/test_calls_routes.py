import contextlib
import importlib.util
import json
import pathlib
import sys
import tempfile
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = "/api/plugins/talaria_push"

if "hermes_constants" not in sys.modules:
    _hermes_constants = types.ModuleType("hermes_constants")
    _hermes_constants.get_hermes_home = lambda: pathlib.Path(tempfile.gettempdir())
    sys.modules["hermes_constants"] = _hermes_constants

_spec = importlib.util.spec_from_file_location("talaria_plugin_api_calls", ROOT / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(api)


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_calls_home", lambda: tmp_path)
    monkeypatch.setattr(api, "_pairing_state_path", lambda: tmp_path / "talaria-push.json")
    monkeypatch.setattr(api, "_profile_scope", lambda profile: contextlib.nullcontext())
    api._calls_limiter._mints.clear()
    return tmp_path


@pytest.fixture()
def paired(home):
    (home / "talaria-push.json").write_text(json.dumps({"credential": "c", "installation_id": "i", "gateway_id": "g"}))
    return home


def _http():
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


def test_status_reports_the_defaults_and_their_bounds(paired):
    response = _http().get(f"{BASE}/calls")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "ok": True,
        "paired": True,
        "settings": {"enabled": False, "when_asked": True, "decides": False, "alerts": False,
                     "min_gap_s": 120, "per_hour": 6, "per_day": 20, "rules": "",
                     "avoid_places": [], "avoid_unknown_place": False},
        "bounds": {
            "min_gap_s": {"min": 30, "max": 3600},
            "per_hour": {"min": 1, "max": 30},
            "per_day": {"min": 1, "max": 60},
        },
        "rules_max": 500,
        "watches": 0,
    }


def test_status_says_when_the_profile_is_not_paired(home):
    assert _http().get(f"{BASE}/calls").json()["paired"] is False


def test_settings_change_and_persist(paired):
    http = _http()
    response = http.put(f"{BASE}/calls", json={"settings": {"enabled": True, "per_day": 40}})
    assert response.status_code == 200
    assert response.json()["settings"]["per_day"] == 40
    assert http.get(f"{BASE}/calls").json()["settings"]["enabled"] is True


@pytest.mark.parametrize("body", [
    {"settings": {"per_day": 61}},
    {"settings": {"min_gap_s": "120"}},
    {"settings": {"ring_tone": "loud"}},
    {"enabled": True},
    [],
])
def test_bad_settings_are_refused(paired, body):
    response = _http().put(f"{BASE}/calls", json=body)
    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"


def test_the_routes_are_rate_limited(paired):
    http = _http()
    for _ in range(api.CALLS_LIMIT):
        assert http.get(f"{BASE}/calls").status_code == 200
    response = http.get(f"{BASE}/calls")
    assert response.status_code == 429
    assert response.headers["cache-control"] == "no-store"


def test_an_oversized_body_is_refused(paired):
    response = _http().put(f"{BASE}/calls", content=b"{" + b" " * 5000 + b"}",
                           headers={"content-type": "application/json"})
    assert response.status_code == 413


def test_the_users_call_rules_change_and_persist(paired):
    http = _http()
    response = http.put(f"{BASE}/calls", json={"settings": {"rules": "Only if production is down. "}})
    assert response.status_code == 200
    assert response.json()["settings"]["rules"] == "Only if production is down."
    assert http.get(f"{BASE}/calls").json()["settings"]["rules"] == "Only if production is down."
    assert http.put(f"{BASE}/calls", json={"settings": {"rules": "x" * 501}}).status_code == 400


def test_new_settings_for_hermes_decides_and_alerts_change(paired):
    http = _http()
    response = http.put(f"{BASE}/calls", json={"settings": {"enabled": True, "decides": True, "alerts": True}})
    assert response.status_code == 200
    settings = http.get(f"{BASE}/calls").json()["settings"]
    assert (settings["decides"], settings["alerts"]) == (True, True)
    assert http.put(f"{BASE}/calls", json={"settings": {"alerts": "yes"}}).status_code == 400


# --- Outcomes ----------------------------------------------------------------

def test_an_outcome_needs_a_paired_profile(home):
    body = {"call_id": "a1b2c3d4e5f6a1b2c3d4e5f6", "session_ids": ["rt-1"], "outcome": "missed"}
    response = _http().post(f"{BASE}/calls/outcomes", json=body)
    assert response.status_code == 409
    assert not (home / "talaria-calls.json").exists()


def test_a_declined_call_is_recorded_for_its_chat(paired):
    home = paired
    http = _http()
    body = {"call_id": "a1b2c3d4e5f6a1b2c3d4e5f6", "session_ids": ["rt-1", "st-1"], "outcome": "declined",
            "kind": "done", "title": "Deploy", "reason": "The deploy finished.", "age_s": 4}
    response = http.post(f"{BASE}/calls/outcomes", json=body)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"ok": True, "status": "recorded"}
    assert http.post(f"{BASE}/calls/outcomes", json=body).json() == {"ok": True, "status": "recorded"}
    taken = api._calls_store().CallStore(home).take_outcomes("st-1")
    assert [(entry["outcome"], entry["reason"]) for entry in taken] == [("declined", "The deploy finished.")]


@pytest.mark.parametrize("body", [
    {"call_id": "a1b2c3d4e5f6a1b2c3d4e5f6", "session_ids": ["rt-1"], "outcome": "answered"},
    {"call_id": "", "session_ids": ["rt-1"], "outcome": "missed"},
    {"call_id": "a1b2c3d4e5f6a1b2c3d4e5f6", "outcome": "missed"},
    {"call_id": "a1b2c3d4e5f6a1b2c3d4e5f6", "session_ids": ["rt-1"], "outcome": "missed", "age_s": 1.5},
    [],
])
def test_a_malformed_outcome_is_refused(paired, body):
    response = _http().post(f"{BASE}/calls/outcomes", json=body)
    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"
