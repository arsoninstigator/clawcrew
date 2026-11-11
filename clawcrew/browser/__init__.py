"""browser access: open source driver, self-hosted browser. see `base` for the rules."""

from __future__ import annotations

from ..errors import ValidationError
from .base import SCOPES, BrowserProvider, BrowserService, NullProvider, Session
from .browser_use import BrowserUseProvider
from .kernel_images import KernelImagesProvider

PROVIDERS = ("browser-use", "kernel-images", "none")


def build_providers(settings=None, computer_cdp=None) -> dict:
    """the two open source lanes, plus a way to say no.

    `computer_cdp` lets browser-use drive the chrome already inside an agent's
    persistent computer instead of starting a second browser next to it.
    """
    return {
        "browser-use": BrowserUseProvider(settings=settings, computer_cdp=computer_cdp),
        "kernel-images": KernelImagesProvider(settings=settings),
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
    "BrowserUseProvider",
    "KernelImagesProvider",
    "NullProvider",
    "PROVIDERS",
    "SCOPES",
    "Session",
    "build_providers",
    "build_service",
    "resolve",
]
