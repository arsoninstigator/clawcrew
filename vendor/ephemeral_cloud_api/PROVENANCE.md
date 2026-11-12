# provenance

- upstream: https://github.com/e2b-dev/E2B
- path: `packages/python-sdk/e2b/api/client`
- commit: `cf7208ad85d5a747ba50ddf4e3bbdd364e4851bf`
- licence: MIT (see `LICENSE` in this directory)
- generated from: `spec/openapi.yml` (same commit) by openapi-python-client
- local changes: none to any file. relative imports only, so the package needed no
  edits, and only the directory name changed (`e2b_api` -> `ephemeral_cloud_api`) so
  it is imported by what it does. the sdk wraps this client with
  `auth_header_name="X-API-KEY"`; our `sandbox/transport.py` does the same.

used by `ingredients/machines/ephemeral/client.py`: sandbox
create/get/pause/resume/connect/delete. command execution is not here: it goes over
envd, which is the upstream sdk's job and stays behind `pip install -e ".[ephemeral]"`.
the project naming this upstream is `crittertown/thirdparty.md`.
