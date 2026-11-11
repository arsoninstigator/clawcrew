"""workspace connectors, and who is allowed to read them.

two halves, kept apart on purpose.

**adapters** turn a workspace tool into documents. every one answers the same four
calls (`probe`, `documents`, `search`, `read`), so adding notion, drive, a shared
folder or some internal wiki is a class, not a refactor. the token is never stored
in the database: a connector row names the environment variable that holds it,
because a config table is the first place credentials leak out of.

**access** is a grant per agent per connector, with an expiry, issued and revoked
by the supervisor of that agent's department. this is the hmas part: the supervisor
is a node in the org chart, not a settings page, so it hands out access because it
is accountable for the work, and it can take it back the same way. reads are
filtered by the grant, so an agent without one does not get an empty list that
looks like "nothing found": it gets a refusal that names who to ask.

the reason this is per agent and not per crew: an agent's knowledge is one of the
nine things that must never merge. a crew-wide connector bucket would be exactly
that merge, one sync at a time.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import httpx

from core.db import Store, dumps, loads, new_id, now
from core.errors import NotAllowed, NotFound, ProviderFailure, ProviderNotConfigured, ValidationError

MAX_DOCS = 200


# ---- adapters ----------------------------------------------------------
class Adapter:
    """what a workspace has to look like to be usable as knowledge."""

    kind = "base"

    def __init__(self, config: dict | None = None, token: str = "",
                 transport: httpx.BaseTransport | None = None):
        self.config = config or {}
        self.token = token or ""
        self.transport = transport

    def configured(self) -> bool:
        return True

    def probe(self) -> dict:
        return {"ok": self.configured(), "kind": self.kind}

    def documents(self, limit: int = 50) -> list[dict]:
        raise NotImplementedError

    def search(self, query: str, limit: int = 10) -> list[dict]:
        query_lower = (query or "").lower()
        found = []
        for doc in self.documents(limit=MAX_DOCS):
            haystack = f"{doc.get('title', '')}\n{doc.get('text', '')}".lower()
            if not query_lower or query_lower in haystack:
                found.append(doc)
            if len(found) >= limit:
                break
        return found

    def read(self, external_id: str) -> dict:
        for doc in self.documents(limit=MAX_DOCS):
            if doc["external_id"] == external_id:
                return doc
        raise NotFound(f"{self.kind} document {external_id} not found")

    def _client(self, base: str) -> httpx.Client:
        return httpx.Client(base_url=base, transport=self.transport, timeout=30.0,
                            headers=self.headers())

    def headers(self) -> dict:
        return {"accept": "application/json"}

    def _get(self, url: str, **kwargs) -> dict:
        try:
            with self._client(url) as client:
                response = client.get("")
        except httpx.HTTPError as exc:  # pragma: no cover - network
            raise ProviderFailure(f"{self.kind} unreachable: {exc}") from exc
        if response.status_code >= 400:
            raise ProviderFailure(f"{self.kind} returned {response.status_code}",
                                  status=response.status_code, body=response.text[:500])
        return response.json() if response.content else {}


def _flatten_rich_text(blocks: list[dict]) -> str:
    out: list[str] = []
    for block in blocks or []:
        for key in ("paragraph", "heading_1", "heading_2", "heading_3", "bulleted_list_item",
                    "numbered_list_item", "quote", "callout", "code", "to_do"):
            node = block.get(key)
            if isinstance(node, dict):
                text = "".join(part.get("plain_text", "")
                               for part in node.get("rich_text") or [])
                if text:
                    out.append(text)
        if block.get("object") == "child_page":
            out.append(str(block.get("child_page", {}).get("title", "")))
    return "\n".join(out).strip()


class NotionAdapter(Adapter):
    """notion search plus a page's blocks. the two calls a knowledge sync needs."""

    kind = "notion"
    BASE = "https://api.notion.com/v1"

    def configured(self) -> bool:
        return bool(self.token)

    def headers(self) -> dict:
        return {
            "accept": "application/json",
            "authorization": f"Bearer {self.token}",
            "notion-version": self.config.get("notion_version", "2022-06-28"),
            "content-type": "application/json",
        }

    def probe(self) -> dict:
        if not self.configured():
            return {"ok": False, "kind": self.kind, "needs": "a notion integration token"}
        payload = self._post("/search", {"page_size": 1})
        return {"ok": True, "kind": self.kind, "results": len(payload.get("results") or [])}

    def _post(self, path: str, payload: dict) -> dict:
        try:
            with self._client(self.BASE) as client:
                response = client.post(path, json=payload)
        except httpx.HTTPError as exc:  # pragma: no cover - network
            raise ProviderFailure(f"notion unreachable: {exc}") from exc
        if response.status_code >= 400:
            raise ProviderFailure(f"notion returned {response.status_code}",
                                  status=response.status_code, body=response.text[:500])
        return response.json()

    def documents(self, limit: int = 50) -> list[dict]:
        if not self.configured():
            raise ProviderNotConfigured("notion connector has no token; set the env var it names")
        payload = self._post("/search", {
            "page_size": min(limit, 100),
            "filter": {"value": "page", "property": "object"},
            "sort": {"direction": "descending", "timestamp": "last_edited_time"},
        })
        docs = []
        for page in payload.get("results") or []:
            title = ""
            for prop in (page.get("properties") or {}).values():
                if prop.get("type") == "title":
                    title = "".join(t.get("plain_text", "") for t in prop.get("title") or [])
                    break
            body = ""
            try:
                body = _flatten_rich_text(self._children(page["id"]))
            except ProviderFailure:
                body = ""
            docs.append({
                "external_id": page["id"],
                "title": title or "untitled",
                "url": page.get("url", ""),
                "text": body,
                "updated_at": page.get("last_edited_time", ""),
            })
        return docs

    def _children(self, page_id: str) -> list[dict]:
        payload = self._get(f"{self.BASE}/blocks/{page_id}/children?page_size=100")
        return payload.get("results") or []


