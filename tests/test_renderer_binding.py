"""Shared-stream isolation and single-use host turn bindings."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


spec = importlib.util.spec_from_file_location("bound_client", Path(__file__).resolve().parents[1] / "scripts/supervised_client.py")
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)


class RendererBindingTests(unittest.TestCase):
    def setUp(self):
        self.host = Mock()
        self.host.gate_final.return_value = SimpleNamespace(release=True, message="bound final", kind="complete")
        self.renderer = client.SupervisedRenderer(self.host, "task", thread_id="thread", turn_id="turn")

    def event(self, method, thread="thread", turn="turn", **extra):
        params = {"threadId": thread, **extra}
        if method == "turn/completed":
            params["turn"] = {"id": turn, "status": extra.pop("status", "completed")}
        else:
            params["turnId"] = turn
        return {"method": method, "params": params}

    def test_unbound_renderer_is_rejected(self):
        with self.assertRaises(TypeError):
            client.SupervisedRenderer(self.host, "task")
        for thread, turn in ((None, "turn"), ("thread", ""), (" ", "turn")):
            with self.assertRaises(ValueError):
                client.SupervisedRenderer(self.host, "task", thread_id=thread, turn_id=turn)

    def test_foreign_and_missing_ids_never_reach_progress_buffer_or_gate(self):
        for thread, turn in (("foreign", "turn"), ("thread", "foreign"), (None, "turn"), ("thread", None)):
            for method, extra in (("item/agentMessage/delta", {"delta": "foreign"}),
                                  ("item/completed", {"item": {"type": "agentMessage", "text": "foreign"}}),
                                  ("item/commandExecution/outputDelta", {"delta": "foreign progress"}),
                                  ("turn/completed", {})):
                with self.subTest(thread=thread, turn=turn, method=method):
                    self.assertEqual(self.renderer.consume(self.event(method, thread, turn, **extra)), [])
        self.host.gate_final.assert_not_called()
        self.renderer.consume(self.event("item/completed", item={"type": "agentMessage", "text": "bound final"}))
        self.renderer.consume(self.event("turn/completed"))
        self.assertEqual(self.host.gate_final.call_args.args[2], "bound final")

    def test_matching_progress_is_visible_and_conflicting_completion_is_ignored(self):
        event = self.event("item/commandExecution/outputDelta", delta="working")
        self.assertEqual(self.renderer.consume(event), [{"kind": "progress", "event": event}])
        event = self.event("turn/completed")
        event["params"]["turnId"] = "foreign"
        self.assertEqual(self.renderer.consume(event), [])
        self.host.gate_final.assert_not_called()

    def test_duplicate_completion_and_reuse_are_ignored(self):
        self.renderer.consume(self.event("turn/completed"))
        self.assertEqual(self.renderer.consume(self.event("turn/completed")), [])
        self.assertEqual(self.renderer.consume(self.event("item/completed", item={"type": "agentMessage", "text": "late"})), [])
        self.assertEqual(self.renderer.consume(self.event("item/commandExecution/outputDelta", delta="late")), [])
        self.assertEqual(self.renderer.consume(self.event("turn/completed", turn="next")), [])
        self.host.gate_final.assert_called_once()

    def test_failed_turn_and_gate_exception_also_close_the_binding(self):
        self.renderer.consume(self.event("turn/completed", status="failed"))
        self.assertEqual(self.renderer.consume(self.event("turn/completed")), [])
        self.host.gate_final.assert_not_called()
        renderer = client.SupervisedRenderer(self.host, "task", thread_id="thread", turn_id="next")
        self.host.gate_final.side_effect = RuntimeError("interrupted host")
        with self.assertRaises(RuntimeError):
            renderer.consume(self.event("turn/completed", turn="next"))
        self.assertEqual(renderer.consume(self.event("turn/completed", turn="next")), [])
        self.host.gate_final.assert_called_once()
