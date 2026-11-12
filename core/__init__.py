"""crittertown backend.

crews of specialist agents, each with a persistent computer, a shared kanban, a
reviewable a2a channel, and visual workflows - self hosted.

    from core import build_app
    app = build_app()
    crew = app.crews.create_team("growth", mission="own the funnel")

importing a `core.*` submodule must not build the object graph, so `App` and `build_app`
are the only names here that are lazy: a broker entrypoint or a config read pulls one
module, not the platform.
"""

from __future__ import annotations

from core.config import Settings
from core.errors import (
    CritterTownError,
    Conflict,
    NotAllowed,
    NotFound,
    ProviderFailure,
    ProviderNotConfigured,
    ValidationError,
)

__version__ = "0.1.0"

__all__ = [
    "App",
    "build_app",
    "Settings",
    "CritterTownError",
    "Conflict",
    "NotAllowed",
    "NotFound",
    "ProviderFailure",
    "ProviderNotConfigured",
    "ValidationError",
    "__version__",
]

_LAZY = ("App", "build_app")


def __getattr__(name: str):
    if name in _LAZY:  # pragma: no cover - exercised by any `from core import build_app`
        from core.app import App, build_app

        return {"App": App, "build_app": build_app}[name]
    raise AttributeError(f"module 'core' has no attribute {name!r}")
