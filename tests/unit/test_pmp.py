import unittest

from pydantic import ValidationError

from common.models.errors import PMPValidationError
from common.models.pmp import MessageType, PMPMessage
from common.validation import PMPValidator


class PMPMessageTests(unittest.TestCase):
    def make_message(self, **overrides):
        data = {
            "sender_agent_id": "producer.manager",
            "receiver_agent_id": "producer.topic_scout",
            "message_type": MessageType.TASK,
            "objective": "Collect topics",
            "payload": {"search_query": "AI"},
        }
        data.update(overrides)
        return PMPMessage.create(**data)

    def test_create_generates_ids_and_utc_timestamps(self):
        message = self.make_message()
        self.assertTrue(message.message_id)
        self.assertTrue(message.workflow_id)
        self.assertIsNotNone(message.metadata.created_at.tzinfo)

    def test_json_round_trip_preserves_workflow(self):
        message = self.make_message()
        restored = PMPMessage.from_json(message.to_json())
        self.assertEqual(restored, message)
        self.assertEqual(restored.workflow_id, message.workflow_id)

    def test_invalid_message_type_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.make_message(message_type="APPROVE")

    def test_missing_required_objective_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.make_message(objective="")

    def test_unknown_agent_is_rejected_by_registry_validation(self):
        message = self.make_message(receiver_agent_id="producer.unknown")
        with self.assertRaises(PMPValidationError):
            PMPValidator().validate(message)

    def test_sender_and_receiver_must_differ(self):
        with self.assertRaises(ValidationError):
            self.make_message(receiver_agent_id="producer.manager")

    def test_retry_creates_child_message_and_increments_count(self):
        message = self.make_message()
        retried = message.with_retry("retry")
        self.assertEqual(retried.workflow_id, message.workflow_id)
        self.assertEqual(retried.parent_message_id, message.message_id)
        self.assertEqual(retried.metadata.retry_count, 1)


if __name__ == "__main__":
    unittest.main()

