"""provider registry.

`providers.get("persistent")` is the only call orchestration code makes. the
resolver prefers the cloud provider and falls back to the local shell provider
only when explicitly allowed, so a production deploy that forgot an api key fails
loudly instead of running agent commands on the host.
"""

from __future__ import annotations

import os
from typing import Callable

from core.config import Settings
from core.errors import ProviderNotConfigured
from sandbox.base import BaseProvider, ExecResult, Handle, SandboxProvider, validate_spec  # noqa: F401
from sandbox import celesto, celesto_local, e2b, local

__all__ = [
    "ExecResult",
    "Handle",
    "SandboxProvider",
    "ProviderRegistry",
    "build_registry",
    "resolver",
]


class ProviderRegistry:
    def __init__(self, settings: Settings):
        self.settings = settings
        db_dir = os.path.dirname(os.path.abspath(settings.database_path))
        self._local = local.LocalProvider(os.path.join(db_dir, "sandboxes"))
        self._celesto_oss = celesto_local.make(
            settings.celesto_bin, boot_timeout=settings.sandbox_timeout_seconds
        )
        self._celesto = celesto.make(
            settings.celesto_api_key, settings.celesto_base_url, settings.celesto_default_image
        )
        self._e2b = e2b.make(settings.e2b_api_key, settings.e2b_base_url, settings.e2b_default_template)

    def get(self, kind: str) -> SandboxProvider:
        """kind: 'persistent' | 'ephemeral' | provider name."""
        kind = (kind or "ephemeral").lower()
        if kind in {"celesto", "celesto-local", "persistent"}:
            # the OSS runtime is the default: their code, on this machine, no account
            if self.settings.celesto_mode != "cloud" and self._celesto_oss.configured():
                return self._celesto_oss
            if self._celesto.configured():
                return self._celesto
            return self._fallback("celesto")
        if kind in {"e2b", "ephemeral"}:
            if self._e2b.configured():
                return self._e2b
            return self._fallback("e2b")
        if kind in {"local", "both"}:
            return self._local
        raise ProviderNotConfigured(f"unknown sandbox provider '{kind}'", provider=kind)

    def _fallback(self, name: str) -> SandboxProvider:
        if not self.settings.allow_local_fallback:
            hint = (
                "pip install celesto && celesto setup, or set CELESTO_MODE=cloud + CELESTO_API_KEY"
                if name == "celesto"
                else "point E2B_BASE_URL at your self-hosted e2b and set E2B_API_KEY"
            )
            raise ProviderNotConfigured(
                f"{name} is not configured and local fallback is disabled",
                provider=name,
                hint=hint,
            )
        return self._local

    def status(self) -> list[dict]:
        return [
            {
                "name": provider.name,
                "kind": provider.kind,
                "configured": provider.configured(),
                "isolated": getattr(provider, "isolated", None),
                "can_exec": getattr(provider, "can_exec", lambda: provider.configured())(),
            }
            for provider in (self._celesto_oss, self._celesto, self._e2b, self._local)
        ]


def build_registry(settings: Settings) -> ProviderRegistry:
    return ProviderRegistry(settings)


def resolver(registry: ProviderRegistry) -> Callable[[str], SandboxProvider]:
    return registry.get
