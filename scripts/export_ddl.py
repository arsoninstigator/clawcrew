"""print the schema as postgres ddl, so insforge or supabase can host the rows.

`core.db.SCHEMA` is sqlite dialect on purpose: it is the file the app writes and the
tests run against. `core.dialect` is the one translation step, shared with the
postgres store, so this script cannot drift from what the runtime creates.
"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")

from core.db import SCHEMA  # noqa: E402
from core.dialect import generate_ddl  # noqa: E402

if __name__ == "__main__":
    sys.stdout.write(generate_ddl(SCHEMA))
