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

    # persistent computers. "local" runs the persistent OSS runtime on this machine;
    # "cloud" is their hosted api and is opt-in only.
    persistent_mode: str = "local"
    persistent_bin: str = "persistent"
    persistent_api_key: str = ""
    persistent_base_url: str = "https://api.machines.internal/v1"
    persistent_default_image: str = "clawcrew/persistent:latest"

    # ephemeral sandboxes. hosted is opt in: their runtime is their cloud,
    # their sdk is the contract, so it never pretends to be byoc.
    hosted_api_key: str = ""
    hosted_base_url: str = "https://api.hosted.ai"
    hosted_template: str = ""
    ephemeral_api_key: str = ""
    ephemeral_base_url: str = "https://api.ephemeral.internal"
    ephemeral_default_template: str = "base"
    ephemeral_exec_module: str = ""

    # self-hosted workflow tools, plugged in over their own apis
    n8n_base_url: str = "http://localhost:5678"
    n8n_api_key: str = ""
    manage_base_url: str = "http://localhost:4000"
    manage_api_key: str = ""
    workflows_base_url: str = "http://localhost:7860"
    workflows_api_key: str = ""
    dify_base_url: str = "http://localhost/v1"
    dify_api_key: str = ""
    dify_user: str = "clawcrew"
    # a toolbox entry: an agent calls it, the platform does not run it
    firecrawl_api_key: str = ""
    firecrawl_base_url: str = "https://api.firecrawl.dev/v1"

    # scheduling broker: inproc | schedule
    broker: str = "inproc"
    schedule_broker_url: str = ""
    schedule_worker_queue: str = "clawcrew"

    # model endpoint used by the default llm runtime (openai-compatible)
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = "gpt-4.1-mini"

    # credentials, brokered. an agent gets a proxy url and the name of a token
    # variable; the proxy attaches the real key on the way out.
    vault_base_url: str = ""
    vault_proxy_url: str = ""
    vault_ttl_hours: float = 24.0

    # comms: rooms whose members are agents, over a signed event log. custody
    # "local" means this node mints and holds each agent's key; "external" means
    # only public halves are stored and signing happens elsewhere (not built).
    comms_custody: str = "local"

    # browser access: open source driver, self-hosted browser, no account and no key.
    # `local-browser` (mit) drives any cdp endpoint; `container-browser` (apache-2.0) runs
    # chrome in a local container; `none` gives an agent no browser at all.
    browser_provider: str = "local-browser"
    browser_profile_root: str = ""
    container_browser_image: str = ""
    browser_cdp_url: str = ""
    # this build makes agents the only first class members of a room; people can
    # be enabled, and nothing else about the log changes when they are
    comms_humans_enabled: bool = False
    comms_relay: str = ""
    comms_relay_transport: str = "nostr"
    comms_message_limit: int = 200

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
            persistent_mode=_env(env, "PERSISTENT_MODE", "local").lower(),
            persistent_bin=_env(env, "PERSISTENT_BIN", "persistent"),
            persistent_api_key=_env(env, "PERSISTENT_API_KEY"),
            persistent_base_url=_env(env, "PERSISTENT_BASE_URL", "https://api.machines.internal/v1"),
            persistent_default_image=_env(env, "PERSISTENT_IMAGE", "clawcrew/persistent:latest"),
            hosted_api_key=_env(env, "HOSTED_API_KEY"),
            hosted_base_url=_env(env, "HOSTED_BASE_URL", "https://api.hosted.ai"),
            hosted_template=_env(env, "HOSTED_TEMPLATE"),
            ephemeral_api_key=_env(env, "EPHEMERAL_API_KEY"),
            ephemeral_base_url=_env(env, "EPHEMERAL_BASE_URL", "https://api.ephemeral.internal"),
            ephemeral_default_template=_env(env, "EPHEMERAL_TEMPLATE", "base"),
            ephemeral_exec_module=_env(env, "EPHEMERAL_EXEC_MODULE"),
            n8n_base_url=_env(env, "CLAWCREW_N8N_BASE_URL", "http://localhost:5678"),
            n8n_api_key=_env(env, "CLAWCREW_N8N_API_KEY"),
            manage_base_url=_env(env, "CLAWCREW_MANAGE_BASE_URL", "http://localhost:4000"),
            manage_api_key=_env(env, "CLAWCREW_MANAGE_API_KEY"),
            workflows_base_url=_env(env, "CLAWCREW_WORKFLOWS_BASE_URL", "http://localhost:7860"),
            workflows_api_key=_env(env, "CLAWCREW_WORKFLOWS_API_KEY"),
            dify_base_url=_env(env, "CLAWCREW_DIFY_BASE_URL", "http://localhost/v1"),
            dify_api_key=_env(env, "CLAWCREW_DIFY_API_KEY"),
            dify_user=_env(env, "CLAWCREW_DIFY_USER", "clawcrew"),
            firecrawl_api_key=_env(env, "CLAWCREW_FIRECRAWL_API_KEY"),
            firecrawl_base_url=_env(env, "CLAWCREW_FIRECRAWL_BASE_URL", "https://api.firecrawl.dev/v1"),
            broker=_env(env, "CLAWCREW_BROKER", "inproc").lower(),
            schedule_broker_url=_env(env, "SCHEDULE_BROKER_URL"),
            schedule_worker_queue=_env(env, "SCHEDULE_QUEUE", "clawcrew"),
            llm_base_url=_env(env, "CLAWCREW_LLM_BASE_URL"),
            llm_api_key=_env(env, "CLAWCREW_LLM_API_KEY"),
            llm_model=_env(env, "CLAWCREW_LLM_MODEL", "gpt-4.1-mini"),
            vault_base_url=_env(env, "CREDENTIAL_BROKER_URL"),
            vault_proxy_url=_env(env, "CREDENTIAL_BROKER_PROXY_URL"),
            vault_ttl_hours=float(_env(env, "CREDENTIAL_BROKER_TTL_HOURS", "24") or 24),
            comms_custody=_env(env, "CLAWCREW_COMMS_CUSTODY", "local").lower(),
            browser_provider=_env(env, "CLAWCREW_BROWSER_PROVIDER", "local-browser").lower(),
            browser_profile_root=_env(env, "CLAWCREW_BROWSER_PROFILE_ROOT"),
            container_browser_image=_env(env, "CLAWCREW_CONTAINER_BROWSER_IMAGE"),
            browser_cdp_url=_env(env, "CLAWCREW_BROWSER_CDP"),
            comms_humans_enabled=_flag(env, "CLAWCREW_COMMS_HUMANS", False),
            comms_relay=_env(env, "CLAWCREW_COMMS_RELAY"),
            comms_relay_transport=_env(env, "CLAWCREW_COMMS_RELAY_TRANSPORT", "nostr").lower(),
            comms_message_limit=int(_env(env, "CLAWCREW_COMMS_LIMIT", "200") or 200),
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
