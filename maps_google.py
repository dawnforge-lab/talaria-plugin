"""talaria_maps' engine: Google Maps Platform, called from this host.

With the host's own Google Maps Platform key (``GOOGLE_MAPS_API_KEY`` in
Hermes' .env), lookups go to Google: Places API (New) for search and places,
the Routes API for directions and travel times (transit routes included),
and the Geocoding API for what's at a spot. The key never leaves this host:
no phone and no relay sees it, and an app built from this code ships none.

Saved places can't be looked up here: their coordinates live on the phone and
the host knows only their names (``NeedsPhone``). "me" is the host's last
fix, at the detail it was shared at. Calls count against a daily share,
which keeps a runaway model from running up the key's bill.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from . import client

logger = logging.getLogger("hermes.plugins.talaria_push")

KEY_ENV = "GOOGLE_MAPS_API_KEY"
NAME = "Google Maps"
PLACES = "https://places.googleapis.com/v1"
ROUTES = "https://routes.googleapis.com"
GEOCODE = "https://maps.googleapis.com/maps/api/geocode/json"
TRAVEL = {"driving": "DRIVE", "walking": "WALK", "cycling": "BICYCLE", "transit": "TRANSIT"}
# Pro fields only: what a place needs to be shown and routed to.
PLACE_FIELDS = "id,displayName,formattedAddress,location,primaryTypeDisplayName"
SEARCH_FIELDS = ",".join(f"places.{field}" for field in PLACE_FIELDS.split(","))
ROUTE_FIELDS = ("routes.description,routes.duration,routes.staticDuration,routes.distanceMeters,"
                "routes.legs.steps.distanceMeters,routes.legs.steps.navigationInstruction.instructions")
TRANSIT_FIELDS = ",routes.legs.steps.transitDetails"
MATRIX_FIELDS = "originIndex,destinationIndex,duration,staticDuration,distanceMeters,condition"
# How far around a point a search leans (Google's most).
BIAS_RADIUS_M = 50_000.0
MAX_PAGE = 20
# A key is a header value: Google's are letters, digits, '-' and '_'.
_KEY = re.compile(r"[A-Za-z0-9_-]{20,200}")
_DURATION = re.compile(r"^(\d+(?:\.\d+)?)s$")
DAILY_CALLS = 200
USAGE_FILE = "talaria-maps-usage.json"
TIMEOUT_S = 10.0
MAX_LOOKUP = 5
MAX_ANSWER_BYTES = 512 * 1024


class NeedsPhone(Exception):
    """This lookup can only be answered by the phone."""


class HostUnavailable(Exception):
    """The host can't answer now (no key, quota, Google refused)."""

    def __init__(self, error: str, note: str) -> None:
        super().__init__(note)
        self.error = error


_usage_lock = threading.Lock()


def _env_value(name: str) -> str | None:
    try:
        from hermes_cli.config import get_env_value
    except ImportError:
        import os
        return os.environ.get(name)
    return get_env_value(name)


def _key() -> str | None:
    try:
        value = str(_env_value(KEY_ENV) or "").strip()
    except Exception:  # noqa: BLE001
        return None
    return value if _KEY.fullmatch(value) else None


def configured() -> bool:
    """Whether this host has a Google Maps Platform key."""
    return _key() is not None


def _spend(home: Path) -> None:
    """One Google call against this host's daily share of what its key may
    spend.

    The count is read and rewritten in place under a file lock, so gateway
    and dashboard processes sharing a home count together. The file is
    owner-only from the first byte and never followed through a symlink.
    """
    path = Path(home) / USAGE_FILE
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        with _usage_lock, os.fdopen(os.open(path, flags, 0o600), "r+b") as handle, client._exclusive(handle):
            _count_one(handle)
    except OSError:
        raise HostUnavailable("unavailable", "This Hermes host can't keep count of its maps lookups.") from None


