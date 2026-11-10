# provenance

- upstream: https://github.com/a2aproject/a2a-python
- paths: `src/a2a/compat/v0_3/types.py` -> `a2a_types/types.py`,
  `src/a2a/_base.py` -> `a2a_types/_base.py`
- commit: `2cc1dd5020874376ed293ef87089e5df9d205571`
- licence: Apache-2.0 (see `LICENSE` in this directory)
- generated from: `specification/json/a2a.json` in a2aproject/A2A by datamodel-codegen
- local changes: one import rewrite. `types.py` imported `from a2a._base import
  A2ABaseModel`, and the base module moved next to it, so the line is now
  `from ._base import A2ABaseModel`. nothing else was touched.

used by `clawcrew/a2a_protocol.py:validate_wire`, which round trips every outbound
message, task and agent card through these models so the protocol layer cannot drift
from the published schema.
