"""channels: a crew's rooms, each with its own board.

the shape here is deliberate and borrowed, not invented: a crew is the company, a
channel is a room (a team, a project, a standing conversation), members are agents
and people, and **every channel carries its own kanban board**. moving a card in
`#growth` must not touch `#support`, and the conversation in a room should be able
to point at the card it is about.

so a task has an optional `channel_id`, and a board is the same board code filtered
by it. no second board implementation, no per-channel copies of the schema.
"""

from __future__ import annotations

from typing import Any

from core.db import Store, dumps, loads, new_id, now
from core.errors import Conflict, NotFound, ValidationError

KINDS = {"channel", "dm", "dept"}
ROLES = {"lead", "member"}


def slugify(value: str) -> str:
    """channel slugs share the crew slug rules so urls read the same everywhere."""
    from agents.crews import slugify as crew_slugify

    return crew_slugify(value)


class ChannelService:
    def __init__(self, store: Store, kanban=None):
        self.store = store
        self.kanban = kanban
        self.events_hook = None

    # ---- channels ------------------------------------------------------
    def create(
        self,
        team_id: str,
        name: str,
        purpose: str = "",
        kind: str = "channel",
        members: list[str] | None = None,
        lead: str | None = None,
        slug: str | None = None,
    ) -> dict:
        if not name:
            raise ValidationError("channel name is required")
        if kind not in KINDS:
            raise ValidationError(f"channel kind must be one of {sorted(KINDS)}")
        if not self.store.one("select id from teams where id = ?", (team_id,)):
            raise NotFound(f"crew {team_id} not found")
        channel_slug = slugify(slug or name)
        if self.store.one(
            "select id from channels where team_id = ? and slug = ?", (team_id, channel_slug)
        ):
            raise Conflict(f"channel '{channel_slug}' already exists in {team_id}")
        channel_id = new_id("chan")
        ts = now()
        with self.store.tx() as c:
            c.execute(
                """insert into channels (id, team_id, name, slug, purpose, kind, created_date, updated_date)
                   values (?,?,?,?,?,?,?,?)""",
                (channel_id, team_id, name, channel_slug, purpose, kind, ts, ts),
            )
        for agent_id in members or []:
            self.join(channel_id, agent_id, role="lead" if agent_id == lead else "member")
        if lead and (not members or lead not in members):
            self.join(channel_id, lead, role="lead")
        channel = self.get(channel_id)
        if self.events_hook:
            # a new room is news: a configured relay wants it, before anyone speaks
            self.events_hook("channel.created", {"channel": channel, "team_id": team_id})
        return channel

    def get(self, channel_id: str) -> dict:
        row = self.store.one("select * from channels where id = ?", (channel_id,))
        if not row:
            raise NotFound(f"channel {channel_id} not found")
        return self._channel(row)

    def list(self, team_id: str) -> list[dict]:
        rows = self.store.query(
            "select * from channels where team_id = ? order by created_date", (team_id,)
        )
        return [self._channel(row) for row in rows]

    def update(self, channel_id: str, **fields) -> dict:
        allowed = {"name", "purpose", "kind"}
        patch = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if "kind" in patch and patch["kind"] not in KINDS:
            raise ValidationError(f"channel kind must be one of {sorted(KINDS)}")
        if not patch:
            return self.get(channel_id)
        sets = ", ".join(f"{k} = ?" for k in patch)
        with self.store.tx() as c:
            c.execute(
                f"update channels set {sets}, updated_date = ? where id = ?",
                (*patch.values(), now(), channel_id),
            )
        return self.get(channel_id)

    def _channel(self, row) -> dict:
        members = self.members(row["id"])
        return {
            "id": row["id"],
            "team_id": row["team_id"],
            "name": row["name"],
            "slug": row["slug"],
            "purpose": row["purpose"],
            "kind": row["kind"],
            "members": members,
            "lead_agent_id": next((m["agent_id"] for m in members if m["role"] == "lead"), None),
            "created_date": row["created_date"],
            "updated_date": row["updated_date"],
        }

    # ---- membership ----------------------------------------------------
    def join(self, channel_id: str, agent_id: str, role: str = "member") -> dict:
        if role not in ROLES:
            raise ValidationError(f"role must be one of {sorted(ROLES)}")
        self.get(channel_id)
        if not self.store.one("select id from agents where id = ?", (agent_id,)):
            raise NotFound(f"agent {agent_id} not found")
        with self.store.tx() as c:
            c.execute(
                """insert into channel_members (channel_id, agent_id, role, created_date)
                   values (?,?,?,?)
                   on conflict(channel_id, agent_id) do update set role = excluded.role""",
                (channel_id, agent_id, role, now()),
            )
        return self.get(channel_id)

    def leave(self, channel_id: str, agent_id: str) -> dict:
        with self.store.tx() as c:
            c.execute(
                "delete from channel_members where channel_id = ? and agent_id = ?",
                (channel_id, agent_id),
            )
        return self.get(channel_id)

    def members(self, channel_id: str) -> list[dict]:
        rows = self.store.query(
            """select m.agent_id, m.role, a.name, a.role as agent_role
               from channel_members m join agents a on a.id = m.agent_id
               where m.channel_id = ? order by m.role desc, a.name""",
            (channel_id,),
        )
        return [
            {"agent_id": r["agent_id"], "role": r["role"], "name": r["name"],
             "agent_role": r["agent_role"]}
            for r in rows
        ]

    def channels_for_agent(self, agent_id: str) -> list[dict]:
        rows = self.store.query(
            """select c.*, m.role from channels c join channel_members m on m.channel_id = c.id
               where m.agent_id = ? order by c.created_date""",
            (agent_id,),
        )
        return [{**self._channel(r), "my_role": r["role"]} for r in rows]

    # ---- conversation --------------------------------------------------
    def post(
        self,
        channel_id: str,
        body: str,
        author_id: str = "",
        author_kind: str = "agent",
        task_id: str | None = None,
        via: str = "app",
    ) -> dict:
        """a message in the room. `author_kind` is agent | human | system, because a
        board that cannot tell the owner from an agent is not auditable."""
        if not str(body or "").strip():
            raise ValidationError("message body is required")
        channel = self.get(channel_id)
        if task_id and not self.store.one("select id from tasks where id = ?", (task_id,)):
            raise NotFound(f"task {task_id} not found")
        message_id = new_id("msg")
        ts = now()
        with self.store.tx() as c:
            c.execute(
                """insert into channel_messages (id, channel_id, task_id, author_kind, author_id, body, created_date, via)
                   values (?,?,?,?,?,?,?,?)""",
                (message_id, channel_id, task_id, author_kind, author_id, body, ts, via),
            )
        message = self.get_message(message_id)
        if self.events_hook:
            self.events_hook(
                "channel.message",
                {"channel": channel, "message": message, "team_id": channel["team_id"]},
            )
        return message

    def get_message(self, message_id: str) -> dict:
        row = self.store.one("select * from channel_messages where id = ?", (message_id,))
        if not row:
            raise NotFound(f"message {message_id} not found")
        return {
            "id": row["id"],
            "channel_id": row["channel_id"],
            "task_id": row["task_id"],
            "author_kind": row["author_kind"],
            "author_id": row["author_id"],
            "body": row["body"],
            "via": row["via"] if "via" in row.keys() else "app",
            "created_date": row["created_date"],
        }

    def messages(self, channel_id: str, limit: int = 50, task_id: str | None = None) -> list[dict]:
        self.get(channel_id)
        if task_id:
            rows = self.store.query(
                """select * from channel_messages where channel_id = ? and task_id = ?
                   order by created_date desc limit ?""",
                (channel_id, task_id, limit),
            )
        else:
            rows = self.store.query(
                "select * from channel_messages where channel_id = ? order by created_date desc limit ?",
                (channel_id, limit),
            )
        return [self.get_message(r["id"]) for r in rows]

    # ---- the board -----------------------------------------------------
    def tasks(self, channel_id: str, column: str | None = None, limit: int = 200) -> list[dict]:
        channel = self.get(channel_id)
        return self.kanban.list_tasks(
            channel["team_id"], column=column, limit=limit, channel_id=channel_id
        )

    def board(self, channel_id: str, include_hidden: bool = True) -> dict:
        channel = self.get(channel_id)
        board = self.kanban.board(
            channel["team_id"], include_hidden=include_hidden, channel_id=channel_id
        )
        board["channel"] = {k: channel[k] for k in ("id", "name", "slug", "purpose", "kind")}
        board["members"] = channel["members"]
        return board

    def create_task(self, channel_id: str, title: str, **fields: Any) -> dict:
        """a card belongs to the room it was raised in."""
        channel = self.get(channel_id)
        fields.pop("channel_id", None)
        fields.pop("team_id", None)
        return self.kanban.create_task(
            team_id=channel["team_id"], title=title, channel_id=channel_id, **fields
        )

    def overview(self, team_id: str) -> dict:
        """every room, its people and how much work is in it. this is the left rail
        of the app: one row per channel, no dashboard needed."""
        channels = self.list(team_id)
        return {
            "team_id": team_id,
            "channels": [
                {
                    **{k: c[k] for k in ("id", "name", "slug", "purpose", "kind")},
                    "members": len(c["members"]),
                    "open_tasks": len(
                        [t for t in self.tasks(c["id"], limit=500)
                         if t["column"] not in {"done", "cancelled"}]
                    ),
                    "messages": int(
                        (self.store.one(
                            "select count(*) as c from channel_messages where channel_id = ?",
                            (c["id"],),
                        ) or {"c": 0})["c"]
                    ),
                }
                for c in channels
            ],
        }
