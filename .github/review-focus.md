This repository is the Talaria plugin for Hermes Agent (Python, loaded into Hermes) and, in
relay/, the Talaria push relay (Node.js, standard library only, run in Docker). Look
especially for:

- Python: exceptions escaping a hook or tool (a plugin must never break Hermes' turn), type
  mistakes, mutable default arguments, file writes that aren't atomic or owner-only, locks
  held across network calls.
- Node.js (relay/): unhandled promise rejections, missing request body limits, prototype
  pollution from parsed JSON, resource leaks, rate limits that can be bypassed.
- Security: credentials or keys logged or returned; missing auth on a dashboard route;
  injection in strings built for shell, SQL or URLs; timing-unsafe secret comparison; the
  relay learning more than routing data (content stays sealed end to end).
- Docker and deploy files: running as root, writable filesystem, secrets in image layers or
  environment.
- Names shared with the Talaria app (routes, push data keys, error codes, encryption labels,
  tool names, links): a change to one needs the same change in the app, said in the PR's
  Siblings section.
- A plugin version bump changes the files CONTRIBUTING.md lists, with a CHANGELOG.md entry.
- Text the models read (tool descriptions, results, skills) is behaviour: check it says what
  the code does.
