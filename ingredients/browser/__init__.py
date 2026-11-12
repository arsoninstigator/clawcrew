"""browser access: open source driver, self-hosted browser. see `base` for the rules."""

from __future__ import annotations

from core.errors import ValidationError
from ingredients.browser.base import SCOPES, BrowserProvider, BrowserService, NullProvider, Session
from ingredients.browser.driver import LocalBrowserProvider
from ingredients.browser.container_browser import ContainerBrowserProvider

PROVIDERS = ("container-browser", "local-browser", "none")


def build_providers(settings=None, computer_cdp=None) -> dict:
    """the two open source lanes, plus a way to say no.

    `computer_cdp` lets local-browser drive the chrome already inside an agent's
    persistent computer instead of starting a second browser next to it.
    """
    return {
        "local-browser": LocalBrowserProvider(settings=settings, computer_cdp=computer_cdp),
        "container-browser": ContainerBrowserProvider(settings=settings),
        "none": NullProvider(),
    }


def build_service(store, crews=None, settings=None, computer_cdp=None) -> BrowserService:
    return BrowserService(store, crews=crews, settings=settings,
                          providers=build_providers(settings, computer_cdp))


def resolve(name: str, providers: dict) -> BrowserProvider:
    """one name in, one provider out, with the hosted check applied in one place."""
    provider = providers.get((name or "").strip())
    if provider is None:
        raise ValidationError(f"unknown browser provider '{name}'; have {sorted(providers)}")
    return provider


__all__ = [
    "BrowserProvider",
    "BrowserService",
    "LocalBrowserProvider",
    "ContainerBrowserProvider",
    "NullProvider",
    "PROVIDERS",
    "SCOPES",
    "Session",
    "build_providers",
    "build_service",
    "resolve",
]
