"""Regression coverage for main/supervised-continuation interactions."""
from pathlib import Path
import tempfile
import unittest

from test_supervised import supervisor


class SupervisedMergeTests(unittest.TestCase):
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
