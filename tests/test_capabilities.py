import importlib.util
import json
import re
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
BASE = "/api/plugins/talaria_push"


def _load_plugin_api():
    spec = importlib.util.spec_from_file_location("talaria_plugin_api_capabilities", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _load_plugin_api()


def _client():
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


def test_capabilities_report_the_manifest_version_and_route_features():
    response = _client().get(f"{BASE}/capabilities")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    manifest = re.search(r"^version:\s*(\S+)", (ROOT / "plugin.yaml").read_text(), re.M).group(1)
    assert body == {"ok": True, "version": manifest, "loaded_version": manifest, "restart_needed": False,
                    "boot_id": api.BOOT_ID, "capabilities": list(api.ROUTE_CAPABILITIES)}
    assert "session-takeover" in body["capabilities"]
    # The dashboard manifest must carry the same version the route reports.
    assert json.loads((ROOT / "dashboard" / "manifest.json").read_text())["version"] == manifest


def test_every_capability_names_a_served_route_family():
    # A capability is a promise Talaria acts on: each one must match a route.
    paths = {route.path for route in api.router.routes}
    prefixes = {
        "session-takeover": "/sessions/takeover", "e2e-notifications": "/e2e",
        "desktop-views": "/sessions/desktop-views",
        "hermes-calls": "/calls",
        "hermes-call-outcomes": "/calls/outcomes",
        "talaria-settings": "/talaria-settings",
        "present-v1": "/present/",
        "health-sync": "/health/",
        "location-sync": "/location/",
        # A fresh lookup is the location status route's `locate` answer.
        "location-locate": "/location/status",
        # Sharing with the host's other profiles rides the ingest route.
        "location-profiles": "/location/ingest",
        "phone-sync": "/phone/",
        "phone-alarms": "/phone/",
        "phone-contacts": "/phone/",
        "pairing": "/pair",
        "self-restart": "/self/restart",
        "cron-notifications": "/cron/notifications",
        "reply-sources": "/notifications/reply-sources",
        "chat-on-screen": "/notifications/on-screen",
        "room-files": "/room-files",
        "room-images": "/room-images",
        "artifacts": "/artifacts",
    }
    assert set(prefixes) == set(api.ROUTE_CAPABILITIES)
    for capability, prefix in prefixes.items():
        assert any(path.startswith(prefix) for path in paths), capability


def test_an_unreadable_manifest_reports_no_version(monkeypatch):
    monkeypatch.setattr(api.os.path, "dirname", lambda path: "/nonexistent")
    assert api._plugin_version() is None


def test_a_newer_version_on_disk_reads_as_restart_needed(monkeypatch):
    monkeypatch.setattr(api, "_plugin_version", lambda: "99.0.0")
    body = _client().get(f"{BASE}/capabilities").json()
    assert body["version"] == "99.0.0"
    assert body["loaded_version"] == api.LOADED_VERSION != "99.0.0"
    assert body["restart_needed"] is True