class DriveAdapter(Adapter):
    """google drive. list files, export the text of the ones that have any."""

    kind = "google_drive"
    BASE = "https://www.googleapis.com/drive/v3"

    def configured(self) -> bool:
        return bool(self.token)

    def headers(self) -> dict:
        return {"accept": "application/json", "authorization": f"Bearer {self.token}"}

    def probe(self) -> dict:
        if not self.configured():
            return {"ok": False, "kind": self.kind, "needs": "a drive access token"}
        payload = self._get(self.BASE + "/files?pageSize=1")
        return {"ok": True, "kind": self.kind, "files": len(payload.get("files") or [])}

    def documents(self, limit: int = 50) -> list[dict]:
        if not self.configured():
            raise ProviderNotConfigured("drive connector has no token; set the env var it names")
        query = self.config.get("query", "trashed=false")
        url = (f"{self.BASE}/files?pageSize={min(limit, 100)}&q={query}"
               "&fields=files(id,name,mimeType,modifiedTime,webViewLink)")
        payload = self._get(url)
        docs = []
        for item in payload.get("files") or []:
            text = ""
            try:
                text = self._export(item["id"], item.get("mimeType", ""))
            except ProviderFailure:
                text = ""
            docs.append({
                "external_id": item["id"],
                "title": item.get("name", "untitled"),
                "url": item.get("webViewLink", ""),
                "text": text,
                "updated_at": item.get("modifiedTime", ""),
            })
        return docs

    def _export(self, file_id: str, mime: str) -> str:
        if mime.startswith("application/vnd.google-apps"):
            payload = self._get(f"{self.BASE}/files/{file_id}/export?mimeType=text/plain")
            return str(payload.get("raw") or payload.get("text") or "")
        payload = self._get(f"{self.BASE}/files/{file_id}?alt=media")
        return str(payload.get("raw") or payload.get("text") or "")


