"""a2a: the review gate is the point - unapproved messages must not reach agents."""

from __future__ import annotations

import unittest

from base import AppCase

from clawcrew.errors import NotAllowed, NotFound, ValidationError


class A2ATests(AppCase):
    def setup_pair(self, review_mode: str = "reviewed"):
        team = self.crews.create_team("ops", review_mode=review_mode)
        sender = self.agent(team["id"], "analyst", skills=["research"])
        receiver = self.agent(team["id"], "writer", skills=["writing"])
        return team, sender, receiver

    def test_reviewed_crew_holds_message_pending(self):
        team, sender, receiver = self.setup_pair()
        message = self.a2a.send(team["id"], sender["id"], "here are the numbers", to_agent_id=receiver["id"])
        self.assertEqual(message["status"], "pending")
        self.assertIsNone(message["reviewed_date"])

    def test_pending_message_is_invisible_to_recipient(self):
        team, sender, receiver = self.setup_pair()
        self.a2a.send(team["id"], sender["id"], "unvetted claim", to_agent_id=receiver["id"])
        self.assertEqual(self.a2a.inbox(receiver["id"]), [])

    def test_approved_message_reaches_recipient(self):
        team, sender, receiver = self.setup_pair()
        message = self.a2a.send(team["id"], sender["id"], "approved fact", to_agent_id=receiver["id"])
        self.a2a.review(message["id"], "approved", reviewer="filmosophy", note="verified")
        inbox = self.a2a.inbox(receiver["id"])
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0]["body"], "approved fact")
        self.assertEqual(inbox[0]["reviewed_by"], "filmosophy")

    def test_rejected_message_never_reaches_recipient(self):
        team, sender, receiver = self.setup_pair()
        message = self.a2a.send(team["id"], sender["id"], "hallucinated number", to_agent_id=receiver["id"])
        self.a2a.review(message["id"], "rejected", note="not supported by source")
        self.assertEqual(self.a2a.inbox(receiver["id"]), [])
        self.assertEqual(self.a2a.stats(team["id"])["rejected"], 1)

    def test_open_crew_delivers_immediately(self):
        team, sender, receiver = self.setup_pair("open")
        message = self.a2a.send(team["id"], sender["id"], "straight through", to_agent_id=receiver["id"])
        self.assertEqual(message["status"], "approved")
        self.assertEqual(len(self.a2a.inbox(receiver["id"])), 1)

    def test_broadcast_reaches_every_agent_but_sender(self):
        team, sender, receiver = self.setup_pair("open")
        self.a2a.send(team["id"], sender["id"], "all hands")
        self.assertEqual(len(self.a2a.inbox(receiver["id"])), 1)
        self.assertEqual(self.a2a.inbox(sender["id"]), [])
        self.assertEqual(len(self.a2a.inbox(sender["id"], include_sent=True)), 1)

    def test_double_review_is_refused(self):
        team, sender, receiver = self.setup_pair()
        message = self.a2a.send(team["id"], sender["id"], "once", to_agent_id=receiver["id"])
        self.a2a.review(message["id"], "approved")
        with self.assertRaises(NotAllowed):
            self.a2a.review(message["id"], "rejected")

    def test_bad_decision_rejected(self):
        team, sender, receiver = self.setup_pair()
        message = self.a2a.send(team["id"], sender["id"], "x", to_agent_id=receiver["id"])
        with self.assertRaises(ValidationError):
            self.a2a.review(message["id"], "maybe")

    def test_bad_kind_rejected(self):
        team, sender, _ = self.setup_pair()
        with self.assertRaises(ValidationError):
            self.a2a.send(team["id"], sender["id"], "x", kind="gossip")

    def test_empty_body_rejected(self):
        team, sender, _ = self.setup_pair()
        with self.assertRaises(ValidationError):
            self.a2a.send(team["id"], sender["id"], "   ")

    def test_cross_crew_message_refused(self):
        team, sender, _ = self.setup_pair()
        other = self.crews.create_team("other")
        outsider = self.agent(other["id"], "intruder")
        with self.assertRaises(NotAllowed):
            self.a2a.send(team["id"], sender["id"], "leak", to_agent_id=outsider["id"])

    def test_unknown_agent_raises(self):
        team, _, receiver = self.setup_pair()
        with self.assertRaises(NotFound):
            self.a2a.send(team["id"], "agent_ghost", "x", to_agent_id=receiver["id"])

    def test_thread_groups_messages_in_order(self):
        team, sender, receiver = self.setup_pair("open")
        first = self.a2a.send(team["id"], sender["id"], "start", to_agent_id=receiver["id"])
        self.a2a.send(team["id"], receiver["id"], "reply", to_agent_id=sender["id"], thread_id=first["thread_id"])
        thread = self.a2a.thread(first["thread_id"])
        self.assertEqual([m["body"] for m in thread], ["start", "reply"])

    def test_review_queue_lists_only_pending(self):
        team, sender, receiver = self.setup_pair()
        kept = self.a2a.send(team["id"], sender["id"], "keep", to_agent_id=receiver["id"])
        cleared = self.a2a.send(team["id"], sender["id"], "clear", to_agent_id=receiver["id"])
        self.a2a.review(cleared["id"], "approved")
        queue = self.a2a.review_queue(team["id"])
        self.assertEqual([m["id"] for m in queue], [kept["id"]])

    def test_log_filters_by_status(self):
        team, sender, receiver = self.setup_pair()
        self.a2a.send(team["id"], sender["id"], "one", to_agent_id=receiver["id"])
        self.assertEqual(len(self.a2a.log(team["id"], "pending")), 1)
        self.assertEqual(len(self.a2a.log(team["id"], "approved")), 0)


if __name__ == "__main__":
    unittest.main()
