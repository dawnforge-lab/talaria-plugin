import importlib.util
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

# The location tests' phone, places and helpers (and their module loading).
_spec = importlib.util.spec_from_file_location("talaria_maps_location_helpers", ROOT / "tests" / "test_location.py")
helpers = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = helpers
_spec.loader.exec_module(helpers)

location, api, Phone, NOW, BASE = helpers.location, helpers.api, helpers.Phone, helpers.NOW, helpers.BASE
sharing, fix, FakeClient = helpers.sharing, helpers.fix, helpers.FakeClient
maps = helpers._load("talaria_push.maps_tool", ROOT / "maps_tool.py")

DEVICE = helpers.DEVICE


def _phone(store, **batch):
    phone = Phone()
    phone.accept(store.register_key(phone.key_request()))
    batch.setdefault("sharing", sharing())
    store.ingest(phone.seal(phone.batch(**batch)))
    return phone


# --- The tool ---------------------------------------------------------------


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(maps, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(maps.location_tool.session_titles, "title", lambda key: "Trip planning")
    original = location.LocationStore
    monkeypatch.setattr(maps, "LocationStore", lambda path: original(path, clock=lambda: NOW))
    # No Google key unless a test gives one (google).
    monkeypatch.setattr(maps.maps_google, "_env_value", lambda name: None)
    return tmp_path


def _shared(home, **batch):
    store = location.LocationStore(home, clock=lambda: NOW)
    return store, _phone(store, **batch)


def _call(args, **kw):
    return json.loads(maps.handle(args, **kw))


def test_the_tool_hides_until_the_host_has_a_google_key(home, monkeypatch):
    assert maps.available() is False
    assert _call({"action": "search", "query": "cafe"})["error"] == "unavailable"
    _shared(home, fix=fix("significant"))
    assert maps.available() is False, "a phone answers no lookups"
    monkeypatch.setattr(maps.maps_google, "_env_value", lambda name: GOOGLE_KEY)
    assert maps.available() is True


@pytest.mark.parametrize("place, ref", [
    ("me", {"me": True}),
    ("Work", {"saved": "p_work"}),
    ("place_id:I63A4F2F1E8C9", {"place_id": "I63A4F2F1E8C9"}),
    ("51.5, -0.12", {"lat": 51.5, "lon": -0.12}),
    ("Heathrow Terminal 5", {"text": "Heathrow Terminal 5"}),
])
def test_places_are_read_the_way_people_write_them(home, place, ref):
    store, _ = _shared(home, fix=fix("significant"))
    request = maps._request("travel_times", {"from": "Home", "to": [place]}, store.snapshot(), NOW)
    assert request["to"] == [ref] and request["from"] == {"saved": "p_home"}


@pytest.mark.parametrize("args, error", [
    ({"action": "directions", "to": "Clinic"}, "private"),
    ({"action": "directions", "to": ["Home", "Work"]}, "invalid"),
    ({"action": "travel_times", "to": ["x"] * 11}, "invalid"),
    ({"action": "directions", "to": "Home", "mode": "teleport"}, "invalid"),
    ({"action": "directions", "to": "Home", "depart_at": "soon"}, "invalid"),
    ({"action": "directions", "to": "Home", "depart_at": "2026-10-13T08:00", "arrive_by": "2026-10-13T09:00"},
     "invalid"),
    ({"action": "search"}, "invalid"),
    ({"action": "fly"}, "invalid"),
])
def test_the_tool_refuses_what_it_cant_ask(home, google, args, error):
    _shared(home, fix=fix("significant"))
    assert _call(args)["error"] == error
    assert google.calls == []


def test_a_subagent_cant_look_things_up(home):
    _shared(home, fix=fix("significant"))
    assert _call({"action": "search", "query": "cafe"}, session_id="child",
                 is_child=lambda session: True)["error"] == "subagent"


def test_a_saved_place_gets_a_labelled_guess(home, google):
    _shared(home, fix=fix("significant"))
    answer = _call({"action": "travel_times", "to": ["Home", "Heathrow"], "mode": "walking"})
    assert answer["error"] == "unavailable" and google.calls == []
    assert answer["rough_estimate"] == [{"to": "Home", "estimate": "about 21 min", "road_distance_guess": "1.6 km"}]
    assert "rough guess" in answer["note"]


def test_impossible_times_are_refused_before_a_lookup(home, google):
    _shared(home, fix=fix("significant"))
    assert _call({"action": "directions", "to": "Home", "depart_at": "2026-10-15T99:99+99:99"})["error"] == "invalid"
    assert _call({"action": "directions", "to": "Home", "arrive_by": "2026-02-31T08:00"})["error"] == "invalid"
    assert google.calls == []


def test_a_usage_file_that_isnt_a_count_starts_the_day_over(tmp_path):
    usage = tmp_path / maps.maps_google.USAGE_FILE
    usage.write_text("[]")
    maps.maps_google._spend(tmp_path)
    assert json.loads(usage.read_text())["count"] == 1
    assert usage.stat().st_mode & 0o777 == 0o600


def test_a_host_fix_without_a_real_time_or_place_isnt_used():
    now = 1_800_000_000
    def view(**fix):
        return {"sharing": {"background": True}, "fix": {"lat": 51.5, "lon": -0.1, "at": now - 60, **fix}}
    assert maps._host_me(view(), now) is not None
    assert maps._host_me(view(at=None), now) is None
    assert maps._host_me(view(at=float("nan")), now) is None
    assert maps._host_me(view(at=now + 3600), now) is None
    assert maps._host_me(view(lat=float("inf")), now) is None
    assert maps._host_me(view(lon=200), now) is None


def test_a_usage_file_that_cant_be_opened_refuses_the_lookup(tmp_path):
    (tmp_path / maps.maps_google.USAGE_FILE).mkdir()
    with pytest.raises(maps.maps_google.HostUnavailable):
        maps.maps_google._spend(tmp_path)


def test_the_phones_result_cant_replace_the_envelope():
    answer = maps._shaped({"ok": True, "result": {"ok": False, "action": "x", "places": []}}, {"action": "search"})
    assert answer["ok"] is True and answer["action"] == "search"


# --- Navigate ----------------------------------------------------------------


class Enqueued:
    def __init__(self):
        self.events = []

    def enqueue(self, event):
        self.events.append(event)


def _present(home, features=("maps-navigate",)):
    present = maps.PresentStore(home)
    present.exchange({"installation_id": "phone-installation-1", "device": "iPhone", "app_version": "2.0",
                      "features": list(features)})
    return present


def test_navigate_sends_a_go_prompt_carrying_where_to(home, monkeypatch):
    store, _ = _shared(home, fix=fix("significant"))
    present = _present(home)
    queue = Enqueued()
    monkeypatch.setattr(maps, "client", queue)
    answer = _call({"action": "navigate", "to": "Heathrow Terminal 5", "mode": "transit"}, profile="work",
                   session_id="session-1")
    assert answer["ok"] is True and answer["status"] == "sent" and answer["to"] == "Heathrow Terminal 5"
    assert answer["link"] == "https://www.google.com/maps/dir/?api=1&destination=Heathrow+Terminal+5&travelmode=transit"
    waiting = present.exchange({"installation_id": "phone-installation-1", "features": ["maps-navigate"]})["prompts"]
    assert waiting[0]["id"] == answer["prompt_id"] and waiting[0]["action_label"] == "Go"
    assert waiting[0]["maps"] == {"to": "Heathrow Terminal 5", "mode": "transit"}
    assert waiting[0]["session_id"] == "session-1" and "go" in waiting[0]["spoken_yes"]
    event = queue.events[0]
    assert event["type"] == "phone.changes" and event["event_id"] == f"present:{answer['prompt_id']}"
    assert event["mode"] == "alert" and event["profile"] == "work"
    assert store.reads()[0]["action"] == "maps navigate"


def test_navigate_from_another_profile_is_logged_under_it(home, monkeypatch):
    work = home / "profiles" / "work"
    for path in (home, work):
        path.mkdir(parents=True, exist_ok=True)
        (path / "talaria-push.json").write_text(json.dumps({"credential": "c", "installation_id": "installation-1"}))
    store, _ = _shared(work, sharing=sharing(other_profiles=True), fix=fix("significant"))
    _present(home)
    monkeypatch.setattr(maps, "client", Enqueued())
    answer = _call({"action": "navigate", "to": "Heathrow Terminal 5"}, profile="default", session_id="session-1")
    assert answer["ok"] is True
    assert store.reads()[0]["session_title"] == "default: Trip planning"


def test_navigate_to_a_saved_place_names_it_and_gives_no_link(home, monkeypatch):
    _shared(home, fix=fix("significant"))
    present = _present(home)
    monkeypatch.setattr(maps, "client", Enqueued())
    answer = _call({"action": "navigate", "to": "work"})
    assert answer["ok"] is True and "link" not in answer
    prompt = present.exchange({"installation_id": "phone-installation-1", "features": ["maps-navigate"]})["prompts"][0]
    assert prompt["maps"] == {"to": "Work", "mode": "driving"} and prompt["title"] == "Directions to Work?"


@pytest.mark.parametrize("args, error", [
    ({"action": "navigate"}, "invalid"),
    ({"action": "navigate", "to": "me"}, "invalid"),
    ({"action": "navigate", "to": ["A", "B"]}, "invalid"),
    ({"action": "navigate", "to": "A", "mode": "rocket"}, "invalid"),
])
def test_navigate_refuses_what_it_cant_open(home, monkeypatch, args, error):
    _present(home)
    monkeypatch.setattr(maps, "client", Enqueued())
    assert _call(args)["error"] == error


def test_navigate_without_a_talaria_that_opens_maps_gives_the_link(home, monkeypatch):
    _present(home, features=())
    queue = Enqueued()
    monkeypatch.setattr(maps, "client", queue)
    answer = _call({"action": "navigate", "to": "51.5, -0.12", "mode": "cycling"})
    assert answer["ok"] is False and answer["error"] == "not_set_up"
    assert answer["link"] == "https://www.google.com/maps/dir/?api=1&destination=51.5%2C-0.12&travelmode=bicycling"
    assert not queue.events


def test_a_subagent_cant_navigate(home):
    _present(home)
    assert _call({"action": "navigate", "to": "Paris"}, is_child=lambda _: True)["error"] == "subagent"


def test_the_phone_reports_navigate_opened_by_itself(home):
    present = _present(home)
    prompt = present.confirm("Directions to Paris?", "Opens Apple Maps.", "Go", maps={"to": "Paris", "mode": "walking"})
    present.exchange({"installation_id": "phone-installation-1", "features": ["maps-navigate"],
                      "results": [{"id": prompt["id"], "outcome": "confirmed", "via": "auto"}]})
    assert present.status(prompt["id"])[0]["status"] == "confirmed"
    assert present.status(prompt["id"])[0]["via"] == "auto"
    other = present.confirm("Directions to Rome?", "Opens Apple Maps.", "Go", maps={"to": "Rome"})
    present.exchange({"installation_id": "phone-installation-1", "features": ["maps-navigate"],
                      "results": [{"id": other["id"], "outcome": "declined", "via": "auto"}]})
    assert present.status(other["id"])[0]["status"] == "waiting", "auto is only ever a yes"
    plain = present.confirm("Send to Mei?", "Hi", "Send")
    present.exchange({"installation_id": "phone-installation-1",
                      "results": [{"id": plain["id"], "outcome": "confirmed", "via": "auto"}]})
    assert present.status(plain["id"])[0]["status"] == "waiting", "auto answers only a Go prompt"


def test_the_preview_shows_the_place_without_markdown_of_its_own(home, monkeypatch):
    present = _present(home)
    monkeypatch.setattr(maps, "client", Enqueued())
    _call({"action": "navigate", "to": "**Paris** [x](y)"})
    prompt = present.exchange({"installation_id": "phone-installation-1", "features": ["maps-navigate"]})["prompts"][0]
    assert prompt["preview"] == "Opens Maps with driving directions to **Paris x(y)** from where you are."
    assert prompt["maps"]["to"] == "**Paris** [x](y)"


def test_a_malformed_maps_target_is_refused_and_dropped_from_disk(home):
    present = _present(home)
    with pytest.raises(maps.PresentError):
        present.confirm("Go?", "x", "Go", maps={"to": "Paris", "mode": "teleport"})
    prompt = present.confirm("Go?", "x", "Go", maps={"to": "Paris"})
    state = json.loads(present.path.read_text())
    state["prompts"][0]["maps"] = {"to": 7}
    present.path.write_text(json.dumps(state))
    waiting = present.exchange({"installation_id": "phone-installation-1"})["prompts"]
    assert waiting[0]["id"] == prompt["id"] and "maps" not in waiting[0]
    assert present.has_feature("maps-navigate") is False, "the exchange without features replaced the old one"


# --- Google's engine ---------------------------------------------------------------


class HostGoogle:
    """Google Maps Platform as the host sees it: answers by path."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        path = url.split("?", 1)[0].split(".com/", 1)[1]
        answer = self.answers[path]
        if isinstance(answer, Exception):
            raise answer
        return answer


GOOGLE_KEY = "AIzaTest_key-0123456789abcdef"


@pytest.fixture()
def google(home, monkeypatch):
    fake = HostGoogle({})
    engine_module = maps.maps_google
    monkeypatch.setattr(engine_module, "_env_value", lambda name: GOOGLE_KEY if name == "GOOGLE_MAPS_API_KEY" else None)
    original = engine_module.Engine.__init__

    def engine(self, home_path, fetch=None, zone=None, clock=None):
        original(self, home_path, fetch=fake, zone=zone or engine_module.timezone.utc, clock=lambda: NOW)

    monkeypatch.setattr(engine_module.Engine, "__init__", engine)
    return fake


def _phone_without_maps(home, **batch):
    """A phone that shares its location but answers no lookups (the Android app)."""
    store = location.LocationStore(home, clock=lambda: NOW)
    phone = Phone()
    phone.accept(store.register_key(phone.key_request()))
    batch.setdefault("sharing", sharing())
    batch.setdefault("fix", fix("significant"))
    store.ingest(phone.seal(phone.batch(**batch)))
    return store


GOOGLE_BOOTS = {"id": "ChIJBoots", "displayName": {"text": "Boots", "languageCode": "en"},
                "formattedAddress": "123 Oxford St, London W1D 2HG, UK",
                "location": {"latitude": 51.5153, "longitude": -0.1418},
                "primaryTypeDisplayName": {"text": "Pharmacy", "languageCode": "en"}}


def test_with_a_google_key_the_host_looks_things_up_without_the_relay(home, google):
    _phone_without_maps(home)
    assert maps.available()
    google.answers["v1/places:searchText"] = {"places": [GOOGLE_BOOTS]}
    answer = _call({"action": "search", "query": "pharmacy", "near": "51.52,-0.14", "limit": 3})
    assert answer["ok"] and answer["source"] == "Google Maps, from this Hermes host"
    place = answer["places"][0]
    assert place == {"name": "Boots", "address": "123 Oxford St, London W1D 2HG, UK", "category": "Pharmacy",
                     "lat": 51.5153, "lon": -0.1418, "distance_m": 537, "place_id": "ChIJBoots"}
    call = google.calls[0]
    assert call["method"] == "POST" and call["headers"]["X-Goog-Api-Key"] == GOOGLE_KEY
    assert call["headers"]["X-Goog-FieldMask"] == ("places.id,places.displayName,places.formattedAddress,"
                                                   "places.location,places.primaryTypeDisplayName")
    assert call["body"]["textQuery"] == "pharmacy" and call["body"]["pageSize"] == 3
    assert call["body"]["locationBias"]["circle"]["center"] == {"latitude": 51.52, "longitude": -0.14}
    assert GOOGLE_KEY not in call["url"]


def test_google_routes_transit_here_with_its_rides(home, google):
    _phone_without_maps(home)
    google.answers["v1/places:searchText"] = {"places": [GOOGLE_BOOTS]}
    google.answers["directions/v2:computeRoutes"] = {"routes": [{
        "description": "", "duration": "1500s", "staticDuration": "1500s", "distanceMeters": 7000,
        "legs": [{"steps": [
            {"distanceMeters": 300, "navigationInstruction": {"instructions": "Walk to Old Street"}},
            {"distanceMeters": 6500, "transitDetails": {
                "headsign": "Morden", "stopCount": 4,
                "stopDetails": {"departureStop": {"name": "Old Street"}, "arrivalStop": {"name": "Bank"}},
                "transitLine": {"nameShort": "Northern", "vehicle": {"name": {"text": "Subway"}}}}},
        ]}]}]}
    answer = _call({"action": "directions", "from": "51.5,-0.12", "to": "Boots Oxford St", "mode": "transit",
                    "arrive_by": "2026-10-13T08:00"})
    assert answer["ok"] and answer["source"] == "Google Maps, from this Hermes host"
    route = answer["routes"][0]
    assert route["duration"] == "about 25 min" and route["name"] is None
    assert route["steps"] == [{"text": "Walk to Old Street", "distance_m": 300},
                              {"text": "Subway Northern towards Morden from Old Street to Bank (4 stops)",
                               "distance_m": 6500}]
    assert answer["leave_by"] == "2026-10-13T07:35+01:00"
    body = google.calls[-1]["body"]
    assert body["travelMode"] == "TRANSIT" and body["arrivalTime"] == "2026-10-13T07:00:00Z"
    assert "routingPreference" not in body
    assert google.calls[-1]["headers"]["X-Goog-FieldMask"].endswith(",routes.legs.steps.transitDetails")


def test_google_drives_with_traffic_and_says_the_delay(home, google):
    _phone_without_maps(home)
    google.answers["directions/v2:computeRoutes"] = {"routes": [
        {"description": "A10", "duration": "1500s", "staticDuration": "1200s", "distanceMeters": 9000,
         "legs": [{"steps": [{"distanceMeters": 200, "navigationInstruction": {"instructions": "Turn left"}}]}]},
        {"description": "A1", "duration": "1700s", "staticDuration": "1650s", "distanceMeters": 9500},
    ]}
    answer = _call({"action": "directions", "from": "51.5,-0.12", "to": "51.52,-0.08", "depart_at": "2026-10-13T06:00"})
    first, second = answer["routes"]
    assert first["name"] == "A10" and first["traffic_delay"] == "about 5 min"
    assert first["steps"] == [{"text": "Turn left", "distance_m": 200}] and second["steps"] == []
    body = google.calls[0]["body"]
    assert body["routingPreference"] == "TRAFFIC_AWARE" and body["computeAlternativeRoutes"] is True
    assert "departureTime" not in body, "a departure in the past leaves now"
    assert body["origin"] == {"location": {"latLng": {"latitude": 51.5, "longitude": -0.12}}}


def test_google_travel_times_follow_each_destination_index(home, google):
    _phone_without_maps(home, sharing=sharing(background_precision="precise"), fix=fix("significant"))
    google.answers["distanceMatrix/v2:computeRouteMatrix"] = [
        {"originIndex": 0, "destinationIndex": 1, "condition": "ROUTE_NOT_FOUND"},
        {"originIndex": 0, "condition": "ROUTE_EXISTS", "duration": "600s", "staticDuration": "540s",
         "distanceMeters": 3000},
    ]
    answer = _call({"action": "travel_times", "to": ["51.52,-0.08", "48.85,2.35"], "mode": "cycling"})
    first, second = answer["results"]
    assert first["duration"] == "about 10 min" and first["distance_m"] == 3000
    assert second["error"] == "no_route"
    body = google.calls[0]["body"]
    assert body["travelMode"] == "BICYCLE" and len(body["destinations"]) == 2
    assert body["origins"][0]["waypoint"]["location"]["latLng"] == {"latitude": 51.532141, "longitude": -0.075512}


def test_google_reverse_geocodes_with_its_key_in_the_query(home, google):
    _phone_without_maps(home)
    google.answers["maps/api/geocode/json"] = {"status": "OK", "results": [
        {"formatted_address": "10 Downing St, London SW1A 2AA, UK", "place_id": "ChIJDowning",
         "geometry": {"location": {"lat": 51.5034, "lng": -0.1276}}}]}
    answer = _call({"action": "reverse_geocode", "near": "51.5034,-0.1276"})
    assert answer["places"][0] == {"name": "10 Downing St", "address": "10 Downing St, London SW1A 2AA, UK",
                                   "lat": 51.5034, "lon": -0.1276, "place_id": "ChIJDowning"}
    call = google.calls[0]
    assert "latlng=51.503400%2C-0.127600" in call["url"] and f"key={GOOGLE_KEY}" in call["url"]
    assert "X-Goog-Api-Key" not in call["headers"]
    google.answers["maps/api/geocode/json"] = {"status": "REQUEST_DENIED", "error_message": "API not enabled"}
    assert _call({"action": "reverse_geocode", "near": "51.5034,-0.1276"})["error"] == "unavailable"


def test_a_key_google_refuses_reads_plainly(home, google):
    import urllib.error
    _phone_without_maps(home)
    google.answers["v1/places:searchText"] = urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
    answer = _call({"action": "lookup", "query": "Boots"})
    assert answer["ok"] is False and answer["error"] == "unavailable" and "key" in answer["note"]


def test_an_older_apps_keep_maps_on_the_phone_switch_is_ignored(home, google):
    _shared(home, sharing=sharing(maps_host=False), fix=fix("significant"))
    assert maps.available() is True, "lookups only ever run on the host now"


def test_saved_places_still_need_the_phone_with_google(home, google):
    _phone_without_maps(home)
    assert _call({"action": "directions", "to": "Home"})["error"] == "unavailable"
    assert google.calls == []


def test_google_spends_the_same_daily_share(home, google, monkeypatch):
    monkeypatch.setattr(maps.maps_google, "DAILY_CALLS", 1)
    _phone_without_maps(home)
    google.answers["v1/places:searchText"] = {"places": [GOOGLE_BOOTS]}
    assert _call({"action": "lookup", "query": "Boots"})["ok"]
    assert _call({"action": "lookup", "query": "Boots"})["error"] == "daily_limit"
    assert len(google.calls) == 1


def test_navigate_to_a_google_place_goes_as_its_coordinates(home, google, monkeypatch):
    _phone_without_maps(home)
    present = _present(home)
    monkeypatch.setattr(maps, "client", Enqueued())
    google.answers["v1/places/ChIJBoots"] = GOOGLE_BOOTS
    answer = _call({"action": "navigate", "to": "place_id:ChIJBoots", "mode": "cycling"})
    assert answer["ok"] and answer["to"] == "Boots"
    assert answer["link"] == ("https://www.google.com/maps/dir/?api=1&destination=51.5153%2C-0.1418"
                              "&travelmode=bicycling")
    prompt = present.exchange({"installation_id": "phone-installation-1", "features": ["maps-navigate"]})["prompts"][0]
    assert prompt["maps"] == {"to": "51.515300,-0.141800", "mode": "cycling"}
    assert prompt["title"] == "Directions to Boots?"
    assert google.calls[0]["headers"]["X-Goog-FieldMask"] == maps.maps_google.PLACE_FIELDS


@pytest.mark.parametrize("value", [None, "", "short", "AIza key with spaces 0123456789",
                                   "AIza\r\nX-Evil: 1aaaaaaaaaaaaa"])
def test_a_key_that_isnt_one_leaves_google_off(monkeypatch, value):
    monkeypatch.setattr(maps.maps_google, "_env_value", lambda name: value)
    assert maps.maps_google.configured() is False
