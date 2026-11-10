"""runtime configuration for clawcrew.

everything is env-driven so a deployment is a set of env vars, no config files
are required. see .env.example in the repo root.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping


def _env(env: Mapping[str, str], key: str, default: str = "") -> str:
    return (env.get(key) or default).strip()


def _flag(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = _env(env, key)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    """process settings.

    the sandbox providers are optional: when a provider has no credentials the
    registry falls back to the local shell provider only if
    `allow_local_fallback` is set, otherwise it raises on use. that keeps a
    misconfigured production deploy loud instead of silently unsandboxed.
    """

    database_path: str = "clawcrew.db"
    allow_local_fallback: bool = True

    # persistent computers. "local" runs the celesto OSS runtime on this machine;
    # "cloud" is their hosted api and is opt-in only.
    celesto_mode: str = "local"
    celesto_bin: str = "celesto"
    celesto_api_key: str = ""
    celesto_base_url: str = "https://api.celesto.ai/v1"
    celesto_default_image: str = "clawcrew/persistent:latest"

    # ephemeral sandboxes. superserve is opt in: their runtime is their cloud,
    # their sdk is the contract, so it never pretends to be byoc.
    superserve_api_key: str = ""
    superserve_base_url: str = "https://api.superserve.ai"
    superserve_template: str = ""
    e2b_api_key: str = ""
    e2b_base_url: str = "https://api.e2b.dev"
    e2b_default_template: str = "base"

    # self-hosted workflow tools, plugged in over their own apis
    n8n_base_url: str = "http://localhost:5678"
    n8n_api_key: str = ""
    dify_base_url: str = "http://localhost/v1"
    dify_api_key: str = ""
    dify_user: str = "clawcrew"

    # scheduling broker: inproc | celery
    broker: str = "inproc"
    celery_broker_url: str = ""
    celery_worker_queue: str = "clawcrew"

    # model endpoint used by the default llm runtime (openai-compatible)
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = "gpt-4.1-mini"

    # credentials, brokered. an agent gets a proxy url and the name of a token
    # variable; the proxy attaches the real key on the way out.
    vault_base_url: str = ""
    vault_proxy_url: str = ""
    vault_ttl_hours: float = 24.0

    # approvals without a login: the link is the credential, so it needs a host to
    # be sent from and a secret to be signed with (generated per db if unset)
    gate_secret: str = ""
    gate_ttl_hours: float = 168.0
    public_base_url: str = ""

    # the morning brief reads the night: 18:00 to 06:00 by default
    brief_window_start_hour: int = 18
    brief_window_end_hour: int = 6

    # review defaults
    api_token: str = ""
    default_review_mode: str = "reviewed"
    max_parallel_runs: int = 4
    sandbox_timeout_seconds: int = 900

    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        return cls(
            database_path=_env(env, "CLAWCREW_DB", "clawcrew.db"),
            allow_local_fallback=_flag(env, "CLAWCREW_ALLOW_LOCAL_FALLBACK", True),
            celesto_mode=_env(env, "CELESTO_MODE", "local").lower(),
            celesto_bin=_env(env, "CELESTO_BIN", "celesto"),
            celesto_api_key=_env(env, "CELESTO_API_KEY"),
            celesto_base_url=_env(env, "CELESTO_BASE_URL", "https://api.celesto.ai/v1"),
            celesto_default_image=_env(env, "CELESTO_IMAGE", "clawcrew/persistent:latest"),
            superserve_api_key=_env(env, "SUPERSERVE_API_KEY"),
            superserve_base_url=_env(env, "SUPERSERVE_BASE_URL", "https://api.superserve.ai"),
            superserve_template=_env(env, "SUPERSERVE_TEMPLATE"),
            e2b_api_key=_env(env, "E2B_API_KEY"),
            e2b_base_url=_env(env, "E2B_BASE_URL", "https://api.e2b.dev"),
            e2b_default_template=_env(env, "E2B_TEMPLATE", "base"),
            n8n_base_url=_env(env, "CLAWCREW_N8N_BASE_URL", "http://localhost:5678"),
            n8n_api_key=_env(env, "CLAWCREW_N8N_API_KEY"),
            dify_base_url=_env(env, "CLAWCREW_DIFY_BASE_URL", "http://localhost/v1"),
            dify_api_key=_env(env, "CLAWCREW_DIFY_API_KEY"),
            dify_user=_env(env, "CLAWCREW_DIFY_USER", "clawcrew"),
            broker=_env(env, "CLAWCREW_BROKER", "inproc").lower(),
            celery_broker_url=_env(env, "CELERY_BROKER_URL"),
            celery_worker_queue=_env(env, "CELERY_QUEUE", "clawcrew"),
            llm_base_url=_env(env, "CLAWCREW_LLM_BASE_URL"),
            llm_api_key=_env(env, "CLAWCREW_LLM_API_KEY"),
            llm_model=_env(env, "CLAWCREW_LLM_MODEL", "gpt-4.1-mini"),
            vault_base_url=_env(env, "AGENT_VAULT_URL"),
            vault_proxy_url=_env(env, "AGENT_VAULT_PROXY_URL"),
            vault_ttl_hours=float(_env(env, "AGENT_VAULT_TTL_HOURS", "24") or 24),
            gate_secret=_env(env, "CLAWCREW_GATE_SECRET"),
            gate_ttl_hours=float(_env(env, "CLAWCREW_GATE_TTL_HOURS", "168") or 168),
            public_base_url=_env(env, "CLAWCREW_PUBLIC_BASE_URL"),
            brief_window_start_hour=int(_env(env, "CLAWCREW_BRIEF_START", "18") or 18),
            brief_window_end_hour=int(_env(env, "CLAWCREW_BRIEF_END", "6") or 6),
            api_token=_env(env, "CLAWCREW_API_TOKEN"),
            default_review_mode=_env(env, "CLAWCREW_REVIEW_MODE", "reviewed"),
            max_parallel_runs=int(_env(env, "CLAWCREW_MAX_PARALLEL", "4") or 4),
            sandbox_timeout_seconds=int(_env(env, "CLAWCREW_SANDBOX_TIMEOUT", "900") or 900),
        )
