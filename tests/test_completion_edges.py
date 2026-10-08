"""State durability and immutable completion regressions."""
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from test_supervised import supervisor, client
from test_container_executor import executor


class CompletionEdges(unittest.TestCase):
    def test_renderer_delivers_recovered_terminal_commit_once(self):
        for mode in ("plain", "validated", "blocker"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "project"
                project.mkdir()
                host = supervisor.HostSupervisor(root / "state")
                host.create_task("task", project, [],
                    validators=[{"id": "check", "command": ["true"]}] if mode == "validated" else [],
                    blockers=[{"owner_action": "Choose."}] if mode == "blocker" else [])
                renderer = client.SupervisedRenderer(host, "task", legacy_test_mode=True)
                renderer.consume({"method": "item/completed", "params": {"item": {
                    "type": "agentMessage", "id": "final", "phase": "final_answer", "text": "done"}}})
                completion = {"method": "turn/completed", "params": {"turnId": "turn", "status": "completed"}}
                original = os.fsync
                def fail_terminal_directory(fd):
                    if stat.S_ISDIR(os.fstat(fd).st_mode) and host.task("task", project=project)["status"] in {"complete", "blocked"}:
                        raise OSError("directory sync failed")
                    original(fd)
                with patch.object(supervisor.os, "fsync", side_effect=fail_terminal_directory):
                    output = renderer.consume(completion)
                self.assertEqual(output, [{"kind": "final", "content": "Choose." if mode == "blocker" else "done",
                    "decision": "blocker" if mode == "blocker" else "complete",
                    "durability_warning": "state_directory_sync_failed"}])
                self.assertEqual(renderer.consume(completion), [])

    def test_post_replace_sync_failure_recovers_release_without_duplicate(self):
        for blocked in (False, True):
            with self.subTest(blocked=blocked), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "project"
                project.mkdir()
                host = supervisor.HostSupervisor(root / "state")
                host.create_task("task", project, [], blockers=[{"owner_action": "Choose."}] if blocked else [])
                original = os.fsync
                def fail_directory(fd):
                    if stat.S_ISDIR(os.fstat(fd).st_mode):
                        raise OSError("directory sync failed")
                    original(fd)
                with patch.object(supervisor.os, "fsync", side_effect=fail_directory):
                    decision = host.gate_final("task", "attempt", "done", project=project)
                self.assertTrue(decision.release)
                self.assertFalse(decision.durability_confirmed)
                self.assertEqual(decision.message, "Choose." if blocked else "done")
                self.assertFalse(host.gate_final("task", "retry", "done", project=project).release)
                self.assertEqual(host.visible_messages("task", project=project), [decision.message])

    def test_cancelled_task_rejects_late_completion_and_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            host = supervisor.HostSupervisor(root / "state")
            host.create_task("task", project, [{"id": "a", "operation": "delegate", "child_task_id": "child"}])
            host.claim_action("task", "a", "attempt", project=project)
            host.cancel("task", project=project)
            before = host.task("task", project=project)
            for callback in (
                lambda: host.complete_action("task", "a", evidence={}, project=project),
                lambda: host.join_child("task", "a", "child", evidence={}, project=project),
                lambda: host.reconcile_action("task", "a", succeeded=True, receipt={}, project=project),
            ):
                with self.assertRaises(ValueError):
                    callback()
                self.assertEqual(host.task("task", project=project), before)

    def test_input_collection_requires_ordered_vector(self):
        for inputs in ("input.txt", {"input.txt"}, {"input.txt": True}, None):
            with self.subTest(inputs=inputs), self.assertRaises(executor.ConfigurationError):
                executor.ExecutionRequest(action_id="a", attempt_id="b", project=Path("/tmp/project"),
                    image="example/tool@sha256:" + "a" * 64, command=["true"], inputs=inputs)

    def test_completion_replay_preserves_evidence_and_events(self):
        for child in (False, True):
            with self.subTest(child=child), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "project"
                project.mkdir()
                host = supervisor.HostSupervisor(root / "state")
                host.create_task("task", project, [{"id": "a", "operation": "delegate" if child else "write",
                                                   "child_task_id": "child"}])
                def complete(evidence):
                    if child:
                        host.join_child("task", "a", "child", evidence=evidence, project=project)
                    else:
                        host.complete_action("task", "a", evidence=evidence, project=project)
                complete({"receipt": "original"})
                for terminal in (False, True):
                    if terminal:
                        host.gate_final("task", "attempt", "done", project=project)
                    before = host.task("task", project=project)
                    events = host.audit("task", project=project)
                    complete({"receipt": "original"})
                    with self.assertRaises(ValueError):
                        complete({"receipt": "different"})
                    self.assertEqual(host.task("task", project=project), before)
                    self.assertEqual(host.audit("task", project=project), events)

    def test_replacement_syncs_parent_after_new_state_is_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.json"
            path.write_text('{"old": true}')
            synced = []
            fsync = os.fsync
            def observe(fd):
                kind = "directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
                if kind == "directory":
                    self.assertEqual(json.loads(path.read_text()), {"new": True})
                synced.append(kind)
                fsync(fd)
            with patch.object(supervisor.os, "fsync", side_effect=observe):
                supervisor.HostSupervisor._write(path, {"new": True})
            self.assertEqual(synced, ["file", "directory"])

    def test_directory_sync_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.json"
            fsync = os.fsync
            def fail_directory(fd):
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    raise OSError("directory sync failed")
                fsync(fd)
            with patch.object(supervisor.os, "fsync", side_effect=fail_directory):
                with self.assertRaises(OSError):
                    supervisor.HostSupervisor._write(path, {"new": True})
            self.assertEqual(list(Path(tmp).iterdir()), [path])
