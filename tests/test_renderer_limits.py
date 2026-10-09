"""Resource-limit regressions using the real renderer and host boundary."""
import tempfile
from pathlib import Path
import unittest

from test_supervised import client, supervisor


class RendererLimits(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        project = base / "project"
        project.mkdir()
        self.host = supervisor.HostSupervisor(base / "host")
        self.host.provision(project)
        self.host.create_task("task", project, [{"id": "write", "operation": "write"}])
        self.host.complete_action("task", "write", evidence={"done": True})
        self.renderer = client.SupervisedRenderer(self.host, "task", legacy_test_mode=True)
        self.renderer.MAX_TEXT_BYTES = 8
        self.renderer.MAX_ITEM_IDS = 3
        self.renderer.MAX_TURN_IDS = 3
        self.renderer.MAX_CHUNKS = 3

    def delta(self, text, item="a"):
        return self.renderer.consume({"method": "item/agentMessage/delta", "params": {
            "itemId": item, "delta": text}})

    def complete(self, text=None, item="a", phase="final_answer"):
        data = {"id": item, "type": "agentMessage", "phase": phase}
        if text is not None:
            data["text"] = text
        return self.renderer.consume({"method": "item/completed", "params": {"item": data}})

    def turn(self, turn="t"):
        return self.renderer.consume({"method": "turn/completed", "params": {
            "turnId": turn, "status": "completed"}})

    def assert_latched(self, output):
        error = [{"kind": "error", "message": "renderer_limit_exceeded"}]
        self.assertEqual(output, error)
        self.assertEqual(self.delta("safe", "later"), error)
        self.assertEqual(self.complete("safe", "later"), error)
        self.assertEqual(self.turn(), error)
        self.assertEqual(self.host.visible_messages("task"), [])
        self.assertNotIn("final_attempt", [e["type"] for e in self.host.audit("task")])

    def test_oversized_completed_text_cannot_bypass_delta_budget(self):
        self.assert_latched(self.complete("x" * 9))

    def test_oversized_delta_permanently_blocks_later_safe_text(self):
        self.assert_latched(self.delta("x" * 9))

    def test_cumulative_pending_and_completed_text_share_budget(self):
        self.complete("1234")
        self.delta("1234", "b")
        self.assert_latched(self.delta("x", "b"))

    def test_multibyte_exact_boundary_and_completion_replacement(self):
        self.delta("éééé")
        self.assertEqual(self.complete("🙂🙂"), [])
        self.assertEqual(self.turn()[0]["content"], "🙂🙂")

    def test_multibyte_overflow(self):
        self.delta("ééé")
        self.assert_latched(self.delta("€"))

    def test_invalid_unicode_fails_closed(self):
        self.assert_latched(self.complete("\ud800"))

    def test_chunk_budget_is_bounded_independently_of_bytes(self):
        for _ in range(3):
            self.delta("x")
        self.assert_latched(self.delta("x"))

    def test_empty_deltas_consume_neither_chunks_nor_item_ids(self):
        for index in range(5000):
            self.assertEqual(self.delta("", str(index)), [])
        self.complete("safe")
        self.assertEqual(self.turn()[0]["content"], "safe")

    def test_pending_item_budget(self):
        for item in ("a", "b", "c"):
            self.delta("x", item)
        self.assert_latched(self.delta("x", "d"))

    def test_completed_item_history_cannot_grow_across_turn_resets(self):
        for index in range(3):
            self.complete("", str(index))
            self.turn(str(index))
        self.assert_latched(self.complete("safe", "fourth"))

    def test_turn_history_overflow_blocks_before_host_gate(self):
        for index in range(3):
            self.turn(str(index))
        self.complete("safe")
        self.assert_latched(self.turn("fourth"))

    def test_replay_at_history_capacity_does_not_fail_or_release(self):
        for index in range(3):
            self.complete("", str(index))
            self.turn(str(index))
        self.assertEqual(self.complete("secret", "0"), [])
        self.assertEqual(self.delta("secret", "0"), [])
        self.assertEqual(self.turn("0"), [])
        self.assertEqual(self.host.visible_messages("task"), [])

    def test_text_budget_resets_after_turn_without_final(self):
        self.complete("12345678", phase="commentary")
        self.assertEqual(self.turn()[0]["kind"], "progress")
        self.delta("12345678", "b")
        self.complete(item="b")
        self.assertEqual(self.turn("next")[0]["content"], "12345678")

    def test_completed_and_pending_item_ids_are_length_bounded(self):
        self.assert_latched(self.complete("safe", "x" * 513))

    def test_pending_item_ids_are_length_bounded(self):
        self.assert_latched(self.delta("safe", "x" * 513))

    def test_large_phase_is_not_treated_as_final_or_retained(self):
        self.complete("secret", phase="x" * 100000)
        self.assertEqual(self.turn()[0]["kind"], "progress")
        self.assertEqual(self.host.visible_messages("task"), [])

    def test_completed_buffer_releases_chunk_budget(self):
        for _ in range(3):
            self.delta("x")
        self.complete("x")
        for _ in range(3):
            self.delta("y", "b")
        self.complete(item="b")
        self.assertEqual(self.turn()[0]["content"], "yyy")


if __name__ == "__main__":
    unittest.main()
