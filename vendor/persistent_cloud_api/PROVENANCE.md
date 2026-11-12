# provenance

- upstream: https://github.com/CelestoAI/celesto
- path: `src/_celesto_cloud_api`
- commit: `28599cba3f96d7ef36071833b9874d8e319b277a`
- licence: Apache-2.0 (see `LICENSE` in this directory)
- generated from: `openapi/cloud.json` (same commit) by openapi-python-client
- local changes: none to any file. only the directory name changed, so the package
  is imported by what it is for rather than by whose project it came from:
  `_celesto_cloud_api` -> `vendor/persistent_cloud_api`. every import inside it was
  already relative, so nothing in the tree needed an edit for the rename.

used by `ingredients/machines/persistent/client.py`: computers
create/get/list/exec/start/stop/delete. the project naming this upstream is
`crittertown/thirdparty.md`.
