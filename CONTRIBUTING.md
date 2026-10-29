# Contributing to talaria-plugin

This repo is the Talaria plugin for Hermes (`talaria_push`) and, in
`relay/`, the Talaria relay that delivers its pushes. The app is Talaria, for
Android, in its own repository.

## Connected features

Most features here have siblings in the app: a tool here and the screen that
offers it there, a route here and the screen that reads it there.

- Before changing a feature, find its siblings in the app and put every one
  on your plan before writing code.
- In the PR, fill in Siblings: for each sibling say done (name the app PR),
  not needed (and why), or a gap left open. "No siblings" is only for a
  change the app doesn't see.
- Text that describes a behavior is one of its siblings: tool descriptions and
  results the models read, README.md, and the app's briefings and Settings text.

## Plugin version

A version bump changes five files together: `plugin.yaml`, `events.py`,
`dashboard/manifest.json`, `tests/test_clarify_loop.py`, and a new entry on
top of `CHANGELOG.md` (Talaria shows it as the update notice). Parallel PRs
often pick the same next number, so take the next free version on main right
before merging. CI fails a PR whose version main already has.

## Tests

CI (`.github/workflows/tests.yml`) runs the plugin suite on every PR and every
push to main. To run them locally:

    pip install -r tests/requirements.txt
    python -m pytest --rootdir=tests tests

## Reviews

Each pull request gets an automated review: an AI model reads the diff and posts its findings
as one comment, updated on every push. It's advice, not a gate: the maintainer reviews every
PR and does the merging. PRs from forks are reviewed once a maintainer has read them and added
the `run-review` label. `main` only changes through pull requests whose checks pass.

## The relay

The relay in `relay/` is a small Node.js service with no npm dependencies,
and it should stay that way: the standard library only.

- Its API is shared with the plugin and the app: push data keys, error codes
  and routes change on every side together, in the same pull request here
  where they can.
- Keep the relay's view of a push to routing only: content stays sealed
  between the plugin and the phone. A change that would let the relay read
  more needs a strong reason in the PR.
- Tests need Node 22 or later. The end-to-end tests start real relay
  processes against a fake Google (`relay/testing/harness.mjs`), so nothing
  reaches Firebase. CI runs them on every PR:

      cd relay
      npm test

## Security

Please don't open a public issue for a security problem. Use GitHub's
private vulnerability reporting on this repository instead.
