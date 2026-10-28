"""Hermes scans a plugin's whole checkout before it installs it. A literal
bidi override or zero-width character anywhere in the tree is a high
"injection" finding, and that verdict stops a one-tap install from Talaria.
Write these characters as escapes instead."""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
INVISIBLE = re.compile("[\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff]")
SKIP = {".git", "__pycache__", "node_modules", ".pytest_cache"}


def test_no_file_holds_a_literal_invisible_character():
    found = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or SKIP.intersection(path.relative_to(ROOT).parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.split("\n"), 1):
            if INVISIBLE.search(line):
                found.append(f"{path.relative_to(ROOT)}:{number}")
    assert not found, "write these characters as \\u escapes: " + ", ".join(found)
