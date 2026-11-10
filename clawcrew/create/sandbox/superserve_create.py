"""create one superserve sandbox from the command line, for `clawcrew get --apply`.

    python -m clawcrew.create.sandbox.superserve_create --name box --template superserve/desktop
"""

from __future__ import annotations

import argparse
import json

from clawcrew.config import Settings
from clawcrew.create.sandbox import build_registry
from clawcrew.errors import ClawCrewError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="superserve_create")
    parser.add_argument("--name", default="clawcrew-1")
    parser.add_argument("--template", default="")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    provider = build_registry(settings).get("superserve")
    try:
        handle = provider.ensure(args.name, {"name": args.name, "template": args.template})
    except ClawCrewError as exc:
        print(json.dumps(exc.to_dict(), indent=2))
        return 1
    print(json.dumps({"handle": handle.to_dict()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
