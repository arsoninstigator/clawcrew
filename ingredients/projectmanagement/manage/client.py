"""manage, self-hosted: the board the agents run themselves.

a crew already has a local kanban for its own lanes. manage is the other half:
projects that outlive one crew, where agents are members with a role and an
assignee slot, and where the unit of delegation is an issue instead of a prompt.

clawcrew agents pull from it the way an external runner does, so the agent never
has to know what woke it up (a delegation, an @mention, a status trigger, a
schedule, a manual run) -- the instance decides, the run arrives with the issue
attached:

    POST /agent-runs/claim                 -> {run} or nothing
    POST /agent-runs/{runId}/heartbeat     -> keeps the lease while it works
    POST /agent-runs/{runId}/events        -> what it is doing, live
    POST /agent-runs/{runId}/result        -> the answer, and the run closes

and the issue routes an agent needs when it wants to look wider than its own run:
`/projects/{projectKey}/issues`, `/projects/{projectKey}/issues/board`,
`/issues/{issueId}`, `/issues/search`.

the paths above are the routes in `apps/api/src/modules/agents/runner` and
`apps/api/src/modules/issues` of croffasia/manage, read from the source, not
guessed. nothing of theirs is vendored: this talks to an instance you run.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from core.errors import ProviderFailure, ProviderNotConfigured

DEFAULT_BASE = "http://localhost:4000"


def _root(base_url: str) -> str:
    return (base_url or DEFAULT_BASE).strip().rstrip("/")


@dataclass
class ManageClient:
    base_url: str = DEFAULT_BASE
    api_key: str = ""
    timeout: float = 60.0
    transport: httpx.BaseTransport | None = None

    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    def status(self) -> dict:
        return {
            "kind": "manage",
            "self_hosted": True,
            "base_url": _root(self.base_url),
            "configured": self.configured(),
            "needs": "CLAWCREW_MANAGE_API_KEY (an external agent's api key, from the agent's page)",
        }

    def _client(self) -> httpx.Client:
        if not self.configured():
            raise ProviderNotConfigured(
                "no manage agent key",
                provider="manage",
                hint="create the agent in manage, copy its api key, set CLAWCREW_MANAGE_BASE_URL and CLAWCREW_MANAGE_API_KEY",
            )
        return httpx.Client(
            base_url=_root(self.base_url),
            headers={"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"},
            timeout=self.timeout,
            transport=self.transport,
        )

    def _call(self, method: str, path: str, what: str, **kwargs) -> httpx.Response:
        with self._client() as client:
            try:
                response = client.request(method, path, **kwargs)
            except httpx.HTTPError as exc:
                raise ProviderFailure(f"{what} failed: {exc}", provider="manage") from exc
        if response.status_code in (404, 405):
            raise ProviderFailure(
                f"{what} is not on this instance ({response.status_code}): check the manage version",
                provider="manage",
            )
        if response.status_code >= 400:
            raise ProviderFailure(
                f"{what} failed ({response.status_code}): {response.text[:300]}",
                provider="manage",
            )
        return response

    def _json(self, method: str, path: str, what: str, **kwargs) -> dict:
        response = self._call(method, path, what, **kwargs)
        return response.json() if response.content else {}

    # -- the run lane ------------------------------------------------------
    def claim(self) -> dict | None:
        """one run, or none. a 204/409 means the queue is empty for this agent.

        the claim is atomic on their side, so two workers of the same agent cannot
        take the same issue and a 409 is never worth retrying.
        """
        with self._client() as client:
            try:
                response = client.post("/agent-runs/claim")
            except httpx.HTTPError as exc:
                raise ProviderFailure(f"claiming a run failed: {exc}", provider="manage") from exc
        if response.status_code in (204, 409):
            return None
        if response.status_code in (404, 405):
            raise ProviderFailure(
                f"claiming a run is not on this instance ({response.status_code}): check the manage version",
                provider="manage",
            )
        if response.status_code >= 400:
            raise ProviderFailure(
                f"claiming a run failed ({response.status_code}): {response.text[:300]}",
                provider="manage",
            )
        payload = response.json() if response.content else {}
        run = payload.get("run") if isinstance(payload, dict) else None
        return run or None

    def heartbeat(self, run_id: int | str, note: str = "") -> dict:
        body = {"note": note} if note else {}
        return self._json("POST", f"/agent-runs/{run_id}/heartbeat",
                          "heartbeating a run", json=body)

    def events(self, run_id: int | str, events: list[dict]) -> dict:
        return self._json("POST", f"/agent-runs/{run_id}/events",
                          "posting run events", json={"events": events})

    def result(self, run_id: int | str, outcome: str, summary: str = "",
               details: dict | None = None) -> dict:
        body: dict = {"status": outcome}
        if summary:
            body["summary"] = summary
        if details:
            body["details"] = details
        return self._json("POST", f"/agent-runs/{run_id}/result",
                          "posting a run result", json=body)

    def queue(self) -> list[dict]:
        payload = self._json("GET", "/agent-runs/queue", "reading the run queue")
        runs = payload.get("runs") or payload.get("queue") or []
        return runs if isinstance(runs, list) else []

    # -- the board ---------------------------------------------------------
    def issues(self, project_key: str) -> list[dict]:
        payload = self._json("GET", f"/projects/{project_key}/issues",
                             f"listing issues in '{project_key}'")
        items = payload.get("issues") or payload.get("items") or payload
        return items if isinstance(items, list) else []

    def board(self, project_key: str) -> dict:
        return self._json("GET", f"/projects/{project_key}/issues/board",
                          f"reading the board for '{project_key}'")

    def issue(self, issue_id: int | str) -> dict:
        return self._json("GET", f"/issues/{issue_id}", f"reading issue {issue_id}")

    def create_issue(self, project_key: str, title: str, description: str = "",
                     assignee_id: int | str | None = None, **fields) -> dict:
        body = {"title": title, **fields}
        if description:
            body["description"] = description
        if assignee_id is not None:
            body["assigneeId"] = assignee_id
        return self._json("POST", f"/projects/{project_key}/issues",
                          f"creating an issue in '{project_key}'", json=body)

    def update_issue(self, issue_id: int | str, **fields) -> dict:
        return self._json("PATCH", f"/issues/{issue_id}",
                          f"updating issue {issue_id}", json=fields)

    def search(self, query: str) -> list[dict]:
        payload = self._json("GET", "/issues/search", "searching issues", params={"q": query})
        items = payload.get("issues") or payload.get("items") or payload
        return items if isinstance(items, list) else []


def make(base_url: str = DEFAULT_BASE, api_key: str = "",
         transport: httpx.BaseTransport | None = None) -> ManageClient:
    return ManageClient(base_url=base_url, api_key=api_key, transport=transport)
