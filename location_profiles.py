"""Location shared with the other profiles on this host (#287).

Talaria sends the user's location to one profile: the one sharing is bound
to, which keeps the store, the agreed key and the place rules. With "Share
with this host's other profiles" on in Talaria (``sharing.other_profiles``),
every other profile on the same host that is paired with the same phone
(the same relay installation) reads that store as its own: ``now``,
``places``, ``history``, ``distance_to``, ``locate`` and maps lookups. A
locate goes through the asking profile's own pairing, naming the sharing
profile, so the phone answers it like one of that profile's. Place rules
stay with the sharing profile.

A profile that shares location itself (talking or background on) always
reads its own store. If several profiles share with the others, the first
one wins: the default profile, then named ones alphabetically.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from .location_store import LocationStore

PAIRING_FILE = "talaria-push.json"
# A folder name goes into notes the agent reads and into the access log, and
# is sent to the phone as is, so one that would need cleaning isn't used.
_UNUSABLE_NAME = re.compile("[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff]")


def store_for(home: str | Path, make: Callable[[Any], LocationStore] = LocationStore) -> tuple[LocationStore, str | None]:
    """The store this profile reads, and the name of the profile it belongs
    to when that's another one (None for its own). ``make`` opens a store."""
    own = make(home)
    if _sharing_on(own):
        return own, None
    installation = _installation(Path(home))
    if installation:
        for name, path in profile_homes(Path(home)):
            if len(name) > 64 or _UNUSABLE_NAME.search(name) or _installation(path) != installation:
                continue
            store = make(path)
            if not store.exists():
                continue
            view = store.snapshot(upgrade=False)  # never another profile's write lock
            if view is not None and view["sharing"].get("other_profiles") is True and _on(view["sharing"]):
                return store, name
    return own, None


def profile_homes(home: Path) -> list[tuple[str, Path]]:
    """Every other Hermes profile on this host, as (name, home): the default
    profile is the root, named ones live in its profiles/ folder."""
    root = home.parent.parent if home.parent.name == "profiles" else home
    try:
        named = sorted(path for path in (root / "profiles").iterdir() if path.is_dir())
    except OSError:
        named = []
    try:
        own = home.resolve()
    except (OSError, RuntimeError):  # RuntimeError: a symlink loop
        return []
    found, seen = [], {own}
    for name, path in [("default", root)] + [(path.name, path) for path in named if path.name != "default"]:
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError):
            continue
        if resolved not in seen:
            seen.add(resolved)
            found.append((name, path))
    return found


def label(name: str) -> str:
    """A profile's name as it may go into another profile's access log."""
    return _UNUSABLE_NAME.sub("", name)[:64]


def _sharing_on(store: LocationStore) -> bool:
    if not store.exists():
        return False
    view = store.snapshot()
    return view is not None and _on(view["sharing"])


def _on(sharing: dict[str, Any]) -> bool:
    return bool(sharing.get("talking") or sharing.get("background"))


def _installation(home: Path) -> str:
    """The relay installation (the phone) this profile is paired with, or ""."""
    try:
        value = json.loads((home / PAIRING_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    installation = value.get("installation_id") if isinstance(value, dict) else None
    credential = value.get("credential") if isinstance(value, dict) else None
    return installation if isinstance(installation, str) and isinstance(credential, str) and credential else ""

