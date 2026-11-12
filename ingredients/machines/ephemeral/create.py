"""create one disposable ephemeral sandbox from the command line.

`clawcrew get disposable --target ephemeral --apply` calls this, so the api key path and
the embed path end up in the same place: a handle on stdout that the rest of
clawcrew already knows how to use.

    python -m clawcrew.create.ingredients.machines.ephemeral.create --template base
"""

from __future__ import annotations

import argparse
import json

from core.config import Settings
from sandbox import build_registry
from core.errors import ClawCrewError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ephemeral_create")
    parser.add_argument("--template", default="")
    parser.add_argument("--ttl", type=int, default=3600)
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    registry = build_registry(settings)
    provider = registry.get("ephemeral")
    spec = {"template": args.template or settings.ephemeral_default_template,
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