class FolderAdapter(Adapter):
    """a directory of markdown or text. the boring one that always works."""

    kind = "folder"

    def configured(self) -> bool:
        return bool(self.config.get("path")) and Path(str(self.config["path"])).is_dir()

    def probe(self) -> dict:
        path = Path(str(self.config.get("path", "")))
        return {"ok": self.configured(), "kind": self.kind, "path": str(path),
                "exists": path.is_dir()}

    def documents(self, limit: int = 50) -> list[dict]:
        if not self.configured():
            raise ProviderNotConfigured("folder connector path does not exist",
                                        path=str(self.config.get("path", "")))
        root = Path(str(self.config["path"]))
        docs = []
        for path in sorted(root.rglob("*")):
            if len(docs) >= limit:
                break
            if not path.is_file() or path.suffix.lower() not in {".md", ".txt", ".markdown"}:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            docs.append({
                "external_id": str(path.relative_to(root)),
                "title": path.stem,
                "url": f"file://{path}",
                "text": text,
                "updated_at": path.stat().st_mtime,
            })
        return docs


class HttpAdapter(Adapter):
    """anything that can answer with a list of documents.

    the escape hatch: a wiki, a ticket tracker, an internal endpoint. it is not a
    provider integration, it is a shape, and naming the shape is what keeps the
    count of adapters small.
    """

    kind = "http"

    def configured(self) -> bool:
        return bool(self.config.get("url"))

    def probe(self) -> dict:
        if not self.configured():
            return {"ok": False, "kind": self.kind, "needs": "a url in config"}
        payload = self._get(str(self.config["url"]))
        return {"ok": True, "kind": self.kind, "documents": len(payload.get("documents") or [])}

    def headers(self) -> dict:
        headers = {"accept": "application/json"}
        if self.token:
            headers["authorization"] = f"Bearer {self.token}"
        return headers

    def documents(self, limit: int = 50) -> list[dict]:
        payload = self._get(str(self.config.get("url", "")))
        docs = []
        for item in (payload.get("documents") or [])[:limit]:
            docs.append({
                "external_id": str(item.get("id") or item.get("external_id") or ""),
                "title": item.get("title", "untitled"),
                "url": item.get("url", ""),
                "text": item.get("text", ""),
                "updated_at": item.get("updated_at", now()),
            })
        return docs


ADAPTERS: dict[str, type[Adapter]] = {
    "notion": NotionAdapter,
    "google_drive": DriveAdapter,
    "folder": FolderAdapter,
    "http": HttpAdapter,
}


def build_adapter(kind: str, config: dict | None, token: str = "",
                  transport: httpx.BaseTransport | None = None) -> Adapter:
    if kind not in ADAPTERS:
        raise ValidationError(f"unknown connector kind '{kind}'", allowed=sorted(ADAPTERS))
    return ADAPTERS[kind](config=config, token=token, transport=transport)


