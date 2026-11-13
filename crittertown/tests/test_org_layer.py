"""channels, departments and point of view: the shape of the workspace.

these are the three ideas the product is built on, so they are tested as behaviour,
not as rows: a room has its own board, a department has one accountable head, and a
pov is a projection that tells the truth about what one agent can see.
"""

from __future__ import annotations

import time
import unittest

import base  # noqa: F401
from base import AppCase

from core.errors import ValidationError


class ChannelTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("shop", mission="sell things")
        self.wren = self.agent(self.team["id"], "wren", role="writer")
        self.okafor = self.agent(self.team["id"], "okafor", role="research")

    def channel(self, name: str = "growth", **kwargs):
        return self.app.channels.create(self.team["id"], name, **kwargs)

    def test_a_room_keeps_its_own_board(self):
        growth = self.channel("growth")
        support = self.channel("support")
        card = self.app.channels.create_task(growth["id"], "write the launch post")
        self.assertEqual(card["channel_id"], growth["id"])
        self.assertEqual(len(self.app.channels.tasks(growth["id"])), 1)
        self.assertEqual(len(self.app.channels.tasks(support["id"])), 0)
        # the crew board still sees everything: channels slice, they do not hide
        self.assertEqual(len(self.kanban.list_tasks(self.team["id"])), 1)

    def test_card_inherits_the_crew_board_columns(self):
        growth = self.channel("growth")
        card = self.app.channels.create_task(growth["id"], "draft", column="ready")
        board = self.app.channels.board(growth["id"])
        names = [c["name"] for c in board["columns"]]
        self.assertIn("ready", names)
        ready = next(c for c in board["columns"] if c["name"] == "ready")
        self.assertEqual([t["id"] for t in ready["tasks"]], [card["id"]])
        self.assertEqual(board["channel"]["slug"], "growth")

    def test_membership_with_a_lead(self):
        growth = self.channel("growth", members=[self.wren["id"]], lead=self.wren["id"])
        self.assertEqual(growth["lead_agent_id"], self.wren["id"])
        self.assertEqual([m["agent_id"] for m in growth["members"]], [self.wren["id"]])
        joined = self.app.channels.join(growth["id"], self.okafor["id"])
        self.assertEqual(len(joined["members"]), 2)
        left = self.app.channels.leave(growth["id"], self.okafor["id"])
        self.assertEqual(len(left["members"]), 1)

    def test_a_room_slug_is_unique_per_crew(self):
        self.channel("growth")
        with self.assertRaises(Exception):
            self.channel("Growth")

    def test_messages_carry_their_author_and_their_card(self):
        growth = self.channel("growth")
        card = self.app.channels.create_task(growth["id"], "draft")
        self.app.channels.post(
            growth["id"], "picked the angle", author_id=self.wren["id"], task_id=card["id"]
        )
        self.app.channels.post(growth["id"], "approved", author_kind="human", author_id="owner")
        messages = self.app.channels.messages(growth["id"])
        self.assertEqual(len(messages), 2)
        kinds = {m["author_kind"] for m in messages}
        self.assertEqual(kinds, {"agent", "human"})
        about = self.app.channels.messages(growth["id"], task_id=card["id"])
        self.assertEqual(len(about), 1)
        self.assertEqual(about[0]["body"], "picked the angle")

    def test_empty_message_is_rejected(self):
        growth = self.channel("growth")
        with self.assertRaises(ValidationError):
            self.app.channels.post(growth["id"], "   ")

    def test_overview_counts_open_work_per_room(self):
        growth = self.channel("growth")
        self.channel("support")
        self.app.channels.create_task(growth["id"], "one")
        finished = self.app.channels.create_task(growth["id"], "two", column="in_progress")
        self.kanban.approve(finished["id"], "owner")
        self.app.channels.post(growth["id"], "hello")
        overview = self.app.channels.overview(self.team["id"])
        rows = {row["slug"]: row for row in overview["channels"]}
        self.assertEqual(rows["growth"]["open_tasks"], 1)
        self.assertEqual(rows["growth"]["messages"], 1)
        self.assertEqual(rows["support"]["open_tasks"], 0)


class DepartmentTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("shop")
        self.head = self.agent(self.team["id"], "head", role="operator")
        self.writer = self.agent(self.team["id"], "writer", role="writer")
        self.sub_head = self.agent(self.team["id"], "sub", role="editor")

    def test_coordinator_is_a_member_by_construction(self):
        dept = self.app.departments.create(
            self.team["id"], "Editorial", coordinator_agent_id=self.head["id"]
        )
        self.assertEqual(dept["coordinator_agent_id"], self.head["id"])
        self.assertIn(self.head["id"], [m["agent_id"] for m in dept["members"]])

    def test_routing_lands_on_the_coordinator(self):
        dept = self.app.departments.create(
            self.team["id"], "Editorial", coordinator_agent_id=self.head["id"],
            members=[self.writer["id"]],
        )
        card = self.task(self.team["id"], "write the brief")
        routed = self.app.departments.route(card["id"], dept["id"])
        self.assertEqual(routed["task"]["assignee_agent_id"], self.head["id"])
        self.assertEqual(routed["coordinator_agent_id"], self.head["id"])

    def test_routing_without_a_department_uses_the_assignees_department(self):
        self.app.departments.create(
            self.team["id"], "Editorial", coordinator_agent_id=self.head["id"],
            members=[self.writer["id"]],
        )
        card = self.task(self.team["id"], "edit this", assignee_agent_id=self.writer["id"])
        routed = self.app.departments.route(card["id"])
        self.assertEqual(routed["task"]["assignee_agent_id"], self.head["id"])

    def test_routing_a_stray_card_is_an_error_not_a_guess(self):
        self.app.departments.create(
            self.team["id"], "Editorial", coordinator_agent_id=self.head["id"]
        )
        card = self.task(self.team["id"], "nobody's")
        with self.assertRaises(ValidationError):
            self.app.departments.route(card["id"])

    def test_departments_nest_and_the_chart_renders(self):
        editorial = self.app.departments.create(
            self.team["id"], "Editorial", coordinator_agent_id=self.head["id"]
        )
        desk = self.app.departments.create(
            self.team["id"], "Copy desk", coordinator_agent_id=self.sub_head["id"],
            parent_id=editorial["id"], members=[self.writer["id"]],
        )
        tree = self.app.departments.tree(self.team["id"])
        self.assertEqual([d["slug"] for d in tree["roots"]], ["editorial"])
        self.assertEqual([d["slug"] for d in tree["roots"][0]["children"]], ["copy-desk"])
        chain = self.app.departments.chain(self.writer["id"])
        self.assertEqual([d["id"] for d in chain], [desk["id"], editorial["id"]])

    def test_a_reporting_cycle_is_refused(self):
        top = self.app.departments.create(
            self.team["id"], "Top", coordinator_agent_id=self.head["id"]
        )
        mid = self.app.departments.create(
            self.team["id"], "Mid", coordinator_agent_id=self.sub_head["id"],
        )
        self.app.departments.update(mid["id"], parent_id=top["id"])
        with self.assertRaises(ValidationError):
            self.app.departments.update(top["id"], parent_id=mid["id"])

    def test_escalation_goes_up_the_line_and_through_the_gate(self):
        editorial = self.app.departments.create(
            self.team["id"], "Editorial", coordinator_agent_id=self.head["id"],
            members=[self.writer["id"]],
        )
        escalated = self.app.departments.escalate(
            self.writer["id"], "need a decision on the headline"
        )
        message = escalated["message"]
        self.assertEqual(message["to_agent_id"], self.head["id"])
        # the crew is reviewed, so nothing reaches the head until reviewed
        self.assertEqual(self.app.a2a.inbox(self.head["id"]), [])
        self.app.a2a.review(message["id"], "approved", reviewer="owner")
        self.assertEqual(len(self.app.a2a.inbox(self.head["id"])), 1)

    def test_unassigned_agents_are_listed_so_nobody_is_invisible(self):
        self.app.departments.create(
            self.team["id"], "Editorial", coordinator_agent_id=self.head["id"]
        )
        unassigned = [row["name"] for row in self.app.departments.unassigned_agents(self.team["id"])]
        self.assertIn("writer", unassigned)
        self.assertNotIn("head", unassigned)


class PovTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("shop")
        self.head = self.agent(self.team["id"], "head", role="operator")
        self.writer = self.agent(self.team["id"], "writer", role="writer")
        self.channel = self.app.channels.create(
            self.team["id"], "growth", members=[self.writer["id"]], lead=self.writer["id"]
        )
        self.department = self.app.departments.create(
            self.team["id"], "Editorial", coordinator_agent_id=self.head["id"],
            members=[self.writer["id"]],
        )

    def test_pov_shows_only_their_own_cards(self):
        mine = self.app.channels.create_task(
            self.channel["id"], "my draft", assignee_agent_id=self.writer["id"]
        )
        self.app.channels.create_task(
            self.channel["id"], "their draft", assignee_agent_id=self.head["id"]
        )
        view = self.app.pov.view(self.writer["id"])
        seen = [t["id"] for column in view["board"]["columns"] for t in column["tasks"]]
        self.assertEqual(seen, [mine["id"]])
        self.assertEqual(view["board"]["open"], 1)

    def test_pov_carries_rooms_reporting_line_and_crew(self):
        view = self.app.pov.view(self.writer["id"])
        self.assertEqual([c["slug"] for c in view["channels"]], ["growth"])
        self.assertEqual(view["channels"][0]["my_role"], "lead")
        self.assertEqual([d["slug"] for d in view["reporting_line"]], ["editorial"])
        self.assertEqual(view["crew"]["name"], "shop")
        self.assertEqual(view["pov"], "agent")

    def test_calendar_merges_blocks_and_card_due_dates(self):
        due = time.time() + 3600
        self.app.channels.create_task(
            self.channel["id"], "ship the draft", assignee_agent_id=self.writer["id"],
            payload={"due_at": due},
        )
        self.app.pov.add_event(
            self.writer["id"], "standup", starts_at=time.time() + 600, kind="meeting"
        )
        calendar = self.app.pov.calendar(self.writer["id"])
        kinds = [entry["kind"] for entry in calendar["entries"]]
        self.assertIn("meeting", kinds)
        self.assertIn("task_due", kinds)
        self.assertEqual(calendar["entries"], sorted(calendar["entries"], key=lambda e: e["starts_at"]))

    def test_calendar_accepts_iso_and_rejects_nonsense(self):
        iso = "2030-01-02T09:30:00Z"
        event = self.app.pov.add_event(self.writer["id"], "planning", starts_at=iso)
        self.assertAlmostEqual(event["starts_at"], 1893576600.0, delta=1)
        with self.assertRaises(ValidationError):
            self.app.pov.add_event(self.writer["id"], "nope", starts_at="next tuesday-ish")

    def test_inbox_counts_what_is_still_behind_the_gate(self):
        self.app.a2a.send(
            team_id=self.team["id"], from_agent_id=self.head["id"],
            to_agent_id=self.writer["id"], body="here is the brief",
        )
        view = self.app.pov.view(self.writer["id"])
        self.assertEqual(view["inbox"]["approved"], [])
        self.assertEqual(view["inbox"]["pending"], 1)

    def test_roster_is_one_query_for_the_switcher(self):
        roster = {row["name"]: row for row in self.app.pov.roster(self.team["id"])}
        self.assertEqual(set(roster), {"head", "writer"})
        self.assertEqual(roster["writer"]["department"], "editorial")

    def test_deleting_someone_elses_event_is_not_found(self):
        event = self.app.pov.add_event(
            self.writer["id"], "focus block", starts_at=time.time() + 60
        )
        with self.assertRaises(Exception):
            self.app.pov.delete_event(event["id"], agent_id=self.head["id"])


if __name__ == "__main__":
    unittest.main()
