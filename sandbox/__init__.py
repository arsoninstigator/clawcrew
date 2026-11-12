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
from sandbox import local
from ingredients.machines.ephemeral import client as ephemeral_client
from ingredients.machines.hosted import client as hosted_client
from ingredients.machines.persistent import client as persistent_client
from ingredients.machines.persistent import local as persistent_local

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
        self._persistent_oss = persistent_local.make(
            settings.persistent_bin, boot_timeout=settings.sandbox_timeout_seconds
        )
        self._persistent = persistent_client.make(
            settings.persistent_api_key, settings.persistent_base_url, settings.persistent_default_image
        )
        self._ephemeral = ephemeral_client.make(settings.ephemeral_api_key, settings.ephemeral_base_url, settings.ephemeral_default_template)

    def get(self, kind: str) -> SandboxProvider:
        """kind: 'persistent' | 'ephemeral' | provider name."""
        kind = (kind or "ephemeral").lower()
        if kind in {"persistent", "persistent-local", "persistent"}:
            # the OSS runtime is the default: their code, on this machine, no account
            if self.settings.persistent_mode != "cloud" and self._persistent_oss.configured():
                return self._persistent_oss
            if self._persistent.configured():
                return self._persistent
            return self._fallback("persistent")
        if kind in {"ephemeral", "ephemeral"}:
            if self._ephemeral.configured():
                return self._ephemeral
            return self._fallback("ephemeral")
        if kind in {"local", "both"}:
            return self._local
        raise ProviderNotConfigured(f"unknown sandbox provider '{kind}'", provider=kind)

    def _fallback(self, name: str) -> SandboxProvider:
        if not self.settings.allow_local_fallback:
            hint = (
                "pip install persistent && persistent setup, or set PERSISTENT_MODE=cloud + PERSISTENT_API_KEY"
                if name == "persistent"
                else "point EPHEMERAL_BASE_URL at your self-hosted ephemeral and set EPHEMERAL_API_KEY"
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
            for provider in (self._persistent_oss, self._persistent, self._ephemeral, self._local)
        ]


def build_registry(settings: Settings) -> ProviderRegistry:
    return ProviderRegistry(settings)


def resolver(registry: ProviderRegistry) -> Callable[[str], SandboxProvider]:
    return registry.get