def _count_one(handle: Any) -> None:
    # A crash mid-write leaves a partial record, read back as a fresh day:
    # at worst one day's count restarts.
    if hasattr(os, "fchmod"):
        os.fchmod(handle.fileno(), 0o600)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    handle.seek(0)
    try:
        usage = json.loads(handle.read(4096) or b"{}")
    except ValueError:
        usage = {}
    if not isinstance(usage, dict):
        usage = {}
    count = usage.get("count", 0) if usage.get("day") == today else 0
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        count = 0
    if count >= DAILY_CALLS:
        raise HostUnavailable("daily_limit", "Today's maps lookups from this host are used up.")
    handle.seek(0)
    handle.truncate()
    handle.write(json.dumps({"day": today, "count": count + 1}).encode())
    handle.flush()
    os.fsync(handle.fileno())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class Engine:
    """talaria_maps' lookups, answered by Google. ``fetch(method, url,
    headers, body)`` is the HTTP call, replaced in tests."""

    name = NAME

    def __init__(self, home: Path, fetch: Callable[[str, str, dict[str, str], Any], Any] | None = None,
                 zone: Any = timezone.utc, clock: Callable[[], float] = time.time) -> None:
        self.home = home
        self.zone = zone
        self.clock = clock
        self.call = fetch or self._call_google

    # --- Google -----------------------------------------------------------------------

    def _call_google(self, method: str, url: str, headers: dict[str, str], body: Any) -> Any:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        # No redirects (the key stays with Google) and a capped body.
        with _OPENER.open(request, timeout=TIMEOUT_S) as response:
            answer = response.read(MAX_ANSWER_BYTES + 1)
        if len(answer) > MAX_ANSWER_BYTES:
            raise HostUnavailable("failed", "Google Maps sent more than this host reads.")
        return json.loads(answer or b"{}")

    def _google(self, method: str, url: str, *, fields: str | None = None, body: Any = None,
                key_in_query: bool = False) -> Any:
        key = _key()
        if key is None:
            raise HostUnavailable("unavailable", "Google Maps isn't set up on this Hermes host.")
        headers = {"Accept": "application/json"}
        if key_in_query:
            # The Geocoding API takes its key in the query; the URL is never logged.
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode({"key": key})
        else:
            headers["X-Goog-Api-Key"] = key
        if fields:
            headers["X-Goog-FieldMask"] = fields
        if body is not None:
            headers["Content-Type"] = "application/json"
        _spend(self.home)
        try:
            return self.call(method, url, headers, body)
        except urllib.error.HTTPError as error:
            if error.code == 429:
                raise HostUnavailable("throttled", "Google Maps is busy; try again shortly.") from None
            if error.code == 404:
                raise HostUnavailable("not_found", "Google Maps found nothing for that.") from None
            if error.code in (401, 403):
                raise HostUnavailable("unavailable", "Google Maps refused this host's key (it may not allow this "
                                                     "API, or billing is off).") from None
            raise HostUnavailable("failed", "Google Maps couldn't answer just now.") from None
        except HostUnavailable:
            raise
        except Exception:  # noqa: BLE001 — network
            raise HostUnavailable("failed", "Google Maps couldn't be reached just now.") from None

    def _search_text(self, query: str, near: tuple[float, float] | None, count: int) -> list[Any]:
        body: dict[str, Any] = {"textQuery": query, "pageSize": max(1, min(count, MAX_PAGE))}
        if near is not None:
            body["locationBias"] = {"circle": {"center": {"latitude": near[0], "longitude": near[1]},
                                               "radius": BIAS_RADIUS_M}}
        answer = self._google("POST", f"{PLACES}/places:searchText", fields=SEARCH_FIELDS, body=body) or {}
        places = answer.get("places") if isinstance(answer, dict) else None
        return places if isinstance(places, list) else []

    # --- Lookups ----------------------------------------------------------------------

    def answer(self, request: dict[str, Any], me: dict[str, Any] | None) -> dict[str, Any]:
        """``me``: {"lat", "lon", "detail", "label"} from the host's last fix,
        or None when the request doesn't use it."""
        action = request["action"]
        if action == "search":
            return self._search(request, me)
        if action == "lookup":
            return self._lookup(request, me)
        if action == "reverse_geocode":
            return self._reverse(request, me)
        if action == "directions":
            return self._directions(request, me)
        return self._travel_times(request, me)

    def _point(self, ref: dict[str, Any], me: dict[str, Any] | None, hint: tuple[float, float] | None
               ) -> tuple[tuple[float, float], dict[str, Any]]:
        if ref.get("me"):
            assert me is not None
            return (me["lat"], me["lon"]), _me_place(me)
        if "saved" in ref:
            raise NeedsPhone()
        if "lat" in ref:
            point = (ref["lat"], ref["lon"])
            return point, {"name": f"{point[0]:.5f}, {point[1]:.5f}", "lat": point[0], "lon": point[1]}
        if "place_id" in ref:
            return self._found(self.place(ref["place_id"]), None)
        found = self._search_text(ref["text"], hint, 1)
        if not found:
            raise HostUnavailable("not_found", f"Google Maps found nothing for \"{ref['text']}\".")
        return self._found(found[0], hint)

    def place(self, place_id: str) -> Any:
        """One place by the id an earlier answer gave."""
        return self._google("GET", f"{PLACES}/places/{urllib.parse.quote(place_id, safe='')}", fields=PLACE_FIELDS)

    def _found(self, place: Any, centre: tuple[float, float] | None) -> tuple[tuple[float, float], dict[str, Any]]:
        shown = _place(place, centre, None)
        if "lat" not in shown:
            raise HostUnavailable("not_found", "Google Maps gave a place without a location.")
        return (shown["lat"], shown["lon"]), shown

    def _search(self, request: dict[str, Any], me: dict[str, Any] | None) -> dict[str, Any]:
        centre, _ = self._point(request["near"], me, _mine(me))
        limit = request.get("limit", 10)
        found = self._search_text(request["query"], centre, limit)
        detail = me["detail"] if me and request["near"].get("me") else "precise"
        places = [_place(item, centre, detail) for item in found[:limit]]
        if not places:
            raise HostUnavailable("not_found", "Google Maps found nothing for that.")
        return {"places": places}

    def _lookup(self, request: dict[str, Any], me: dict[str, Any] | None) -> dict[str, Any]:
        hint = None
        if request.get("near"):
            hint, _ = self._point(request["near"], me, _mine(me))
        elif me:
            hint = _mine(me)
        found = self._search_text(request["query"], hint, MAX_LOOKUP)
        places = [_place(item, None, None) for item in found[:MAX_LOOKUP]]
        if not places:
            raise HostUnavailable("not_found", "Google Maps found nothing for that.")
        return {"places": places}

    def _reverse(self, request: dict[str, Any], me: dict[str, Any] | None) -> dict[str, Any]:
        if request["near"].get("me") and me and me["detail"] != "precise":
            # Around a 1 km cell, the area name is the honest answer.
            return {"places": [_me_place(me)]}
        point, _ = self._point(request["near"], me, None)
        answer = self._google("GET", f"{GEOCODE}?{urllib.parse.urlencode({'latlng': _pair(point)})}",
                              key_in_query=True) or {}
        status = answer.get("status") if isinstance(answer, dict) else None
        if status in ("REQUEST_DENIED", "INVALID_REQUEST"):
            raise HostUnavailable("unavailable", "Google Maps refused this host's key for addresses (the "
                                                 "Geocoding API may be off).")
        if status in ("OVER_QUERY_LIMIT", "OVER_DAILY_LIMIT"):
            raise HostUnavailable("throttled", "Google Maps is limiting this host's lookups; try again later.")
        found = answer.get("results") if status == "OK" else None
        if not isinstance(found, list) or not found:
            raise HostUnavailable("not_found", "Google Maps has no address there.")
        return {"places": [_address(item) for item in found[:MAX_LOOKUP]]}

    def _timing(self, request: dict[str, Any]) -> dict[str, str]:
        """departureTime or arrivalTime: Google takes a past time and an
        arrival time only for transit, so the rest leave now and the answer
        works out when to leave from the duration."""
        transit = request["mode"] == "transit"
        if request.get("arrive_by"):
            return {"arrivalTime": _utc(request["arrive_by"], self.zone)} if transit else {}
        if request.get("depart_at"):
            moment = _local(request["depart_at"], self.zone)
            if transit or moment.timestamp() > self.clock() + 60:
                return {"departureTime": _utc(request["depart_at"], self.zone)}
        return {}

    def _directions(self, request: dict[str, Any], me: dict[str, Any] | None) -> dict[str, Any]:
        origin, origin_place = self._point(request["from"], me, _mine(me))
        destination, destination_place = self._point(request["to"][0], me, origin)
        mode = request["mode"]
        body: dict[str, Any] = {"origin": _waypoint(origin), "destination": _waypoint(destination),
                                "travelMode": TRAVEL[mode],
                                "computeAlternativeRoutes": bool(request.get("alternatives", True)),
                                **self._timing(request)}
        if mode == "driving":
            body["routingPreference"] = "TRAFFIC_AWARE"
        fields = ROUTE_FIELDS + (TRANSIT_FIELDS if mode == "transit" else "")
        answer = self._google("POST", f"{ROUTES}/directions/v2:computeRoutes", fields=fields, body=body) or {}
        hide_steps = bool(request["from"].get("me") and me and me["detail"] != "precise") or not request.get("steps", True)
        routes = []
        for route in (answer.get("routes") or [])[:3 if request.get("alternatives", True) else 1]:
            if not isinstance(route, dict):
                continue
            shown: dict[str, Any] = {"name": route.get("description") or None,
                                     "duration_s": _seconds(route.get("duration")),
                                     "distance_m": route.get("distanceMeters")}
            typical = _seconds(route.get("staticDuration"))
            if mode == "driving" and typical is not None:
                shown["typical_duration_s"] = typical
            if not hide_steps:
                shown["steps"] = [step for step in (_step(item) for leg in route.get("legs") or []
                                                    if isinstance(leg, dict) for item in leg.get("steps") or [])
                                  if step]
            routes.append(shown)
        if not routes:
            raise HostUnavailable("no_route", "Google Maps has no route for that.")
        result = {"from": origin_place, "to": destination_place, "mode": mode, "routes": routes}
        result.update(self._times(request, routes[0].get("duration_s")))
        return result

    def _travel_times(self, request: dict[str, Any], me: dict[str, Any] | None) -> dict[str, Any]:
        origin, origin_place = self._point(request["from"], me, _mine(me))
        targets: list[tuple[tuple[float, float] | None, dict[str, Any]]] = []
        for ref in request["to"]:
            try:
                targets.append(self._point(ref, me, origin))
            except HostUnavailable as error:
                if error.error != "not_found":
                    raise
                targets.append((None, {"name": ref.get("text") or ref.get("place_id") or "?"}))
        wanted = [point for point, _ in targets if point is not None]
        cells: dict[int, dict[str, Any]] = {}
        if wanted:
            mode = request["mode"]
            body: dict[str, Any] = {"origins": [{"waypoint": _waypoint(origin)}],
                                    "destinations": [{"waypoint": _waypoint(point)} for point in wanted],
                                    "travelMode": TRAVEL[mode], **self._timing(request)}
            if mode == "driving":
                body["routingPreference"] = "TRAFFIC_AWARE"
            answer = self._google("POST", f"{ROUTES}/distanceMatrix/v2:computeRouteMatrix", fields=MATRIX_FIELDS,
                                  body=body)
            for cell in answer if isinstance(answer, list) else []:
                index = cell.get("destinationIndex", 0) if isinstance(cell, dict) else None
                if isinstance(index, int) and not isinstance(index, bool):
                    cells[index] = cell
        results, cursor = [], 0
        for point, place in targets:
            if point is None:
                results.append({"to": place, "error": "not_found"})
                continue
            cell = cells.get(cursor) or {}
            cursor += 1
            seconds = _seconds(cell.get("duration"))
            if cell.get("condition") != "ROUTE_EXISTS" or seconds is None:
                results.append({"to": place, "error": "no_route"})
                continue
            item = {"to": place, "duration_s": seconds, "typical_duration_s": _seconds(cell.get("staticDuration")),
                    "distance_m": cell.get("distanceMeters")}
            item.update({key: value for key, value in self._times(request, seconds).items()
                         if key in ("arrive_at", "leave_by")})
            results.append(item)
        return {"from": origin_place, "mode": request["mode"], "results": results}

    def _times(self, request: dict[str, Any], seconds: Any) -> dict[str, str]:
        if not isinstance(seconds, (int, float)):
            return {}
        if request.get("arrive_by"):
            arrive = _local(request["arrive_by"], self.zone)
            return {"arrive_at": _show(arrive), "leave_by": _show(arrive - timedelta(seconds=seconds))}
        depart = _local(request["depart_at"], self.zone) if request.get("depart_at") else \
            datetime.fromtimestamp(self.clock(), self.zone)
        return {"depart_at": _show(depart), "arrive_at": _show(depart + timedelta(seconds=seconds))}


