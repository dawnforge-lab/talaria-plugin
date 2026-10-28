import contextlib
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
BASE = "/api/plugins/talaria_push"

# The dashboard imports plugin_api.py by file path, not as a package.
_spec = importlib.util.spec_from_file_location("talaria_plugin_api", ROOT / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(api)


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


@pytest.fixture()
def hermes_scope(monkeypatch):
    """Hermes' profile scope: any profile named ghost* doesn't exist."""

    @contextlib.contextmanager
    def scope(profile):
        if profile and profile.startswith("ghost"):
            raise HTTPException(status_code=404, detail="Profile not found")
        yield

    fake = types.ModuleType("hermes_cli.web_server_profiles")
    fake._config_profile_scope = scope
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server_profiles", fake)


def test_a_full_limiter_turns_new_keys_away():
    clock = [0.0]
    limiter = api._MintLimiter(5, 60, clock=lambda: clock[0], max_keys=2)
    limiter.acquire("coder")
    limiter.acquire("work")
    with pytest.raises(api.TokenError) as refused:
        limiter.acquire("third")
    assert refused.value.status == 429
    # Known keys keep their budget, and the dashboard's own profile always fits.
    limiter.acquire("coder")
    limiter.acquire("")
    assert set(limiter._mints) == {"coder", "work", ""}
    # Once a window passes the idle keys go, and new ones fit again.
    clock[0] = 61
    limiter.acquire("third")
    assert set(limiter._mints) == {"third"}


@pytest.mark.parametrize("method, path, limiter", [
    ("put", "/talaria-settings", "_talaria_settings_limiter"),
    ("get", "/cron/notifications", "_cron_limiter"),
    ("get", "/calls", "_calls_limiter"),
    ("post", "/health/key", "_health_key_limiter"),
    ("post", "/phone/key", "_phone_key_limiter"),
    ("post", "/location/key", "_location_key_limiter"),
    ("post", "/e2e", "_e2e_limiter"),
])
def test_an_unknown_profile_gets_no_rate_limit_bucket(client, hermes_scope, monkeypatch, method, path, limiter):
    fresh = api._MintLimiter(1000, 60)
    monkeypatch.setattr(api, limiter, fresh)
    for n in range(5):
        response = getattr(client, method)(f"{BASE}{path}?profile=ghost{n}", **({} if method == "get" else {"json": {}}))
        assert response.status_code == 404, response.text
        assert response.headers["cache-control"] == "no-store"
    assert fresh._mints == {}


def test_a_broken_scope_is_a_named_host_failure(client, monkeypatch):
    def broken(profile):
        raise RuntimeError("scope exploded")

    monkeypatch.setattr(api, "_profile_scope", broken)
    response = client.post(f"{BASE}/health/key?profile=coder", json={})
    assert response.status_code == 500 and response.headers["cache-control"] == "no-store"
    assert response.json()["detail"] == "Talaria profile check failed on the host (RuntimeError)"


def test_manifest_mounts_the_api_under_the_plugin_name():
    manifest = json.loads((ROOT / "dashboard" / "manifest.json").read_text())
    assert manifest["name"] == "talaria_push"
    assert manifest["api"] == "plugin_api.py"
    assert manifest["tab"]["hidden"] is True
    assert (ROOT / "dashboard" / manifest["entry"]).exists()


def test_a_limiter_frees_slots_after_the_window():
    now = [0.0]
    limiter = api._MintLimiter(1, 60.0, clock=lambda: now[0])
    limiter.acquire("default")
    with pytest.raises(api.TokenError) as raised:
        limiter.acquire("default")
    assert raised.value.status == 429
    limiter.acquire("coder")  # each profile has its own budget
    now[0] = 61.0
    limiter.acquire("default")


def test_current_profile_aliases_share_one_budget():
    assert {api._limiter_key(p) for p in (None, "", " ", "current", "Current")} == {""}
    assert api._limiter_key(" coder ") == "coder"


def test_requests_without_a_profile_still_enter_hermes_scope(client, hermes_scope, monkeypatch):
    # Once the dashboard hosts several profiles, an unscoped secret read raises
    # (UnscopedSecretError), so the no-profile path must enter the scope too.
    entered = []

    @contextlib.contextmanager
    def scope(profile):
        entered.append(profile)
        yield

    monkeypatch.setattr(sys.modules["hermes_cli.web_server_profiles"], "_config_profile_scope", scope)
    monkeypatch.setattr(api, "calls_status", lambda: {})
    assert client.get(f"{BASE}/calls").status_code == 200
    assert client.get(f"{BASE}/calls?profile=coder").status_code == 200
    assert entered == [None, "coder"]


def test_a_requested_profile_fails_closed_when_it_cannot_be_scoped(client, monkeypatch):
    # No hermes_cli here, so profile scoping is unavailable: the request must
    # not fall back to the default profile.
    monkeypatch.setattr(api, "calls_status", lambda: {})
    assert client.get(f"{BASE}/calls?profile=coder").status_code == 503


def test_hermes_without_the_scope_helper_fails_closed_for_named_profiles(client, monkeypatch):
    # The module exists but the private helper was renamed: plain ImportError.
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server_profiles", types.ModuleType("hermes_cli.web_server_profiles"))
    monkeypatch.setattr(api, "calls_status", lambda: {})
    assert client.get(f"{BASE}/calls?profile=coder").status_code == 503


def test_an_oversized_body_is_refused(client):
    response = client.put(f"{BASE}/calls", content=b'{"settings":"' + b"x" * 20000 + b'"}',
                          headers={"Content-Type": "application/json"})
    assert response.status_code == 413


def test_an_oversized_chunked_body_is_refused(client):
    def chunks():
        for _ in range(40):
            yield b"x" * 1024

    response = client.put(f"{BASE}/calls", content=chunks(), headers={"Content-Type": "application/json"})
    assert response.status_code == 413
