"""the seam between the app's conversation and the signed log.

the app already had a conversation per channel, written by `ChannelService.post`.
that stays the place the board and the ui read from. this gateway makes it a view
of the log rather than a second truth:

    channel message -> signed event (unless it came from the log)
    signed event    -> channel message (marked as such, so it is not re-signed)

the marker is a column, not a flag in memory, because a re-delivery after a
restart must not double-post. `via` says which side wrote the row.
"""

from __future__ import annotations

from core.errors import CritterTownError, NotAllowed
from comms import events as spec


class CommsGateway:
    def __init__(self, comms, channels):
        self.comms = comms
        self.channels = channels
        self._installed = False

    def install(self) -> "CommsGateway":
        """one hook each way. installing twice is a no-op so `build_app` and a
        test that builds its own graph cannot end up with two hops."""
        if self._installed:
            return self
        self.comms.events_hook = self._on_comms_event
        previous = self.channels.events_hook

        def channel_hook(event: str, context: dict | None = None) -> None:
            try:
                self._on_channel_event(event, context)
            finally:
                if previous:
                    previous(event, context)

        self.channels.events_hook = channel_hook
        self._installed = True
        return self

    # ---- app -> log ------------------------------------------------------
    def _on_channel_event(self, name: str, context: dict | None) -> None:
        if name == "channel.created" and context:
            channel = context.get("channel") or {}
            if channel.get("id"):
                try:
                    self.comms.auto_bridge(channel["id"])
                except CritterTownError:
                    pass
            return
        if name != "channel.message" or not context:
            return
        message = context.get("message") or {}
        if message.get("via") == "comms":
            return
        if message.get("author_kind") != "agent" or not message.get("author_id"):
            return
        try:
            self._enrol(message["channel_id"], message["author_id"])
            tags = [["task", message["task_id"]]] if message.get("task_id") else []
            self.comms.publish(
                message["channel_id"],
                message.get("body") or "",
                kind=spec.ROOM_MESSAGE,
                author_id=message["author_id"],
                tags=tags,
                created_at=int(message["created_date"]),
                # it came from the channel: mirroring it back would be a loop
                mirror=False,
            )
        except CritterTownError:
            # a message that cannot be signed is still a message: the app has it,
            # and losing the conversation to protect the log would be the wrong
            # trade. the room's own `stats` shows the gap.
            return

    def _enrol(self, channel_id: str, agent_id: str) -> None:
        row = self.comms.store.one(
            "select member_id from comms_members where room_id = ? and member_id = ?",
            (channel_id, agent_id),
        )
        if row:
            return
        agent = self.comms.store.one("select role from agents where id = ?", (agent_id,))
        role = "member"
        if agent:
            members = self.channels.get(channel_id)["members"]
            role = next((m["role"] for m in members if m["agent_id"] == agent_id), "member")
        try:
            self.comms.add_member(channel_id, agent_id, "agent", role)
        except NotAllowed:
            return

    # ---- log -> app ------------------------------------------------------
    def _on_comms_event(self, name: str, context: dict | None) -> None:
        if name != "comms.event" or not context:
            return
        if not context.get("mirror", True):
            return
        event = context.get("event") or {}
        if event.get("kind") != spec.ROOM_MESSAGE or not event.get("content"):
            return
        try:
            self.channels.post(
                event["room_id"],
                event["content"],
                author_id=event.get("author_id") or "",
                author_kind=event.get("author_kind") or "external",
                via="comms",
            )
        except CritterTownError:
            return
