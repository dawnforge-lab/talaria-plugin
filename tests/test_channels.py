"""channels.yaml tells Talaria's release builds which plugin version to
ask for. Talaria reads it from main, so a version main doesn't have would
keep the update notice up after every update."""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
STABLE = re.compile(r"^stable:\s*[\"']?([0-9]+(?:\.[0-9]+)*)[\"']?\s*(?:#.*)?$", re.MULTILINE)
VERSION = re.compile(r"^version:\s*[\"']?([0-9]+(?:\.[0-9]+)*)", re.MULTILINE)


def _parts(version):
    return tuple(int(part) for part in version.split("."))


def test_stable_is_a_version_main_has():
    stable = STABLE.search((ROOT / "channels.yaml").read_text(encoding="utf-8"))
    assert stable, "channels.yaml needs a `stable: X.Y.Z` line"
    version = VERSION.search((ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    assert version
    assert _parts(stable.group(1)) <= _parts(version.group(1)), (
        f"stable {stable.group(1)} is newer than plugin.yaml's {version.group(1)}")
