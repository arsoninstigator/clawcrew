"""per-agent credentials, brokered instead of handed over.

the rule this module exists to enforce: an agent never holds a provider key. a key in an
environment variable is a key in a prompt injection away from being exfiltrated, and a key
in a run row is a key in a log. so crittertown stores the *name* of the variable a brokered
token arrives in, and the sandbox talks to a credential proxy (agent-vault by credential-broker,
or any proxy with the same shape) that attaches the real credential on the way out.

what lives in this file:

- grants: agent -> proxy url, token variable name, host scopes, expiry, issuer
- the egress decision: is this agent allowed to reach this host through the broker
- the env contract a sandbox needs, resolved at spawn time
- revocation that leaves a row behind, because an audit trail that deletes itself is not one

what deliberately does not live here: secret values, and any invented management endpoint.
the proxy is administered out of band (its own ui, cli or api), and crittertown refuses to
guess at paths it has not been told about.
"""

from __future__ import annotations

import os
from typing import Any, Mapping

from core.db import Store, dumps, loads, new_id, now
from core.errors import NotFound, ProviderNotConfigured, ValidationError

GRANT_STATUSES = ("active", "revoked", "expired")


def _host_matches(scope: str, host: str) -> bool:
    """`api.github.com`, `*.github.com` and `github.com` all cover their own subtree."""
    scope = scope.strip().lower()
    host = host.strip().lower().split(":")[0]
    if not scope or not host:
        return False
    if scope.startswith("*."):
        scope = scope[2:]
    return host == scope or host.endswith("." + scope)


