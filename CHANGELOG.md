# Talaria plugin changelog

What each plugin version brings, newest first. Talaria shows the entries
between the version on your host and the one an update installs, security
fixes first, so you can tell whether an update matters to you.

Every version bump in `plugin.yaml` adds an entry here (tests/test_changelog.py
checks it). The format is fixed, because Talaria reads it:

- `## <version> (<YYYY-MM-DD>)`: the version and the day it reached main.
- Then any of these sections, in this order: `### Security`, `### Fixes`,
  `### New and improved`. Leave out a section with nothing in it.
- One line per change, starting with `- `. Fixes say what goes wrong without
  the update. Plain words: these show on a phone.

## 1.0.0 (unreleased)

### New and improved
- First Talaria release, from the Conduit notifier.
