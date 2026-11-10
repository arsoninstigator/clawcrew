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

    # ephemeral sandboxes
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
            api_token=_env(env, "CLAWCREW_API_TOKEN"),
            default_review_mode=_env(env, "CLAWCREW_REVIEW_MODE", "reviewed"),
            max_parallel_runs=int(_env(env, "CLAWCREW_MAX_PARALLEL", "4") or 4),
            sandbox_timeout_seconds=int(_env(env, "CLAWCREW_SANDBOX_TIMEOUT", "900") or 900),
        )
