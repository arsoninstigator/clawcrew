# provenance

- upstream: https://github.com/e2b-dev/E2B
- path: `packages/python-sdk/e2b/api/client`
- commit: `cf7208ad85d5a747ba50ddf4e3bbdd364e4851bf`
- licence: MIT (see `LICENSE` in this directory)
- generated from: `spec/openapi.yml` (same commit) by openapi-python-client
- local changes: none. relative imports only, so the package needed no edits. the
  e2b sdk wraps it with `auth_header_name="X-API-KEY"`; our `sandbox/transport.py`
  does the same.

used by `clawcrew/sandbox/e2b.py`: sandbox create/get/pause/resume/connect/delete.
command execution is not here: it goes over envd, which is the sdk's job.