# --- Helpers -------------------------------------------------------------------------


def _pair(point: tuple[float, float] | None) -> str | None:
    return None if point is None else f"{point[0]:.6f},{point[1]:.6f}"


def _mine(me: dict[str, Any] | None) -> tuple[float, float] | None:
    return None if me is None else (me["lat"], me["lon"])


def _me_place(me: dict[str, Any]) -> dict[str, Any]:
    # The user's own position is never echoed back as coordinates or an address.
    return {"name": me.get("label") or "the user's position"}


def _metres_between(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


def _local(value: str, zone: Any) -> datetime:
    parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    return parsed.replace(tzinfo=zone) if parsed.tzinfo is None else parsed.astimezone(zone)


def _utc(value: str, zone: Any) -> str:
    return _local(value, zone).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _show(moment: datetime) -> str:
    return moment.isoformat(timespec="minutes")


def _real(value: Any, low: float, high: float) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and         low <= value <= high


def _waypoint(point: tuple[float, float]) -> dict[str, Any]:
    return {"location": {"latLng": {"latitude": point[0], "longitude": point[1]}}}


def _seconds(value: Any) -> int | None:
    """Google's "123s" (or "1.5s") as whole seconds."""
    match = _DURATION.match(value) if isinstance(value, str) else None
    return round(float(match.group(1))) if match else None


def _text(value: Any) -> str | None:
    """A localized text ({"text": ...}) or a plain string."""
    if isinstance(value, dict):
        value = value.get("text")
    return (value.strip() or None) if isinstance(value, str) else None


def _place(item: Any, centre: tuple[float, float] | None, detail: str | None) -> dict[str, Any]:
    item = item if isinstance(item, dict) else {}
    address = item.get("formattedAddress") if isinstance(item.get("formattedAddress"), str) else None
    shown: dict[str, Any] = {"name": _text(item.get("displayName")) or (address or "").split(",")[0].strip() or "?"}
    if address:
        shown["address"] = address
    category = _text(item.get("primaryTypeDisplayName"))
    if category:
        shown["category"] = category
    _locate(shown, item.get("location") or {}, centre, detail)
    if isinstance(item.get("id"), str) and item["id"]:
        shown["place_id"] = item["id"]
    return shown


def _address(item: Any) -> dict[str, Any]:
    """A Geocoding result: the address is the answer."""
    item = item if isinstance(item, dict) else {}
    address = item.get("formatted_address") if isinstance(item.get("formatted_address"), str) else ""
    shown: dict[str, Any] = {"name": address.split(",")[0].strip() or "?"}
    if address:
        shown["address"] = address
    location = (item.get("geometry") or {}).get("location") or {}
    _locate(shown, {"latitude": location.get("lat"), "longitude": location.get("lng")}, None, None)
    if isinstance(item.get("place_id"), str) and item["place_id"]:
        shown["place_id"] = item["place_id"]
    return shown


def _locate(shown: dict[str, Any], location: Any, centre: tuple[float, float] | None, detail: str | None) -> None:
    location = location if isinstance(location, dict) else {}
    lat, lon = location.get("latitude"), location.get("longitude")
    if _real(lat, -90, 90) and _real(lon, -180, 180):
        shown["lat"], shown["lon"] = lat, lon
        if centre is not None:
            metres = _metres_between(centre, (lat, lon))
            shown["distance_m"] = round(metres, -2) if detail == "area" else round(metres)


def _step(item: Any) -> dict[str, Any] | None:
    """One turn, or one ride for transit, as text."""
    if not isinstance(item, dict):
        return None
    text = _text((item.get("navigationInstruction") or {}).get("instructions"))
    transit = item.get("transitDetails")
    if isinstance(transit, dict):
        line = transit.get("transitLine") or {}
        stops = transit.get("stopDetails") or {}
        vehicle = _text(((line.get("vehicle") or {}).get("name"))) or "Transit"
        name = _text(line.get("nameShort")) or _text(line.get("name"))
        ride = " ".join(part for part in (vehicle, name) if part)
        headsign = _text(transit.get("headsign"))
        board = _text((stops.get("departureStop") or {}).get("name"))
        alight = _text((stops.get("arrivalStop") or {}).get("name"))
        text = ride + (f" towards {headsign}" if headsign else "") + (f" from {board}" if board else "") + \
            (f" to {alight}" if alight else "")
        count = transit.get("stopCount")
        if isinstance(count, int) and not isinstance(count, bool) and count > 0:
            text += f" ({count} stop{'s' if count != 1 else ''})"
    if not text:
        return None
    step: dict[str, Any] = {"text": text}
    if isinstance(item.get("distanceMeters"), int):
        step["distance_m"] = item["distanceMeters"]
    return step
