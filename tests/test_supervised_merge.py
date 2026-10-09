"""Regression coverage for main/supervised-continuation interactions."""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_supervised import supervisor


class SupervisedMergeTests(unittest.TestCase):
    def test_failed_override_import_blocks_decisions_until_retry(self):
        for mode, expected in (("pause", "paused"), ("reset", "cancelled")):
            for failure in ("audit", "transition"):
                with self.subTest(mode=mode, failure=failure), tempfile.TemporaryDirectory() as tmp:
                    project = Path(tmp) / "project"
                    project.mkdir()
                    host = supervisor.HostSupervisor(Path(tmp) / "state")
                    for task_id in ("first", "second"):
                        host.create_task(task_id, project, [{"id": "work", "operation": "write"}])
                    method = "_event" if failure == "audit" else ("pause" if mode == "pause" else "cancel")
                    with patch.object(host, method, side_effect=OSError("import interrupted")):
                        with self.assertRaises(OSError):
                            host.import_owner_override(project, [mode], source_digest="a" * 64)
                    restarted = supervisor.HostSupervisor(Path(tmp) / "state")
                    for action in (
                        lambda: restarted.claim_action("second", "work", "attempt", project=project),
                        lambda: restarted.recover("second", project=project),
                        lambda: restarted.gate_final("second", "attempt", "done", project=project),
                    ):
                        with self.assertRaisesRegex(ValueError, "incomplete"):
                            action()
                    self.assertEqual(restarted.visible_messages("second", project=project), [])
                    restarted.import_owner_override(project, [mode], source_digest="a" * 64)
                    self.assertEqual(restarted.recover("second", project=project).kind, expected)
                    if mode == "pause":
                        restarted.resume("second", project=project)
                        self.assertEqual(restarted.claim_action("second", "work", "resumed", project=project).kind, "dispatch")

    def test_override_import_serializes_decisions_for_later_tasks(self):
        for mode, expected in (("pause", "paused"), ("reset", "cancelled")):
            for decision in ("claim", "recover", "final"):
                with self.subTest(mode=mode, decision=decision), tempfile.TemporaryDirectory() as tmp:
                    project = Path(tmp) / "project"
                    project.mkdir()
                    host = supervisor.HostSupervisor(Path(tmp) / "state")
                    for task_id in ("first", "second"):
                        host.create_task(task_id, project, [{"id": "work", "operation": "write"}])
                    entered, proceed, finished = threading.Event(), threading.Event(), threading.Event()
                    errors, results = [], []
                    method = "pause" if mode == "pause" else "cancel"
                    original = getattr(host, method)
                    def delayed(task_id, **kwargs):
                        if task_id == "first":
                            entered.set()
                            if not proceed.wait(3):
                                raise RuntimeError("import barrier timed out")
                        return original(task_id, **kwargs)
                    def importing():
                        try:
                            host.import_owner_override(project, [mode], source_digest="a" * 64)
                        except Exception as error:
                            errors.append(error)
                    def deciding():
                        try:
                            if decision == "claim":
                                result = host.claim_action("second", "work", "attempt", project=project)
                            elif decision == "recover":
                                result = host.recover("second", project=project)
                            else:
                                result = host.gate_final("second", "attempt", "done", project=project)
                            results.append(result.kind)
                        except Exception as error:
                            errors.append(error)
                        finally:
                            finished.set()
                    with patch.object(host, method, side_effect=delayed):
                        importer = threading.Thread(target=importing)
                        importer.start()
                        self.assertTrue(entered.wait(1))
                        claimant = threading.Thread(target=deciding)
                        claimant.start()
                        try:
                            self.assertFalse(finished.wait(0.05))
                        finally:
                            proceed.set()
                            importer.join(3)
                            claimant.join(3)
                    self.assertFalse(importer.is_alive() or claimant.is_alive())
                    self.assertEqual(errors, [])
                    self.assertEqual(results, [expected])

    def test_imported_pause_survives_legacy_validation_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            host = supervisor.HostSupervisor(Path(tmp) / "state")
            host.create_task("task", project, [])
            task = host.task("task", project=project)
            task.update(status="validating", validation_attempt="old-attempt")
            host._save_task(task)
            host.import_owner_override(project, ["pause"], source_digest="a" * 64)
            restarted = supervisor.HostSupervisor(Path(tmp) / "state")
            self.assertEqual(restarted.recover("task", project=project).kind, "paused")
            self.assertFalse(restarted.gate_final("task", "new-attempt", "done", project=project).release)
            self.assertEqual(restarted.task("task", project=project)["status"], "paused")

    def test_emergency_release_preserves_committed_blocker_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            host = supervisor.HostSupervisor(Path(tmp) / "state")
            host.create_task("task", project, [], blockers=[{"id": "approval", "owner_action": "Choose."}])
            host.gate_final("task", "attempt", "candidate", project=project)
            prior = [event for event in host.audit("task", project=project)
                     if event["type"] == "blocker_release_committed"]
            self.assertEqual(len(prior), 1)
            self.assertEqual(prior[0]["blocker_id"], "approval")
            host.release_withheld_final("task", owner="owner", reason="Reviewed", project=project)
            restarted = supervisor.HostSupervisor(Path(tmp) / "state")
            events = restarted.audit("task", project=project)
            self.assertEqual([event for event in events if event["type"] == "blocker_release_committed"], prior)
            self.assertEqual(len([event for event in events if event["type"] == "owner_override_final_released"]), 1)
            self.assertEqual(restarted.visible_messages("task", project=project), ["Choose.", "candidate"])
            with self.assertRaises(ValueError):
                restarted.release_withheld_final("task", owner="owner", reason="Retry", project=project)
            self.assertEqual(restarted.audit("task", project=project), events)