# ---- service -----------------------------------------------------------
class ConnectorService:
    def __init__(self, store: Store, crews, profile, audit=None, transport=None,
                 env=None, events_hook=None):
        self.store = store
        self.crews = crews
        self.profile = profile
        self.audit = audit
        self.transport = transport
        self.env = env if env is not None else os.environ
        self.events_hook = events_hook
        self.departments = None  # injected; supervisor checks need the org chart

    # ---- rows ----------------------------------------------------------
    def _connector(self, row) -> dict:
        return {
            "id": row["id"],
            "team_id": row["team_id"],
            "kind": row["kind"],
            "name": row["name"],
            "config": loads(row["config"], {}),
            "secret_env": row["secret_env"],
            "status": row["status"],
            "last_sync": row["last_sync"],
            "doc_count": row["doc_count"],
            "created_date": row["created_date"],
        }

    def get(self, connector_id: str) -> dict:
        row = self.store.one("select * from connectors where id = ?", (connector_id,))
        if not row:
            raise NotFound(f"connector {connector_id} not found")
        return self._connector(row)

    def list(self, team_id: str) -> list[dict]:
        rows = self.store.query(
            "select * from connectors where team_id = ? order by created_date", (team_id,))
        return [self._connector(r) for r in rows]

    def connect(self, team_id: str, kind: str, name: str, config: dict | None = None,
                secret_env: str = "", actor: str = "owner") -> dict:
        """register a workspace tool. the token lives in the environment, and the
        row only names it, because a config table is where credentials go to leak."""
        if not self.store.one("select id from teams where id = ?", (team_id,)):
            raise NotFound(f"crew {team_id} not found")
        if not name:
            raise ValidationError("connector needs a name")
        adapter = build_adapter(kind, config, token=self._token(secret_env), transport=self.transport)
        status = "connected" if adapter.configured() else "unverified"
        connector_id = new_id("con")
        with self.store.tx() as c:
            c.execute(
                """insert into connectors (id, team_id, kind, name, config, secret_env, status, created_date)
                   values (?,?,?,?,?,?,?,?)""",
                (connector_id, team_id, kind, name, dumps(config or {}), secret_env, status, now()),
            )
        connector = self.get(connector_id)
        self._emit("connector.connected", {"team_id": team_id, "connector": connector,
                                           "actor": actor})
        return connector

    def disconnect(self, connector_id: str, actor: str = "owner") -> dict:
        connector = self.get(connector_id)
        with self.store.tx() as c:
            c.execute("delete from connectors where id = ?", (connector_id,))
        self._emit("connector.disconnected", {"team_id": connector["team_id"],
                                              "connector": connector, "actor": actor})
        return connector

    def _token(self, secret_env: str) -> str:
        return str(self.env.get(secret_env, "")) if secret_env else ""

    def adapter(self, connector: dict) -> Adapter:
        return build_adapter(connector["kind"], connector["config"],
                             token=self._token(connector["secret_env"]),
                             transport=self.transport)

    def probe(self, connector_id: str) -> dict:
        connector = self.get(connector_id)
        return self.adapter(connector).probe()

    # ---- sync ----------------------------------------------------------
    def sync(self, connector_id: str, limit: int = 50) -> dict:
        connector = self.get(connector_id)
        adapter = self.adapter(connector)
        if not adapter.configured():
            raise ProviderNotConfigured(f"connector '{connector['name']}' is not configured",
                                        needs=connector["secret_env"] or "config")
        docs = adapter.documents(limit=min(limit, MAX_DOCS))
        added = updated = 0
        for doc in docs:
            checksum = hashlib.sha256(
                f"{doc.get('title', '')}\n{doc.get('text', '')}".encode()).hexdigest()[:32]
            existing = self.store.one(
                "select id, checksum from knowledge_docs where connector_id = ? and external_id = ?",
                (connector_id, doc["external_id"]))
            if existing:
                if existing["checksum"] == checksum:
                    with self.store.tx() as c:
                        c.execute("update knowledge_docs set fetched_at = ? where id = ?",
                                  (now(), existing["id"]))
                    continue
                with self.store.tx() as c:
                    c.execute(
                        """update knowledge_docs set title = ?, url = ?, text = ?, checksum = ?,
                               updated_at = ?, fetched_at = ? where id = ?""",
                        (doc.get("title", ""), doc.get("url", ""), doc.get("text", ""), checksum,
                         now(), now(), existing["id"]))
                updated += 1
            else:
                with self.store.tx() as c:
                    c.execute(
                        """insert into knowledge_docs
                           (id, team_id, connector_id, external_id, title, url, text, checksum,
                            updated_at, fetched_at)
                           values (?,?,?,?,?,?,?,?,?,?)""",
                        (new_id("doc"), connector["team_id"], connector_id, doc["external_id"],
                         doc.get("title", ""), doc.get("url", ""), doc.get("text", ""),
                         checksum, now(), now()))
                added += 1
        count = self.store.one(
            "select count(*) as n from knowledge_docs where connector_id = ?", (connector_id,))
        with self.store.tx() as c:
            c.execute("update connectors set last_sync = ?, status = 'connected', doc_count = ? "
                      "where id = ?", (now(), int(count["n"]) if count else 0, connector_id))
        result = {"connector": connector_id, "fetched": len(docs), "added": added,
                  "updated": updated, "total": int(count["n"]) if count else 0}
        self._emit("connector.synced", {"team_id": connector["team_id"],
                                        "connector_id": connector_id, **result})
        return result

    def docs(self, connector_id: str, limit: int = 50) -> list[dict]:
        rows = self.store.query(
            "select id, external_id, title, url, length(text) as chars, fetched_at "
            "from knowledge_docs where connector_id = ? order by title limit ?",
            (connector_id, limit))
        return [dict(r) for r in rows]

    # ---- access --------------------------------------------------------
    def grant(self, agent_id: str, connector_id: str, supervisor_id: str = "",
              expires_hours: float = 24.0, scopes: list[str] | None = None,
              actor: str = "owner") -> dict:
        """hand one agent access to one workspace, for a while.

        issued by the agent's department supervisor, which is the hmas rule: the
        node accountable for the work decides what the work can read. a human can
        still do it directly, because a supervisor can be wrong.
        """
        agent = self.crews.get_agent(agent_id)
        connector = self.get(connector_id)
        if connector["team_id"] != agent["team_id"]:
            raise NotAllowed("connector belongs to a different crew")
        if supervisor_id:
            self._assert_supervisor(supervisor_id, agent_id)
        expires_at = now() + float(expires_hours) * 3600 if expires_hours else None
        key = f"connectors:{connector_id}"
        entry = self.profile.set(
            agent_id,
            "perms",
            key,
            {"kind": "connectors", "resource": connector_id,
             "scopes": list(scopes or ["read"]),
             "granted_by": f"agent:{supervisor_id}" if supervisor_id else actor,
             "granted_date": now(), "expires_at": expires_at},
            origin="agent" if supervisor_id else "human",
        )
        self._record_grant(agent, "grant.added", connector_id, supervisor_id or actor,
                           {"expires_at": expires_at, "scopes": scopes or ["read"]})
        self._emit("connector.access_granted",
                   {"team_id": agent["team_id"], "agent_id": agent_id,
                    "connector_id": connector_id, "supervisor_id": supervisor_id})
        return {"agent_id": agent_id, "connector_id": connector_id, "entry": entry,
                "expires_at": expires_at}

    def revoke(self, agent_id: str, connector_id: str, supervisor_id: str = "",
               reason: str = "", actor: str = "owner") -> dict:
        agent = self.crews.get_agent(agent_id)
        if supervisor_id:
            self._assert_supervisor(supervisor_id, agent_id)
        key = f"connectors:{connector_id}"
        try:
            self.profile.delete(agent_id, "perms", key)
        except NotFound:
            return {"agent_id": agent_id, "connector_id": connector_id,
                    "revoked": False, "reason": "no such grant"}
        self._record_grant(agent, "grant.revoked", connector_id, supervisor_id or actor,
                           {"reason": reason})
        self._emit("connector.access_revoked",
                   {"team_id": agent["team_id"], "agent_id": agent_id,
                    "connector_id": connector_id, "supervisor_id": supervisor_id,
                    "reason": reason})
        return {"agent_id": agent_id, "connector_id": connector_id, "revoked": True}

    def allowed(self, agent_id: str, connector_id: str) -> bool:
        for grant in self.profile.grants(agent_id, active_only=True):
            if grant.get("kind") == "connectors" and grant.get("resource") == connector_id:
                return True
        return False

    def access(self, supervisor_id: str = "", team_id: str = "") -> dict:
        """who can read what, and who gave it to them. one query, no inference."""
        if supervisor_id:
            team_scope = self.crews.get_agent(supervisor_id)["team_id"]
            agent_ids = self._subordinates(supervisor_id)
        else:
            team_scope = team_id
            agent_ids = {r["id"] for r in self.store.query(
                "select id from agents where team_id = ?", (team_id,))} if team_id else set()
        grants = []
        for agent_id in sorted(agent_ids):
            for grant in self.profile.grants(agent_id, active_only=True):
                if grant.get("kind") != "connectors":
                    continue
                grants.append({
                    "agent_id": agent_id,
                    "connector_id": grant.get("resource"),
                    "granted_by": grant.get("granted_by", ""),
                    "expires_at": grant.get("expires_at"),
                    "scopes": grant.get("scopes", []),
                })
        connectors = self.list(team_scope) if team_scope else []
        return {"connectors": connectors, "grants": grants}

    # ---- reading -------------------------------------------------------
    def search(self, agent_id: str, query: str, limit: int = 10,
               connector_id: str | None = None) -> dict:
        """what the agent may know, and where each answer came from."""
        agent = self.crews.get_agent(agent_id)
        available = [c for c in self.list(agent["team_id"])
                     if connector_id is None or c["id"] == connector_id]
        permitted = [c for c in available if self.allowed(agent_id, c["id"])]
        denied = [c["id"] for c in available if c["id"] not in {p["id"] for p in permitted}]
        if connector_id and not permitted:
            raise NotAllowed(
                "this agent has no access to that workspace",
                ask="its department supervisor can grant it",
                connector_id=connector_id,
            )
        hits: list[dict] = []
        pattern = f"%{(query or '').lower()}%"
        for connector in permitted:
            rows = self.store.query(
                """select id, external_id, title, url, text, checksum, fetched_at
                   from knowledge_docs
                   where connector_id = ? and (lower(title) like ? or lower(text) like ?)
                   order by title limit ?""",
                (connector["id"], pattern, pattern, limit))
            for row in rows:
                hits.append({
                    "connector": connector["id"], "connector_name": connector["name"],
                    "external_id": row["external_id"], "title": row["title"],
                    "url": row["url"], "checksum": row["checksum"],
                    "excerpt": (row["text"] or "")[:400],
                })
        return {"agent_id": agent_id, "query": query, "hits": hits[:limit],
                "connectors": [c["name"] for c in permitted], "denied": denied}

    # ---- org checks ----------------------------------------------------
    def _assert_supervisor(self, supervisor_id: str, agent_id: str) -> None:
        supervisor = self.crews.get_agent(supervisor_id)
        if supervisor["team_id"] != self.crews.get_agent(agent_id)["team_id"]:
            raise NotAllowed("supervisor and agent are in different crews")
        if agent_id not in self._subordinates(supervisor_id):
            raise NotAllowed(
                "only the department supervisor can hand out workspace access",
                why="this agent does not report to that supervisor",
            )

    def _subordinates(self, supervisor_id: str) -> set[str]:
        """the supervisor's department and everything under it, by membership."""
        roots = self.store.query(
            "select id from departments where coordinator_agent_id = ?", (supervisor_id,))
        if not roots:
            raise NotAllowed("only a department supervisor can hand out workspace access",
                             why="this agent coordinates no department")
        departments: set[str] = set()
        frontier = [r["id"] for r in roots]
        while frontier:
            department_id = frontier.pop()
            if department_id in departments:
                continue
            departments.add(department_id)
            frontier.extend(r["id"] for r in self.store.query(
                "select id from departments where parent_id = ?", (department_id,)))
        agents: set[str] = {supervisor_id}
        for department_id in departments:
            for row in self.store.query(
                    "select agent_id from department_members where department_id = ?",
                    (department_id,)):
                agents.add(row["agent_id"])
        return agents

    # ---- internals -----------------------------------------------------
    def _record_grant(self, agent: dict, action: str, connector_id: str,
                      actor: str, payload: dict) -> None:
        if self.audit is None:
            return
        self.audit.record(
            team_id=agent["team_id"],
            agent_id=agent["id"],
            action=action,
            actor_kind="agent" if str(actor).startswith("agent_") else "human",
            actor_id=actor,
            target_kind="connector",
            target_id=connector_id,
            payload=payload,
        )

    def _emit(self, event: str, context: dict) -> None:
        if self.events_hook is not None:
            self.events_hook(event, context)
