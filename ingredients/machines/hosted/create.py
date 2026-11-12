"""create one hosted sandbox from the command line, for `crittertown get --apply`.

    python -m ingredients.machines.hosted.create --name box --template hosted/desktop
"""

from __future__ import annotations

import argparse
import json

from core.config import Settings
from sandbox import build_registry
from core.errors import CritterTownError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hosted_create")
    parser.add_argument("--name", default="crittertown-1")
    parser.add_argument("--template", default="")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    provider = build_registry(settings).get("hosted")
    try:
        handle = provider.ensure(args.name, {"name": args.name, "template": args.template})
    except CritterTownError as exc:
        print(json.dumps(exc.to_dict(), indent=2))
        return 1
    print(json.dumps({"handle": handle.to_dict()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
