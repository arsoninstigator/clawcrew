"""create one disposable e2b sandbox from the command line.

`clawcrew get disposable --target e2b --apply` calls this, so the api key path and
the embed path end up in the same place: a handle on stdout that the rest of
clawcrew already knows how to use.

    python -m clawcrew.create.sandbox.e2b_create --template base
"""

from __future__ import annotations

import argparse
import json

from clawcrew.config import Settings
from clawcrew.create.sandbox import build_registry
from clawcrew.errors import ClawCrewError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="e2b_create")
    parser.add_argument("--template", default="")
    parser.add_argument("--ttl", type=int, default=3600)
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    registry = build_registry(settings)
    provider = registry.get("e2b")
    spec = {"template": args.template or settings.e2b_default_template,
            "ttl_seconds": args.ttl}
    try:
        handle = provider.ensure("cli", spec)
    except ClawCrewError as exc:
        print(json.dumps(exc.to_dict(), indent=2))
        return 1
    print(json.dumps({"handle": handle.to_dict()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
