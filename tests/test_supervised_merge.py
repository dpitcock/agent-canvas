"""Regression coverage for main/supervised-continuation interactions."""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_supervised import supervisor, installer


class SupervisedMergeTests(unittest.TestCase):
    def test_missing_file_retry_cannot_clear_a_newer_incomplete_import(self):
        for mode in ("pause", "reset"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp).resolve()
                project = base / "project"
                project.mkdir()
                state = base / "state"
                installer.install(project, apply=True, supervised=True, supervisor_state_dir=state, home=base / "home")
                host = supervisor.HostSupervisor(state)
                host.create_task("task", project, [{"id": "work", "operation": "write"}])
                def interrupted_import():
                    with patch.object(host, "_event", side_effect=OSError("interrupted")):
                        with self.assertRaises(OSError):
                            host.import_owner_override(project, [mode], source_digest="a" * 64)
                def newer_import(*args, **kwargs):
                    interrupted_import()
                    return None
                # Even identical contents and timestamps are separate imports.
                with patch.object(supervisor, "_now", return_value=123):
                    interrupted_import()
                    with patch.object(installer, "root_owner_override", side_effect=newer_import):
                        with self.assertRaisesRegex(ValueError, "changed"):
                            installer.upgrade(project, apply=True, supervised=True,
                                supervisor_state_dir=state, home=base / "home")
                current = host.provision(project)["owner_override"]
                self.assertFalse(current["import_complete"])
                self.assertEqual(current["modes"], [mode])
                with self.assertRaisesRegex(ValueError, "incomplete"):
                    host.claim_action("task", "work", "attempt", project=project)

    def test_missing_file_retry_preserves_concurrently_completed_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            project = base / "project"
            project.mkdir()
            state = base / "state"
            installer.install(project, apply=True, supervised=True, supervisor_state_dir=state, home=base / "home")
            host = supervisor.HostSupervisor(state)
            with patch.object(host, "_event", side_effect=OSError("failed import")):
                with self.assertRaises(OSError):
                    host.import_owner_override(project, [], source_digest="a" * 64)
            def concurrent_import(*args, **kwargs):
                host.import_owner_override(project, ["pause"], source_digest="b" * 64)
                return None
            with patch.object(installer, "root_owner_override", side_effect=concurrent_import):
                installer.upgrade(project, apply=True, supervised=True, supervisor_state_dir=state, home=base / "home")
            current = host.provision(project)["owner_override"]
            self.assertEqual(current["modes"], ["pause"])
            self.assertEqual(current["source_digest"], "b" * 64)
            self.assertEqual(host.create_task("new", project, [])["status"], "paused")

    def test_resume_waits_for_project_override_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            host = supervisor.HostSupervisor(Path(tmp) / "state")
            host.create_task("task", project, [])
            host.pause("task", project=project)
            entered, proceed, resumed = threading.Event(), threading.Event(), threading.Event()
            errors = []
            original = host._read
            def read_then_wait(path):
                result = original(path)
                if path.name == "task.json" and threading.current_thread().name == "import-worker":
                    entered.set()
                    if not proceed.wait(3):
                        raise RuntimeError("import read barrier timed out")
                return result
            def importing():
                try:
                    host.import_owner_override(project, ["pause"], source_digest="a" * 64)
                except Exception as error:
                    errors.append(error)
            def resuming():
                try:
                    host.resume("task", project=project)
                except Exception as error:
                    errors.append(error)
                finally:
                    resumed.set()
            with patch.object(host, "_read", side_effect=read_then_wait):
                importer = threading.Thread(target=importing, name="import-worker")
                importer.start()
                self.assertTrue(entered.wait(1))
                resume = threading.Thread(target=resuming)
                resume.start()
                try:
                    self.assertFalse(resumed.wait(0.05))
                finally:
                    proceed.set()
                    importer.join(3)
                    resume.join(3)
            self.assertFalse(importer.is_alive() or resume.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(host.task("task", project=project)["status"], "active")

    def test_missing_override_preserves_completed_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            project = base / "project"
            project.mkdir()
            override = project / ".owner-override"
            override.write_text("OWNER_OVERRIDE=pause")
            installer.install(project, apply=True, supervised=True, supervisor_state_dir=base / "state", home=base / "home")
            host = supervisor.HostSupervisor(base / "state")
            before = host.provision(project)["owner_override"]
            override.unlink()
            installer.upgrade(project, apply=True, supervised=True, supervisor_state_dir=base / "state", home=base / "home")
            self.assertEqual(host.provision(project)["owner_override"], before)

    def test_missing_override_completes_interrupted_installer_import(self):
        for operation in ("install", "upgrade"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp).resolve()
                project = base / "project"
                project.mkdir()
                state = base / "state"
                installer.install(project, apply=True, supervised=True, supervisor_state_dir=state, home=base / "home")
                host = supervisor.HostSupervisor(state)
                host.create_task("task", project, [{"id": "work", "operation": "write"}])
                override = project / ".owner-override"
                override.write_text("OWNER_OVERRIDE=pause")
                with patch.object(host, "_event", side_effect=OSError("interrupted import")):
                    with self.assertRaises(OSError):
                        host.import_owner_override(project, ["pause"], source_digest="a" * 64)
                override.unlink()
                getattr(installer, operation)(project, apply=True, supervised=True,
                    supervisor_state_dir=state, home=base / "home")
                self.assertEqual(host.recover("task", project=project).kind, "continue")
                registration = host.provision(project)
                self.assertTrue(registration["owner_override"]["import_complete"])
                self.assertEqual(registration["owner_override"]["modes"], [])

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
