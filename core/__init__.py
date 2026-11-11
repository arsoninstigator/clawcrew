"""clawcrew backend.

crews of specialist agents, each with a persistent computer, a shared kanban, a
reviewable a2a channel, and visual workflows - self hosted.

    from core import build_app
    app = build_app()
    crew = app.crews.create_team("growth", mission="own the funnel")
"""

from core.app import App, build_app
from core.config import Settings
from core.errors import (
    ClawCrewError,
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
    "ClawCrewError",
    "Conflict",
    "NotAllowed",
    "NotFound",
    "ProviderFailure",
    "ProviderNotConfigured",
    "ValidationError",
    "__version__",
]