class CredentialBroker:
    """grants, egress policy and the env contract, for one sqlite file of agents."""

    def __init__(self, store: Store, crews=None, audit=None, settings=None,
                 env: Mapping[str, str] | None = None):
        self.store = store
        self.crews = crews
        self.audit = audit
        self.settings = settings
        self.env = env if env is not None else os.environ

    # ---- configuration -------------------------------------------------
    @property
    def proxy_url(self) -> str:
        return getattr(self.settings, "vault_proxy_url", "") or ""

    @property
    def base_url(self) -> str:
        return getattr(self.settings, "vault_base_url", "") or ""

    def configured(self) -> bool:
        return bool(self.proxy_url or self.base_url)

    def health(self) -> dict:
        """what is configured, and what is missing. never a fabricated "ok"."""
        return {
            "configured": self.configured(),
            "proxy_url": self.proxy_url,
            "base_url": self.base_url,
            "grants": len([g for g in self.list() if g["status"] == "active"]),
            "note": "" if self.configured() else
                    "set CREDENTIAL_BROKER_PROXY_URL (and CREDENTIAL_BROKER_URL if you want the "
                    "management api linked) to broker credentials; until then agents "
                    "keep using plain environment variables",
        }

    # ---- rows ----------------------------------------------------------
    def _grant(self, row) -> dict:
        return {
            "id": row["id"],
            "team_id": row["team_id"],
            "agent_id": row["agent_id"],
            "label": row["label"],
            "proxy_url": row["proxy_url"],
            "token_env": row["token_env"],
            "scopes": loads(row["scopes"], []),
            "status": row["status"],
            "issued_by": row["issued_by"],
            "expires_at": row["expires_at"],
            "created_date": row["created_date"],
            "revoked_at": row["revoked_at"],
        }

    def get(self, grant_id: str) -> dict:
        row = self.store.one("select * from credential_broker where id = ?", (grant_id,))
        if not row:
            raise NotFound(f"vault grant {grant_id} not found")
        return self._grant(row)

    def list(self, agent_id: str | None = None, team_id: str | None = None,
             status: str | None = None) -> list[dict]:
        sql = "select * from credential_broker where 1 = 1"
        args: list[Any] = []
        if agent_id:
            sql += " and agent_id = ?"
            args.append(agent_id)
        if team_id:
            sql += " and team_id = ?"
            args.append(team_id)
        if status:
            sql += " and status = ?"
            args.append(status)
        sql += " order by created_date desc"
        grants = [self._grant(row) for row in self.store.query(sql, tuple(args))]
        for grant in grants:
            if grant["status"] == "active" and grant["expires_at"] and grant["expires_at"] < now():
                self._set_status(grant["id"], "expired")
                grant["status"] = "expired"
        return grants

    def _set_status(self, grant_id: str, status: str) -> None:
        stamp = now() if status == "revoked" else None
        with self.store.tx() as c:
            c.execute(
                "update credential_broker set status = ?, revoked_at = coalesce(?, revoked_at) where id = ?",
                (status, stamp, grant_id),
            )

    # ---- lifecycle -----------------------------------------------------
    def grant(self, agent_id: str, label: str = "", token_env: str = "",
              proxy_url: str = "", scopes: list[str] | None = None,
              expires_hours: float | None = None, issued_by: str = "owner") -> dict:
        """give one agent one brokered credential path. never the credential itself."""
        agent = self.crews.get_agent(agent_id) if self.crews is not None else {"team_id": ""}
        token_env = (token_env or "").strip()
        if not token_env:
            raise ValidationError("token_env is required: the name of the variable the "
                                 "brokered token arrives in")
        if not token_env.isidentifier():
            raise ValidationError(f"token_env '{token_env}' is not a valid variable name")
        proxy = (proxy_url or self.proxy_url or "").strip()
        if not proxy:
            raise ProviderNotConfigured(
                "no credential proxy configured; set CREDENTIAL_BROKER_PROXY_URL or pass proxy_url"
            )
        scopes = [s.strip().lower() for s in (scopes or []) if str(s).strip()]
        hours = float(expires_hours if expires_hours is not None
                      else getattr(self.settings, "vault_ttl_hours", 24) or 24)
        stamp = now()
        existing = self.store.one(
            "select id from credential_broker where agent_id = ? and label = ?",
            (agent_id, label or ""),
        )
        expires = stamp + hours * 3600 if hours else None
        common = (agent.get("team_id", ""), proxy, token_env, dumps(scopes),
                  issued_by, expires, stamp)
        if existing:
            grant_id = existing["id"]
            with self.store.tx() as c:
                c.execute(
                    """update credential_broker set team_id = ?, proxy_url = ?, token_env = ?,
                       scopes = ?, status = 'active', issued_by = ?, expires_at = ?,
                       created_date = ?, revoked_at = null where id = ?""",
                    common + (grant_id,),
                )
        else:
            grant_id = new_id("vlt")
            with self.store.tx() as c:
                c.execute(
                    """insert into credential_broker (team_id, proxy_url, token_env, scopes, status,
                       issued_by, expires_at, created_date, id, agent_id, label)
                       values (?,?,?,?,?,?,?,?,?,?,?)""",
                    (common[0], common[1], common[2], common[3], "active", common[4],
                     common[5], common[6], grant_id, agent_id, label or ""),
                )
        if self.audit is not None:
            self.audit.record_event("vault.granted", {
                "agent_id": agent_id, "agent_team_id": agent.get("team_id", ""),
                "grant_id": grant_id, "label": label,
                "scopes": scopes, "actor": issued_by,
            })
        return self.get(grant_id)

    def revoke(self, grant_id: str, actor: str = "owner", reason: str = "") -> dict:
        grant = self.get(grant_id)
        if grant["status"] == "revoked":
            return grant
        self._set_status(grant_id, "revoked")
        if self.audit is not None:
            self.audit.record_event("vault.revoked", {
                "agent_id": grant["agent_id"], "agent_team_id": grant["team_id"],
                "grant_id": grant_id, "reason": reason, "actor": actor,
            })
        return self.get(grant_id)

    # ---- what a sandbox gets -------------------------------------------
    def active(self, agent_id: str) -> list[dict]:
        return [g for g in self.list(agent_id=agent_id) if g["status"] == "active"]

    def egress_allowed(self, agent_id: str, host: str) -> dict:
        """may this agent reach this host through the broker, and why not if not.

        a grant with no scopes does not claim to police hosts: the proxy's own service
        rules do. a grant with scopes is enforced here as well, so a scoped grant cannot
        be widened by editing the proxy.
        """
        grants = self.active(agent_id)
        if not grants:
            return {"allowed": False, "reason": "no active vault grant for this agent"}
        for grant in grants:
            if not grant["scopes"]:
                return {"allowed": True, "grant": grant["id"],
                        "reason": "unscoped grant: the proxy decides"}
            if any(_host_matches(scope, host) for scope in grant["scopes"]):
                return {"allowed": True, "grant": grant["id"], "reason": "scope matched"}
        return {"allowed": False,
                "reason": f"{host} is not in any scope of this agent's grants",
                "scopes": sorted({s for g in grants for s in g["scopes"]})}

    def sandbox_env(self, agent_id: str, env: Mapping[str, str] | None = None) -> dict:
        """the variables to hand a sandbox at spawn time.

        the token is read from this process's environment at call time and is never
        stored, logged or returned by an api route: only whether it was found.
        """
        grants = self.active(agent_id)
        if not grants:
            return {}
        env = env if env is not None else self.env
        out: dict[str, str] = {}
        for grant in grants:
            token = (env.get(grant["token_env"]) or "").strip()
            if not token:
                raise ProviderNotConfigured(
                    f"grant '{grant['label'] or grant['id']}' expects a token in "
                    f"{grant['token_env']}, and this process does not have it"
                )
            out["HTTPS_PROXY"] = grant["proxy_url"]
            out["HTTP_PROXY"] = grant["proxy_url"]
            out[grant["token_env"]] = token
            out["CREDENTIAL_BROKER_GRANT"] = grant["id"]
        return out

    def describe_env(self, agent_id: str, env: Mapping[str, str] | None = None) -> dict:
        """what the sandbox would receive, with every value masked but the shape kept."""
        env = env if env is not None else self.env
        shape: dict[str, Any] = {}
        for grant in self.active(agent_id):
            shape[grant["token_env"]] = "<set>" if (env.get(grant["token_env"]) or "").strip() else "<missing>"
            shape["HTTPS_PROXY"] = grant["proxy_url"]
            shape["HTTP_PROXY"] = grant["proxy_url"]
        return {"agent_id": agent_id, "env": shape, "grants": len(self.active(agent_id))}
