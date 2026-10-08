"""Host-supervised task tests; all state lives in temporary host directories."""
import hashlib
import importlib.util
import multiprocessing
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


supervisor = load("supervisor")
client = load("supervised_client")
installer = load("install")


def claim_from_another_process(state_dir, project, start, results, attempt_id):
    """Claim one action after every contender is ready to race for it."""
    start.wait()
    host = supervisor.HostSupervisor(state_dir)
    results.put(host.claim_action("task-1", "write-doc", attempt_id, project=project).kind)


def complete_from_another_process(state_dir, project, start, action_id):
    """Complete an independent action while every worker races from the same start."""
    start.wait()
    host = supervisor.HostSupervisor(state_dir)
    host.complete_action("task-1", action_id, evidence={"worker": action_id}, project=project)


class SupervisedTasks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.project = self.base / "project"
        self.project.mkdir()
        self.host = supervisor.HostSupervisor(self.base / "host-state")
        self.host.provision(self.project)

    def tearDown(self):
        self.tmp.cleanup()

    def task(self, actions=None, validators=(), blockers=()):
        return self.host.create_task("task-1", self.project, actions or [{"id": "write-doc", "operation": "write"}],
                                     validators=list(validators), blockers=list(blockers))

    def renderer(self, task_id="task-1", **kwargs):
        """Create a legacy unbound renderer only for protocol-fixture tests."""
        return client.SupervisedRenderer(
            self.host, task_id, legacy_test_mode=True, **kwargs
        )

    def test_early_final_is_withheld_and_queues_exact_remaining_action(self):
        self.task()
        decision = self.host.gate_final("task-1", "attempt-1", "I am done")
        self.assertFalse(decision.release)
        self.assertEqual(decision.kind, "continue")
        self.assertEqual(decision.next_action["id"], "write-doc")
        self.assertNotIn("I am done", self.host.visible_messages("task-1"))

    def test_workspace_task_file_claim_cannot_complete_host_action(self):
        self.task()
        (self.project / "tasks.md").write_text("write-doc: complete\n")
        self.assertEqual(self.host.gate_final("task-1", "attempt-1", "done").kind, "continue")

    def test_action_ids_must_be_nonempty_strings_before_persistence(self):
        for index, identifier in enumerate((1, True, [], [1], {}, {"id": "x"}, None, "", "  ")):
            with self.subTest(identifier=identifier):
                task_id = f"invalid-action-{index}"
                with self.assertRaisesRegex(ValueError, "actions require"):
                    self.host.create_task(task_id, self.project, [{"id": identifier, "operation": "write"}])
                self.assertEqual(list((self.base / "host-state").rglob(f"{task_id}.json")), [])

    def test_action_string_id_survives_reload_and_completion(self):
        self.host.create_task("task-1", self.project, [{"id": "1", "operation": "write"}])
        reopened = supervisor.HostSupervisor(self.base / "host-state")
        self.assertEqual(reopened.gate_final("task-1", "first", "done").next_action["id"], "1")
        reopened.complete_action("task-1", "1", evidence={"receipt": "checked"})
        self.assertTrue(reopened.gate_final("task-1", "second", "done").release)

    def test_validator_ids_are_unique_nonempty_strings_before_task_creation(self):
        for index, validators in enumerate((
            [{"id": "same", "command": ["true"]}, {"id": "same", "command": ["true"]}],
            *[[{"id": value, "command": ["true"]}] for value in (None, "", " ", [], {}, 1)],
            ["not a validator"],
        )):
            with self.subTest(validators=validators):
                task_id = f"invalid-validators-{index}"
                with self.assertRaisesRegex(ValueError, "validator"):
                    self.host.create_task(task_id, self.project, [], validators=validators)
                self.assertEqual(list((self.base / "host-state").rglob(f"{task_id}.json")), [])

    def test_existing_duplicate_validators_are_rejected_before_any_execution(self):
        self.host.create_task("task-1", self.project, [], validators=[{"id": "same", "command": ["true"]}])
        task = self.host.task("task-1")
        marker = self.project / "must-not-run"
        task["validators"] = [
            {"id": "same", "command": [sys.executable, "-c", "from pathlib import Path; Path('must-not-run').touch()"]},
            {"id": "same", "command": ["true"]},
        ]
        self.host._save_task(task)
        with self.assertRaisesRegex(ValueError, "validator"):
            self.host.gate_final("task-1", "attempt", "finished")
        self.assertFalse(marker.exists())
        self.assertEqual(self.host.visible_messages("task-1"), [])
        self.assertEqual(self.host.task("task-1")["status"], "active")

    def test_distinct_validators_retain_each_receipt_and_audit_event(self):
        self.host.create_task("task-1", self.project, [], validators=[
            {"id": "first", "command": ["true"]}, {"id": "second", "command": ["true"]},
        ])
        self.assertTrue(self.host.gate_final("task-1", "attempt", "finished").release)
        self.assertEqual(set(self.host.task("task-1")["evidence"]["validators"]), {"first", "second"})
        events = [e for e in self.host.audit("task-1") if e["type"] == "validator_received"]
        self.assertEqual([e["validator_id"] for e in events], ["first", "second"])

    def test_task_id_reuse_rejects_a_different_durable_definition(self):
        actions = [{"id": "write-doc", "operation": "write"}]
        validators = [{"id": "unit", "command": ["python3", "-m", "unittest"]}]
        blockers = [{"id": "approval", "required": True}]
        self.host.create_task(
            "task-1", self.project, actions, validators=validators, blockers=blockers,
            permitted_operations=("read", "write"),
        )

        changed_definitions = (
            {"actions": [{"id": "read-doc", "operation": "read"}]},
            {"validators": [{"id": "lint", "command": ["python3", "-m", "compileall"]}]},
            {"blockers": [{"id": "approval", "required": False}]},
            {"permitted_operations": ("read", "write", "delegate")},
        )
        for changed in changed_definitions:
            with self.subTest(changed=changed):
                with self.assertRaisesRegex(ValueError, "definition"):
                    self.host.create_task(
                        "task-1", self.project,
                        changed.get("actions", actions),
                        validators=changed.get("validators", validators),
                        blockers=changed.get("blockers", blockers),
                        permitted_operations=changed.get("permitted_operations", ("read", "write")),
                    )

    def test_task_creation_rejects_actions_outside_its_authorization(self):
        for task_id, action, permitted_operations in (
            ("publish-task", {"id": "publish", "operation": "publish"}, ("read", "write")),
            ("push-task", {"id": "push", "operation": "push"}, ("read", "write", "push")),
        ):
            with self.subTest(action=action):
                with self.assertRaisesRegex(ValueError, "authorization"):
                    self.host.create_task(task_id, self.project, [action],
                                          permitted_operations=permitted_operations)

    def test_task_creation_rejects_prohibited_authorization_even_without_actions(self):
        """A task must not persist a default-prohibited capability for later use."""
        with self.assertRaisesRegex(ValueError, "prohibited"):
            self.host.create_task("push-capability", self.project, [],
                                  permitted_operations=("read", "push"))

    def test_task_creation_preserves_generator_definitions(self):
        actions = ({"id": "write-doc", "operation": "write"} for _ in range(1))
        validators = ({"id": "check", "command": ["true"]} for _ in range(1))
        blockers = ({"id": "owner", "owner_action": "Approve."} for _ in range(1))

        task = self.host.create_task("task-1", self.project, actions,
                                     validators=validators, blockers=blockers)

        self.assertEqual(list(task["actions"]), ["write-doc"])
        self.assertEqual(task["validators"], [{"id": "check", "command": ["true"]}])
        self.assertEqual(task["blockers"], [{"id": "owner", "owner_action": "Approve."}])

    def test_rejects_project_nested_state_directory_without_creating_it(self):
        """Creating state before the project-boundary check would leave this directory behind."""
        nested_state = self.project / "host-state"
        host = supervisor.HostSupervisor(nested_state)

        with self.assertRaisesRegex(ValueError, "outside"):
            host.provision(self.project)

        self.assertFalse(nested_state.exists())

    def test_unsafe_task_ids_are_rejected_before_task_path_lookup_or_creation(self):
        for task_id in ("../other", "task/child", ".", ""):
            with self.subTest(task_id=task_id):
                with self.assertRaisesRegex(ValueError, "task_id"):
                    self.host.task(task_id, project=self.project)
                with self.assertRaisesRegex(ValueError, "task_id"):
                    self.host.create_task(task_id, self.project, [{"id": "write", "operation": "write"}])

    def test_registration_rejects_a_replacement_directory_at_the_same_path(self):
        original = self.project
        replacement = self.base / "replacement"
        replacement.mkdir()
        original.rename(self.base / "original")
        replacement.rename(original)

        with self.assertRaisesRegex(ValueError, "identity"):
            self.host.provision(original)

    def test_provision_rejects_state_roots_that_overlap_the_project(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for state_dir, project in (
                (base / "project" / "host-state", base / "project"),
                (base / "host-state", base / "host-state" / "project"),
            ):
                with self.subTest(state_dir=state_dir, project=project):
                    project.mkdir(parents=True, exist_ok=True)
                    host = supervisor.HostSupervisor(state_dir)

                    with self.assertRaisesRegex(ValueError, "outside"):
                        host.provision(project)

    def test_rejects_projects_symlink_before_writing_supervised_workspace(self):
        """A preexisting state/projects link must not redirect durable state into a project."""
        state_dir = self.base / "linked-host-state"
        state_dir.mkdir()
        os.symlink(self.project, state_dir / "projects")
        host = supervisor.HostSupervisor(state_dir)

        with self.assertRaisesRegex(ValueError, "symlink"):
            host.create_task("task-1", self.project, [{"id": "write-doc", "operation": "write"}])

        self.assertEqual(list(self.project.iterdir()), [])

    def test_rejects_symlinked_project_state_before_creating_task_files(self):
        """A project-key link must not redirect registration or task state into a workspace."""
        project_state = self.host._project_dir(self.project)
        shutil.rmtree(project_state)
        os.symlink(self.project, project_state)

        with self.assertRaisesRegex(ValueError, "project state directory must not be a symlink"):
            self.host.create_task("task-1", self.project, [{"id": "write-doc", "operation": "write"}])

        self.assertEqual(list(self.project.iterdir()), [])

    def test_rejects_non_directory_project_state_before_creating_task_files(self):
        project_state = self.host._project_dir(self.project)
        shutil.rmtree(project_state)
        project_state.write_text("not a directory")

        with self.assertRaisesRegex(ValueError, "project state directory must be a directory"):
            self.host.create_task("task-1", self.project, [{"id": "write-doc", "operation": "write"}])

        self.assertEqual(list(self.project.iterdir()), [])

    def test_project_scoped_task_operations_reject_a_replacement_directory(self):
        """Skipping registration verification lets a replacement use the prior task state."""
        self.task()
        original = self.project
        replacement = self.base / "replacement"
        replacement.mkdir()
        original.rename(self.base / "original")
        replacement.rename(original)

        for operation in (
            lambda: self.host.task("task-1", project=original),
            lambda: self.host.claim_action("task-1", "write-doc", "attempt-1", project=original),
            lambda: self.host.gate_final("task-1", "attempt-1", "done", project=original),
        ):
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(ValueError, "identity"):
                    operation()

    def test_restart_recovers_rejected_final_and_leased_action(self):
        self.task()
        self.host.gate_final("task-1", "attempt-1", "done")
        self.assertEqual(self.host.claim_action("task-1", "write-doc", "lease-1").kind, "dispatch")
        recovered = supervisor.HostSupervisor(self.base / "host-state")
        self.assertEqual(recovered.recover("task-1").next_action["id"], "write-doc")
        self.assertEqual(recovered.claim_action("task-1", "write-doc", "lease-2").kind, "reconcile")

    def test_restart_recovers_abandoned_validation_for_a_fresh_final_attempt(self):
        """A process crash after persisting validation must not require a manual resume."""
        self.task(validators=[{"id": "check", "command": [sys.executable, "-c", "print('ok')"], "timeout_s": 2}])
        self.host.complete_action("task-1", "write-doc", evidence={"receipt": "host-observed"})
        with self.host._locked_task("task-1") as task:
            task["status"] = "validating"
            task["validation_attempt"] = "interrupted-attempt"
            self.host._save_task(task)

        restarted = supervisor.HostSupervisor(self.base / "host-state")
        recovered = restarted.recover("task-1")

        self.assertEqual(recovered.kind, "active")
        self.assertEqual(restarted.task("task-1")["status"], "active")
        self.assertNotIn("validation_attempt", restarted.task("task-1"))
        decision = restarted.gate_final("task-1", "fresh-attempt", "finished")
        self.assertTrue(decision.release)
        self.assertEqual(decision.kind, "complete")

    def test_valid_completion_runs_fresh_host_validator_then_releases_buffer(self):
        self.task(validators=[{"id": "check", "command": [sys.executable, "-c", "print('ok')"], "timeout_s": 2}])
        self.host.complete_action("task-1", "write-doc", evidence={"receipt": "host-observed"})
        decision = self.host.gate_final("task-1", "attempt-1", "finished")
        self.assertTrue(decision.release)
        self.assertEqual(self.host.visible_messages("task-1"), ["finished"])
        receipt = self.host.task("task-1")["evidence"]["validators"]["check"]
        self.assertEqual(receipt["exit_status"], 0)
        self.assertIn("digest", receipt)

    def test_host_validator_executes_from_the_registered_project(self):
        """Relative validator paths must resolve from the task's registered project."""
        (self.project / "validator-input.txt").write_text("project-owned input\n")
        self.task(validators=[{
            "id": "check-project-cwd",
            "command": [sys.executable, "-c", "from pathlib import Path; print(Path('validator-input.txt').read_text().strip())"],
            "timeout_s": 2,
        }])
        self.host.complete_action("task-1", "write-doc", evidence={"receipt": "host-observed"})

        decision = self.host.gate_final("task-1", "attempt-1", "finished")

        self.assertTrue(decision.release)
        receipt = self.host.task("task-1")["evidence"]["validators"]["check-project-cwd"]
        self.assertEqual(receipt["exit_status"], 0)
        self.assertEqual(receipt["output"], "project-owned input\n")

    def test_validator_receipt_hashes_a_project_relative_executable(self):
        """The receipt must identify the binary executed after changing to the project."""
        check = self.project / "check"
        check.write_text("#!/bin/sh\nexit 0\n")
        check.chmod(0o700)
        self.task(validators=[{
            "id": "project-check",
            "command": ["./check"],
            "timeout_s": 2,
        }])
        self.host.complete_action("task-1", "write-doc", evidence={"receipt": "host-observed"})

        decision = self.host.gate_final("task-1", "attempt-1", "finished")

        self.assertTrue(decision.release)
        receipt = self.host.task("task-1")["evidence"]["validators"]["project-check"]
        self.assertEqual(receipt["binary_digest"], hashlib.sha256(check.read_bytes()).hexdigest())

    def test_validator_receipt_hashes_the_project_executable_snapshot_it_runs(self):
        """Rewriting a validator after launch must not change the executed-binary receipt."""
        check = self.project / "check"
        original = b"#!/bin/sh\necho started > started\nsleep 0.2\necho original\n"
        replacement = b"#!/bin/sh\necho replacement\n"
        check.write_bytes(original)
        check.chmod(0o700)
        result = []

        with self.host._pinned_registered_project(self.project) as descriptor:
            worker = threading.Thread(target=lambda: result.append(self.host._validator_receipt(
                {"id": "project-check", "command": ["./check"], "timeout_s": 2},
                project_descriptor=descriptor,
            )))
            worker.start()
            deadline = time.monotonic() + 2
            while not (self.project / "started").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue((self.project / "started").exists())
            check.write_bytes(replacement)
            check.chmod(0o700)
            worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        receipt = result[0]
        self.assertEqual(receipt["exit_status"], 0)
        self.assertIn("original", receipt["output"])
        self.assertEqual(receipt["binary_digest"], hashlib.sha256(original).hexdigest())

    def test_validator_receipt_hashes_bare_executable_from_relative_path_entry(self):
        """A bare command must be hashed as execvp resolves it after entering the project."""
        check = self.project / "check"
        check.write_text("#!/bin/sh\nexit 0\n")
        check.chmod(0o700)
        self.task(validators=[{
            "id": "project-check-from-path",
            "command": ["check"],
            "timeout_s": 2,
        }])
        self.host.complete_action("task-1", "write-doc", evidence={"receipt": "host-observed"})

        with patch.dict(os.environ, {"PATH": f".{os.pathsep}{os.environ['PATH']}"}):
            decision = self.host.gate_final("task-1", "attempt-1", "finished")

        self.assertTrue(decision.release)
        receipt = self.host.task("task-1")["evidence"]["validators"]["project-check-from-path"]
        self.assertEqual(receipt["binary_digest"], hashlib.sha256(check.read_bytes()).hexdigest())

    def test_host_validator_remains_in_registered_directory_if_project_path_is_replaced(self):
        """A path replacement just before spawn must not validate the substitute project."""
        (self.project / "validator-input.txt").write_text("registered project\n")
        replacement = self.base / "replacement"
        replacement.mkdir()
        (replacement / "validator-input.txt").write_text("replacement project\n")
        self.task(validators=[{
            "id": "check-pinned-project-cwd",
            "command": [sys.executable, "-c", "from pathlib import Path; print(Path('validator-input.txt').read_text().strip())"],
            "timeout_s": 2,
        }])
        task = self.host.task("task-1")
        popen = supervisor.subprocess.Popen

        def replace_project_before_spawn(*args, **kwargs):
            self.assertNotIn("preexec_fn", kwargs)
            descriptor = kwargs["pass_fds"][0]
            self.assertIsNone(kwargs["cwd"])
            self.assertEqual(args[0][:4], [sys.executable, "-c", supervisor._VALIDATOR_LAUNCHER, str(descriptor)])
            self.project.rename(self.base / "registered-project")
            replacement.rename(self.project)
            return popen(*args, **kwargs)

        with patch.object(supervisor.subprocess, "Popen", side_effect=replace_project_before_spawn):
            valid, receipts = self.host._validate(task)

        self.assertTrue(valid)
        receipt = receipts["check-pinned-project-cwd"]
        self.assertEqual(receipt["exit_status"], 0)
        self.assertEqual(receipt["output"], "registered project\n")

    def test_genuine_blocker_is_released_only_after_independent_actions_finish(self):
        self.task(blockers=[{"id": "owner-choice", "owner_action": "Choose the deployment region."}])
        self.assertEqual(self.host.gate_final("task-1", "attempt-1", "blocked").kind, "continue")
        self.host.complete_action("task-1", "write-doc", evidence={"receipt": "host-observed"})
        decision = self.host.gate_final("task-1", "attempt-2", "blocked")
        self.assertTrue(decision.release)
        self.assertEqual(decision.kind, "blocker")
        self.assertEqual(self.host.visible_messages("task-1"), ["Choose the deployment region."])

    def test_delivery_audit_failure_can_retry_without_losing_or_duplicating_output(self):
        cases = (
            ("plain", [], [], "final_attempt", "complete", "finished"),
            ("validated", [{"id": "check", "command": ["true"]}], [], "validator_received", "complete", "finished"),
            ("blocker", [], [{"id": "choice", "owner_action": "Choose a region."}], "final_attempt", "blocker", "Choose a region."),
            ("validation-start", [{"id": "check", "command": ["true"]}], [], "validation_started", "complete", "finished"),
            ("validation-receipt", [{"id": "check", "command": ["true"]}], [], "validator_received", "complete", "finished"),
        )
        for task_id, validators, blockers, failed_event, decision_kind, message in cases:
            with self.subTest(task_id=task_id):
                self.host.create_task(task_id, self.project, [], validators=validators, blockers=blockers)
                renderer = self.renderer(task_id)
                renderer.consume({"method": "item/completed", "params": {"item": {
                    "type": "agentMessage", "id": "final", "phase": "final_answer", "text": "finished",
                }}})
                completion = {"method": "turn/completed", "params": {"turnId": "turn-1", "status": "completed"}}
                original_event = self.host._event

                def fail_audit(task_id, event, **kwargs):
                    if event == failed_event:
                        raise OSError("audit unavailable")
                    return original_event(task_id, event, **kwargs)

                with patch.object(self.host, "_validate", return_value=(True, {"check": {"exit_status": 0}})):
                    with patch.object(self.host, "_event", side_effect=fail_audit):
                        with self.assertRaisesRegex(OSError, "audit unavailable"):
                            renderer.consume(completion)
                    self.assertEqual(self.host.task(task_id)["status"], "active")
                    self.assertNotIn("validation_attempt", self.host.task(task_id))
                    self.assertEqual(self.host.visible_messages(task_id), [])
                    self.assertEqual(renderer.consume(completion), [
                        {"kind": "final", "content": message, "decision": decision_kind},
                    ])
                self.assertEqual(renderer.consume(completion), [])
                self.assertFalse(self.host.gate_final(task_id, "replay", "finished").release)
                self.assertEqual(self.host.visible_messages(task_id), [message])

    def test_failed_terminal_state_write_does_not_audit_a_successful_release(self):
        for task_id, validators, blockers, message in (
            ("plain", [], [], "finished"),
            ("validated", [{"id": "check", "command": ["true"]}], [], "finished"),
            ("blocker", [], [{"id": "choice", "owner_action": "Choose."}], "Choose."),
        ):
            with self.subTest(task_id=task_id):
                self.host.create_task(task_id, self.project, [], validators=validators, blockers=blockers)
                renderer = self.renderer(task_id)
                renderer.consume({"method": "item/completed", "params": {"item": {
                    "type": "agentMessage", "id": "final", "phase": "final_answer", "text": "finished",
                }}})
                completion = {"method": "turn/completed", "params": {"turnId": "turn-1", "status": "completed"}}
                original_replace = supervisor.os.replace

                def fail_terminal_replace(source, destination):
                    candidate = supervisor.json.loads(Path(source).read_text())
                    if candidate["status"] in {"complete", "blocked"}:
                        raise OSError("state unavailable")
                    return original_replace(source, destination)

                release_types = {"final_released", "blocker_delivered", "final_release_committed", "blocker_release_committed"}
                with patch.object(supervisor.os, "replace", side_effect=fail_terminal_replace):
                    with self.assertRaisesRegex(OSError, "state unavailable"):
                        renderer.consume(completion)
                self.assertEqual(self.host.task(task_id)["status"], "active")
                self.assertEqual(self.host.visible_messages(task_id), [])
                self.assertFalse(any(e["type"] in release_types for e in self.host.audit(task_id)))
                self.assertEqual(renderer.consume(completion)[0]["content"], message)
                self.assertEqual(renderer.consume(completion), [])
                reopened = supervisor.HostSupervisor(self.base / "host-state")
                releases = [e for e in reopened.audit(task_id) if e["task_id"] == task_id and e["type"] in release_types]
                self.assertEqual(len(releases), 1)
                self.assertEqual(reopened.visible_messages(task_id), [message])

    def test_release_commit_survives_client_failure_without_claiming_display(self):
        self.host.create_task("task-1", self.project, [])
        original_save = self.host._save_task

        def fail_after_commit(task):
            original_save(task)
            raise OSError("client lost response")

        with patch.object(self.host, "_save_task", side_effect=fail_after_commit):
            with self.assertRaisesRegex(OSError, "client lost response"):
                self.host.gate_final("task-1", "first", "finished")
        reopened = supervisor.HostSupervisor(self.base / "host-state")
        self.assertEqual(reopened.visible_messages("task-1"), ["finished"])
        self.assertFalse(reopened.gate_final("task-1", "retry", "finished").release)
        events = reopened.audit("task-1")
        self.assertEqual([e["type"] for e in events].count("final_release_committed"), 1)
        self.assertNotIn("final_released", [e["type"] for e in events])

    def test_completed_final_is_not_released_twice_when_a_turn_is_replayed(self):
        self.task()
        self.host.complete_action("task-1", "write-doc", evidence={"receipt": "host-observed"})

        first = self.host.gate_final("task-1", "attempt-1", "finished")
        replay = self.host.gate_final("task-1", "attempt-2", "replayed final")

        self.assertTrue(first.release)
        self.assertEqual(replay.kind, "complete")
        self.assertFalse(replay.release)
        self.assertEqual(self.host.visible_messages("task-1"), ["finished"])

    def test_blocked_final_is_not_released_twice_when_a_turn_is_replayed(self):
        self.task(blockers=[{"id": "owner-choice", "owner_action": "Choose the deployment region."}])
        self.host.complete_action("task-1", "write-doc", evidence={"receipt": "host-observed"})

        first = self.host.gate_final("task-1", "attempt-1", "blocked")
        replay = self.host.gate_final("task-1", "attempt-2", "replayed final")

        self.assertTrue(first.release)
        self.assertEqual(replay.kind, "blocked")
        self.assertFalse(replay.release)
        self.assertEqual(self.host.visible_messages("task-1"), ["Choose the deployment region."])

    def test_pause_and_cancel_interrupt_and_prevent_automatic_continuation(self):
        self.task()
        self.host.pause("task-1")
        self.assertEqual(self.host.gate_final("task-1", "attempt-1", "done").kind, "paused")
        self.host.resume("task-1")
        self.host.cancel("task-1")
        self.assertEqual(self.host.gate_final("task-1", "attempt-2", "done").kind, "cancelled")

    def test_pause_and_cancel_preserve_completed_and_blocked_terminal_states(self):
        terminal_cases = (
            ("complete", (), "finished"),
            ("blocked", [{"id": "owner-choice", "owner_action": "Choose the deployment region."}], "blocked"),
        )
        for expected_status, blockers, final_content in terminal_cases:
            with self.subTest(status=expected_status):
                task_id = f"task-{expected_status}"
                self.host.create_task(task_id, self.project, actions=[{"id": "write-doc", "operation": "write"}],
                                      blockers=blockers)
                self.host.complete_action(task_id, "write-doc", evidence={"receipt": "host-observed"})
                self.host.gate_final(task_id, "terminal-final", final_content)

                self.host.pause(task_id)
                self.assertEqual(self.host.task(task_id)["status"], expected_status)
                self.host.cancel(task_id)
                self.assertEqual(self.host.task(task_id)["status"], expected_status)
                with self.assertRaisesRegex(ValueError, "Only a paused task can resume"):
                    self.host.resume(task_id)

    def test_pause_cannot_make_a_cancelled_task_resumable(self):
        self.task()
        self.host.cancel("task-1")

        self.host.pause("task-1")

        self.assertEqual(self.host.task("task-1")["status"], "cancelled")
        with self.assertRaisesRegex(ValueError, "Only a paused task can resume"):
            self.host.resume("task-1")

    def test_pause_and_cancel_preempt_a_running_validator_without_releasing_the_final(self):
        for transition, expected_status in ((self.host.pause, "paused"), (self.host.cancel, "cancelled")):
            with self.subTest(transition=expected_status):
                task_id = f"{expected_status}-validator"
                validator_started = threading.Event()
                allow_validator_to_finish = threading.Event()
                transitioned = threading.Event()
                result = []

                def blocking_validator(_validator, *, project=None, project_descriptor=None):
                    validator_started.set()
                    self.assertTrue(allow_validator_to_finish.wait(timeout=5))
                    return {"exit_status": 0}

                self.host.create_task(task_id, self.project, [{"id": "write-doc", "operation": "write"}],
                                      validators=[{"id": "check", "command": ["unused"]}])
                self.host.complete_action(task_id, "write-doc", evidence={"receipt": "host-observed"})
                with patch.object(self.host, "_validator_receipt", side_effect=blocking_validator):
                    worker = threading.Thread(target=lambda: result.append(
                        self.host.gate_final(task_id, "attempt-1", "finished")
                    ))
                    worker.start()
                    self.assertTrue(validator_started.wait(timeout=5))

                    def apply_transition():
                        transition(task_id)
                        transitioned.set()

                    transition_worker = threading.Thread(target=apply_transition)
                    transition_worker.start()
                    try:
                        preempted = transitioned.wait(timeout=1)
                    finally:
                        allow_validator_to_finish.set()
                    worker.join(timeout=5)
                    transition_worker.join(timeout=5)

                self.assertTrue(preempted)
                self.assertFalse(worker.is_alive())
                self.assertFalse(transition_worker.is_alive())
                self.assertEqual(self.host.task(task_id)["status"], expected_status)
                self.assertEqual(result[0].kind, expected_status)
                self.assertFalse(result[0].release)
                self.assertEqual(self.host.visible_messages(task_id), [])

    def test_crash_after_dispatch_requires_reconciliation_not_duplicate_dispatch(self):
        self.task(actions=[{"id": "send", "operation": "write", "side_effect": True}])
        self.assertEqual(self.host.claim_action("task-1", "send", "attempt-1").kind, "dispatch")
        restarted = supervisor.HostSupervisor(self.base / "host-state")
        self.assertEqual(restarted.claim_action("task-1", "send", "attempt-2").kind, "reconcile")
        restarted.reconcile_action("task-1", "send", succeeded=True, receipt={"provider_id": "one"})
        self.assertEqual(restarted.task("task-1")["actions"]["send"]["status"], "complete")

    def test_concurrent_processes_dispatch_an_action_once(self):
        """Removing claim serialization allows multiple processes to dispatch this action."""
        self.task()
        context = multiprocessing.get_context("fork")
        start = context.Event()
        results = context.Queue()
        workers = [
            context.Process(target=claim_from_another_process,
                            args=(str(self.base / "host-state"), str(self.project), start, results, f"attempt-{index}"))
            for index in range(16)
        ]
        for worker in workers:
            worker.start()
        start.set()
        decisions = [results.get(timeout=10) for _ in workers]
        for worker in workers:
            worker.join(timeout=10)
            self.assertEqual(worker.exitcode, 0)
        self.assertEqual(decisions.count("dispatch"), 1)
        self.assertEqual(decisions.count("reconcile"), len(workers) - 1)

    def test_concurrent_processes_complete_independent_actions_without_losing_state(self):
        """Removing mutation serialization loses completions from competing file replacements."""
        action_ids = [f"write-{index}" for index in range(16)]
        self.task(actions=[{"id": action_id, "operation": "write"} for action_id in action_ids])
        context = multiprocessing.get_context("fork")
        start = context.Event()
        workers = [
            context.Process(target=complete_from_another_process,
                            args=(str(self.base / "host-state"), str(self.project), start, action_id))
            for action_id in action_ids
        ]
        for worker in workers:
            worker.start()
        start.set()
        for worker in workers:
            worker.join(timeout=10)
            self.assertEqual(worker.exitcode, 0)

        actions = self.host.task("task-1")["actions"]
        self.assertEqual({action_id for action_id, action in actions.items() if action["status"] == "complete"}, set(action_ids))

    def test_validator_receipt_decodes_byte_output_before_hashing(self):
        """A timed-out byte stream still yields a text receipt and digest."""
        started = self.base / "validator-started"
        receipt = self.host._validator_receipt({
            "id": "check",
            "command": [sys.executable, "-c", f"import pathlib, sys, time; pathlib.Path({str(started)!r}).write_text('started'); sys.stdout.buffer.write(b'validator output'); sys.stdout.flush(); time.sleep(10)"],
            "timeout_s": 1,
        })

        self.assertEqual(receipt["output"], "validator output")
        self.assertEqual(receipt["digest"], "af0c829e3106013e4ef9555521848126776f8a7054fc42e1b64b94dbc4627de1")
        self.assertEqual(receipt["exit_status"], "timeout")
        self.assertTrue(started.exists())

    def test_validator_timeout_terminates_descendants_that_hold_the_output_pipe_open(self):
        """Killing only the parent leaves its child holding stdout open forever."""
        started = self.base / "validator-started"
        child_pid = self.base / "validator-child-pid"
        gate = self.base / "validator-gate"
        code = (
            "import os, pathlib, subprocess, sys, time\n"
            f"gate = pathlib.Path({str(gate)!r})\n"
            f"pathlib.Path({str(started)!r}).write_text('started')\n"
            "while not gate.exists(): time.sleep(0.01)\n"
            "child = subprocess.Popen([sys.executable, '-c', \"import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)\"])\n"
            f"pathlib.Path({str(child_pid)!r}).write_text(str(child.pid))\n"
            "print('child-started', flush=True)\n"
            "time.sleep(30)\n"
        )
        result = []
        worker = threading.Thread(target=lambda: result.append(self.host._validator_receipt({
            "id": "descendant-pipe", "command": [sys.executable, "-c", code], "timeout_s": 1,
        })))
        worker.start()
        try:
            deadline = time.monotonic() + 2
            while not started.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(started.exists())
            gate.touch()
            worker.join(timeout=4)
            self.assertFalse(worker.is_alive())
            self.assertEqual(result[0]["exit_status"], "timeout")
        finally:
            if child_pid.exists():
                try:
                    os.kill(int(child_pid.read_text()), 9)
                except ProcessLookupError:
                    pass
            worker.join(timeout=2)

    def test_validator_receipt_bounds_retained_output_while_draining_the_process(self):
        """Validator receipts retain at most 8 KiB even when a child writes much more."""
        with patch.object(supervisor.subprocess, "run", side_effect=AssertionError("must stream output")):
            receipt = self.host._validator_receipt({
                "id": "large-output",
                "command": [sys.executable, "-c", "import sys; sys.stdout.write('x' * 65536)"],
                "timeout_s": 2,
            })

        self.assertEqual(receipt["exit_status"], 0)
        self.assertEqual(len(receipt["output"].encode()), 8192)

    def test_validator_detached_pipe_has_absolute_drain_deadline(self):
        child_pid = self.base / "detached-pid"
        code = (
            "import os, pathlib, time\n"
            "if os.fork() == 0:\n"
            " os.setsid()\n"
            f" pathlib.Path({str(child_pid)!r}).write_text(str(os.getpid()))\n"
            " time.sleep(30)\n"
            "else:\n"
            " time.sleep(30)\n"
        )
        result = []
        worker = threading.Thread(target=lambda: result.append(self.host._validator_receipt({
            "command": [sys.executable, "-c", code], "timeout_s": 0.5,
        })))
        worker.start()
        try:
            worker.join(timeout=4)
            self.assertFalse(worker.is_alive(), "detached stdout must not prevent timeout")
            self.assertEqual(result[0]["exit_status"], "timeout")
        finally:
            if child_pid.exists():
                try:
                    os.kill(int(child_pid.read_text()), 9)
                except ProcessLookupError:
                    pass
            worker.join(timeout=2)

    def test_validator_snapshot_failure_never_launches_original(self):
        with patch.object(supervisor.os, "fsync", side_effect=OSError("disk full")), \
                patch.object(supervisor.subprocess, "Popen") as launch:
            with self.assertRaisesRegex(ValueError, "snapshot"):
                self.host._validator_receipt({"command": [sys.executable, "-c", "pass"]})
            launch.assert_not_called()
        self.assertEqual(list((self.host.root / "validator-snapshots").iterdir()), [])

    def test_validator_missing_digest_never_launches(self):
        for result in ((None, None), (None, "digest")):
            with self.subTest(result=result), \
                    patch.object(self.host, "_validator_snapshot", return_value=result), \
                    patch.object(supervisor.subprocess, "Popen") as launch:
                with self.assertRaisesRegex(ValueError, "snapshot"):
                    self.host._validator_receipt({"command": [sys.executable]})
                launch.assert_not_called()

    def test_validator_snapshot_copy_deadline_cleans_up(self):
        with patch.object(supervisor.time, "monotonic", side_effect=(0, 6)):
            with self.assertRaisesRegex(ValueError, "snapshot"):
                self.host._validator_snapshot(sys.executable)
        self.assertEqual(list((self.host.root / "validator-snapshots").iterdir()), [])

    def test_validator_snapshot_is_read_only(self):
        snapshot, digest = self.host._validator_snapshot(sys.executable)
        try:
            self.assertEqual(Path(snapshot).stat().st_mode & 0o777, 0o500)
            self.assertEqual(digest, hashlib.sha256(Path(snapshot).read_bytes()).hexdigest())
        finally:
            Path(snapshot).unlink()

    def test_validator_stream_failure_terminates_and_reaps_process(self):
        processes = []
        real_launch = supervisor.subprocess.Popen

        def launch(*args, **kwargs):
            process = real_launch(*args, **kwargs)
            processes.append(process)
            return process

        with patch.object(supervisor.subprocess, "Popen", side_effect=launch), \
                patch.object(self.host, "_stream_validator_output", side_effect=OSError("read failed")):
            with self.assertRaisesRegex(OSError, "read failed"):
                self.host._validator_receipt({"command": [sys.executable, "-c", "import time; time.sleep(30)"]})
        self.assertIsNotNone(processes[0].poll())
        self.assertTrue(processes[0].stdout.closed)
        self.assertTrue(processes[0].stderr.closed)
        self.assertEqual(list((self.host.root / "validator-snapshots").iterdir()), [])

    def test_validator_rejects_nonfinite_timeout_before_snapshot(self):
        with patch.object(self.host, "_validator_snapshot") as snapshot:
            for timeout in (float("nan"), float("inf"), True):
                with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                    self.host._validator_receipt({"command": [sys.executable], "timeout_s": timeout})
            snapshot.assert_not_called()

    def test_validator_continuous_output_cannot_starve_timeout(self):
        started = time.monotonic()
        receipt = self.host._validator_receipt({
            "command": [sys.executable, "-c", "import os\nwhile True: os.write(1, b'x' * 65536)"],
            "timeout_s": 0.2,
        })
        self.assertEqual(receipt["exit_status"], "timeout")
        self.assertEqual(len(receipt["output"]), 8192)
        self.assertLess(time.monotonic() - started, 4)

    def test_validator_path_skips_nonexecutable_entry(self):
        first = self.base / "first"
        second = self.base / "second"
        first.mkdir()
        second.mkdir()
        (first / "check").write_text("not executable")
        executable = second / "check"
        executable.write_text("#!/bin/sh\nprintf checked")
        executable.chmod(0o700)
        with patch.dict(os.environ, {"PATH": os.pathsep.join((str(first), str(second)))}):
            receipt = self.host._validator_receipt({"command": ["check"]})
        self.assertEqual(receipt["output"], "checked")
        self.assertEqual(receipt["binary_digest"], hashlib.sha256(executable.read_bytes()).hexdigest())

    def test_continuation_cannot_broaden_default_prohibited_operations(self):
        self.task()
        result = self.host.request_operation("task-1", "push")
        self.assertEqual(result.kind, "blocker")
        self.assertIn("explicit owner authorization", result.message)
        self.assertNotIn("push", self.host.task("task-1")["authorization"]["permitted_operations"])

    def test_corrupt_legacy_authorization_cannot_permit_default_prohibited_operation(self):
        """Request-time enforcement protects state written before capability validation."""
        self.task()
        with self.host._locked_task("task-1") as task:
            task["authorization"]["permitted_operations"].append("push")
            self.host._save_task(task)

        result = self.host.request_operation("task-1", "push")

        self.assertEqual(result.kind, "blocker")

    def test_child_evidence_must_be_joined_before_parent_finalizes(self):
        self.task(actions=[{"id": "child", "operation": "delegate", "child_task_id": "child-1"}])
        self.assertEqual(self.host.gate_final("task-1", "attempt-1", "done").kind, "continue")
        self.host.join_child("task-1", "child", "child-1", evidence={"validator": "passed"})
        self.assertTrue(self.host.gate_final("task-1", "attempt-2", "done").release)

    def test_audit_log_records_all_gate_decisions_and_renderer_never_leaks_deltas(self):
        self.task()
        renderer = self.renderer()
        self.assertEqual(renderer.consume({"method": "item/agentMessage/delta", "params": {"delta": "secret final"}}), [])
        self.assertEqual(renderer.consume({"method": "item/completed", "params": {"item": {
            "id": "candidate", "type": "agentMessage", "text": "safe candidate"
        }}}), [])
        output = renderer.consume({"method": "turn/completed", "params": {"turnId": "turn-1", "status": "completed"}})
        self.assertEqual(output[0]["kind"], "continuation")
        events = [event["type"] for event in self.host.audit("task-1")]
        self.assertIn("final_attempt", events)
        self.assertIn("continuation_queued", events)

    def test_renderer_requires_a_thread_and_turn_binding_by_default(self):
        """Production renderers cannot accept un-routed private notifications."""
        self.task()

        with self.assertRaisesRegex(ValueError, "thread_id and turn_id"):
            client.SupervisedRenderer(self.host, "task-1")

        renderer = client.SupervisedRenderer(
            self.host, "task-1", thread_id="thread-1", turn_id="turn-1"
        )
        self.assertEqual((renderer.thread_id, renderer.turn_id), ("thread-1", "turn-1"))

    def test_renderer_withholds_completed_agent_message_notifications_until_the_gate(self):
        self.task()
        renderer = self.renderer()

        for event in (
            {"method": "item/completed", "params": {"item": {
                "id": "candidate", "type": "agentMessage", "text": "secret final"
            }}},
            {"method": "item/agentMessage/completed", "params": {"text": "another secret final"}},
        ):
            with self.subTest(method=event["method"]):
                self.assertEqual(renderer.consume(event), [])
        output = renderer.consume({"method": "turn/completed", "params": {"turnId": "turn-1", "status": "completed"}})
        self.assertEqual(output[0]["kind"], "continuation")
        self.assertNotIn("secret final", str(output))

    def test_renderer_suppresses_raw_response_items_before_the_host_gate(self):
        """Raw response items can carry assistant text and are never display-safe."""
        self.task()
        renderer = self.renderer()

        for item in (
            {"type": "message", "role": "assistant", "content": [{"text": "secret answer"}]},
            {"type": "agent_message", "content": [{"text": "another secret answer"}]},
        ):
            with self.subTest(item_type=item["type"]):
                output = renderer.consume({"method": "rawResponseItem/completed", "params": {
                    "threadId": "thread-1", "turnId": "turn-1", "item": item,
                }})
                self.assertEqual(output, [])
                self.assertNotIn("secret", str(output))

        self.assertNotIn("final_attempt", [event["type"] for event in self.host.audit("task-1")])

    def test_renderer_suppresses_unknown_completed_items_before_the_host_gate(self):
        """Non-agent item types can still contain assistant content."""
        self.task()
        renderer = self.renderer()

        output = renderer.consume({"method": "item/completed", "params": {"item": {
            "type": "message", "role": "assistant", "text": "secret answer",
        }}})

        self.assertEqual(output, [])
        self.assertNotIn("final_attempt", [event["type"] for event in self.host.audit("task-1")])

    def test_renderer_rejects_messages_and_completions_outside_its_thread_and_turn(self):
        """Sibling events cannot supply text or complete this supervised task."""
        self.task()
        self.host.complete_action("task-1", "write-doc", evidence={"receipt": "host-observed"})
        unbound_renderer = self.renderer()
        self.assertEqual(unbound_renderer.consume({"method": "turn/completed", "params": {
            "threadId": "thread-b", "turn": {"id": "turn-b", "status": "completed"},
        }}), [])
        self.assertNotIn("final_attempt", [event["type"] for event in self.host.audit("task-1")])
        renderer = self.renderer(thread_id="thread-a", turn_id="turn-a")

        for event in (
            {"method": "item/agentMessage/delta", "params": {
                "threadId": "thread-b", "turnId": "turn-a", "itemId": "foreign", "delta": "foreign text",
            }},
            {"method": "item/completed", "params": {
                "threadId": "thread-a", "turnId": "turn-b",
                "item": {"id": "foreign", "type": "agentMessage", "text": "foreign text"},
            }},
            {"method": "turn/completed", "params": {
                "threadId": "thread-b", "turn": {"id": "turn-b", "status": "completed"},
            }},
        ):
            self.assertEqual(renderer.consume(event), [])

        self.assertNotIn("final_attempt", [event["type"] for event in self.host.audit("task-1")])
        self.assertEqual(renderer.consume({"method": "item/agentMessage/delta", "params": {
            "threadId": "thread-a", "turnId": "turn-a", "itemId": "final", "delta": "approved text",
        }}), [])
        self.assertEqual(renderer.consume({"method": "item/completed", "params": {
            "threadId": "thread-a", "turnId": "turn-a",
            "item": {"id": "final", "type": "agentMessage"},
        }}), [])
        output = renderer.consume({"method": "turn/completed", "params": {
            "threadId": "thread-a", "turn": {"id": "turn-a", "status": "completed"},
        }})
        self.assertEqual(output, [{"kind": "final", "content": "approved text", "decision": "complete"}])

    def test_renderer_releases_only_the_last_completed_agent_message_item(self):
        self.task()
        self.host.complete_action("task-1", "write-doc", evidence={"receipt": "host-observed"})
        renderer = self.renderer()

        for event in (
            {"method": "item/agentMessage/delta", "params": {"itemId": "draft", "delta": "discard me"}},
            {"method": "item/completed", "params": {"item": {"id": "draft", "type": "agentMessage"}}},
            {"method": "item/agentMessage/delta", "params": {"itemId": "final", "delta": "release me"}},
            {"method": "item/completed", "params": {"item": {"id": "final", "type": "agentMessage"}}},
        ):
            self.assertEqual(renderer.consume(event), [])

        output = renderer.consume({"method": "turn/completed", "params": {"turnId": "turn-1", "status": "completed"}})

        self.assertEqual(output, [{"kind": "final", "content": "release me", "decision": "complete"}])

    def test_renderer_prefers_completed_final_answer_over_later_commentary(self):
        self.task()
        self.host.complete_action("task-1", "write-doc", evidence={"receipt": "host-observed"})
        renderer = self.renderer()

        for event in (
            {"method": "item/completed", "params": {"item": {
                "id": "answer", "type": "agentMessage", "phase": "final_answer", "text": "final answer"
            }}},
            {"method": "item/completed", "params": {"item": {
                "id": "commentary", "type": "agentMessage", "phase": "commentary", "text": "never release"
            }}},
        ):
            self.assertEqual(renderer.consume(event), [])

        output = renderer.consume({"method": "turn/completed", "params": {"turnId": "turn-1", "status": "completed"}})

        self.assertEqual(output[0]["content"], "final answer")
        self.assertNotIn("never release", str(output))

    def test_renderer_uses_phase_unknown_completed_text_but_never_commentary(self):
        self.task()
        self.host.complete_action("task-1", "write-doc", evidence={"receipt": "host-observed"})
        renderer = self.renderer()

        for event in (
            {"method": "item/completed", "params": {"item": {
                "id": "commentary", "type": "agentMessage", "phase": "commentary", "text": "never release"
            }}},
            {"method": "item/completed", "params": {"item": {
                "id": "unknown", "type": "agentMessage", "text": "phase unknown"
            }}},
        ):
            self.assertEqual(renderer.consume(event), [])

        output = renderer.consume({"method": "turn/completed", "params": {"turnId": "turn-1", "status": "completed"}})

        self.assertEqual(output[0]["content"], "phase unknown")
        self.assertNotIn("never release", str(output))

    def test_renderer_does_not_gate_a_completed_turn_without_a_selectable_final(self):
        """Commentary-only turns stay nonterminal until a final candidate arrives."""
        self.task()
        self.host.complete_action("task-1", "write-doc", evidence={"receipt": "host-observed"})
        renderer = self.renderer()

        self.assertEqual(renderer.consume({"method": "item/completed", "params": {"item": {
            "id": "commentary", "type": "agentMessage", "phase": "commentary", "text": "not final"
        }}}), [])
        output = renderer.consume({"method": "turn/completed", "params": {
            "turnId": "turn-without-final", "status": "completed"
        }})

        self.assertEqual(output, [{"kind": "progress", "event": {
            "method": "turn/completed", "params": {"status": "completed"}
        }}])
        self.assertEqual(self.host.task("task-1")["status"], "active")
        self.assertNotIn("final_attempt", [event["type"] for event in self.host.audit("task-1")])

        self.assertEqual(renderer.consume({"method": "item/completed", "params": {"item": {
            "id": "final", "type": "agentMessage", "phase": "final_answer", "text": "real final"
        }}}), [])
        self.assertEqual(renderer.consume({"method": "turn/completed", "params": {
            "turnId": "turn-with-final", "status": "completed"
        }}), [{"kind": "final", "content": "real final", "decision": "complete"}])

    def test_renderer_retries_a_completion_after_the_gate_raises(self):
        """A transient gate failure leaves the same completion eligible for replay."""
        self.task()
        self.host.complete_action("task-1", "write-doc", evidence={"receipt": "host-observed"})
        renderer = self.renderer()
        completion = {"method": "turn/completed", "params": {"turnId": "retry-turn", "status": "completed"}}
        self.assertEqual(renderer.consume({"method": "item/completed", "params": {"item": {
            "id": "final", "type": "agentMessage", "phase": "final_answer", "text": "retry final"
        }}}), [])
        original_gate = self.host.gate_final
        gate_attempts = 0

        def transient_gate(*args, **kwargs):
            nonlocal gate_attempts
            gate_attempts += 1
            if gate_attempts == 1:
                raise RuntimeError("temporary failure")
            return original_gate(*args, **kwargs)

        with patch.object(self.host, "gate_final", side_effect=transient_gate):
            with self.assertRaisesRegex(RuntimeError, "temporary failure"):
                renderer.consume(completion)
            output = renderer.consume(completion)

        self.assertEqual(output, [{"kind": "final", "content": "retry final", "decision": "complete"}])

    def test_renderer_ignores_malformed_agent_message_notifications_without_leaking_text(self):
        self.task()
        self.host.complete_action("task-1", "write-doc", evidence={"receipt": "host-observed"})
        renderer = self.renderer()

        self.assertEqual(renderer.consume({"method": "item/agentMessage/delta", "params": None}), [])
        self.assertEqual(renderer.consume({"method": "item/completed", "params": None}), [])
        self.assertEqual(renderer.consume({
            "method": "item/agentMessage/delta", "params": {"delta": "unmatched secret"}
        }), [])
        self.assertEqual(renderer.consume({
            "method": "item/agentMessage/delta", "params": {"itemId": "final", "delta": "safe final"}
        }), [])
        self.assertEqual(renderer.consume({
            "method": "item/completed", "params": {"item": {"id": "final", "type": "agentMessage"}}
        }), [])

        output = renderer.consume({"method": "turn/completed", "params": {"turnId": "turn-1", "status": "completed"}})

        self.assertEqual(output[0]["content"], "safe final")
        self.assertNotIn("unmatched secret", str(output))

    def test_renderer_ignores_late_deltas_after_an_agent_message_item_completes(self):
        self.task()
        self.host.complete_action("task-1", "write-doc", evidence={"receipt": "host-observed"})
        renderer = self.renderer()

        for event in (
            {"method": "item/agentMessage/delta", "params": {"itemId": "final", "delta": "safe final"}},
            {"method": "item/completed", "params": {"item": {"id": "final", "type": "agentMessage"}}},
            {"method": "item/agentMessage/delta", "params": {"itemId": "final", "delta": " late text"}},
            {"method": "item/completed", "params": {"item": {"id": "final", "type": "agentMessage"}}},
        ):
            self.assertEqual(renderer.consume(event), [])

        output = renderer.consume({"method": "turn/completed", "params": {"turnId": "turn-1", "status": "completed"}})

        self.assertEqual(output[0]["content"], "safe final")

    def test_renderer_ignores_replayed_completed_items_after_a_continuation(self):
        """A stale item replay cannot replace a later turn's approved final answer."""
        self.task()
        renderer = self.renderer()
        withheld = {"method": "item/completed", "params": {"item": {
            "id": "first-final", "type": "agentMessage", "phase": "final_answer",
            "text": "withheld first attempt"
        }}}

        self.assertEqual(renderer.consume(withheld), [])
        first = renderer.consume({"method": "turn/completed", "params": {
            "turnId": "turn-1", "status": "completed"
        }})
        self.assertEqual(first[0]["kind"], "continuation")

        self.host.complete_action("task-1", "write-doc", evidence={"receipt": "host-observed"})
        self.assertEqual(renderer.consume({"method": "item/completed", "params": {"item": {
            "id": "second-final", "type": "agentMessage", "phase": "final_answer",
            "text": "approved second attempt"
        }}}), [])
        self.assertEqual(renderer.consume(withheld), [])

        output = renderer.consume({"method": "turn/completed", "params": {
            "turnId": "turn-2", "status": "completed"
        }})

        self.assertEqual(output, [{
            "kind": "final", "content": "approved second attempt", "decision": "complete"
        }])

    def test_renderer_deduplicates_a_replayed_completed_turn_before_gating(self):
        """A replay after a continuation must not queue that action a second time."""
        self.task()
        renderer = self.renderer()
        completion = {"method": "turn/completed", "params": {
            "turn": {"id": "turn-1", "status": "completed"}
        }}
        self.assertEqual(renderer.consume({"method": "item/completed", "params": {"item": {
            "id": "candidate", "type": "agentMessage", "text": "safe candidate"
        }}}), [])

        first = renderer.consume(completion)
        replay = renderer.consume(completion)

        self.assertEqual(first[0]["kind"], "continuation")
        self.assertEqual(replay, [])
        audit_events = self.host.audit("task-1")
        self.assertEqual([event["type"] for event in audit_events].count("final_attempt"), 1)
        self.assertEqual([event["type"] for event in audit_events].count("continuation_queued"), 1)

    def test_renderer_never_evicts_completed_turns_before_task_completion(self):
        self.task()
        renderer = self.renderer()
        original = {"method": "turn/completed", "params": {
            "turn": {"id": "turn-original", "status": "completed"}
        }}
        self.assertEqual(renderer.consume({"method": "item/completed", "params": {"item": {
            "id": "item-original", "type": "agentMessage", "text": "safe candidate"
        }}}), [])
        renderer.consume(original)
        for index in range(129):
            self.assertEqual(renderer.consume({"method": "item/completed", "params": {"item": {
                "id": f"item-{index}", "type": "agentMessage", "text": "safe candidate"
            }}}), [])
            renderer.consume({"method": "turn/completed", "params": {
                "turn": {"id": f"turn-{index}", "status": "completed"}
            }})

        self.assertEqual(renderer.consume(original), [])
        self.assertEqual([event["type"] for event in self.host.audit("task-1")].count("final_attempt"), 130)

    def test_renderer_discards_malformed_completed_turn_ids_without_gating(self):
        self.task()
        renderer = self.renderer()

        for params in (
            {"status": "completed"},
            {"turnId": "turn-1", "turn": {"id": "turn-2", "status": "completed"}},
            {"turnId": ["turn-1"], "status": "completed"},
        ):
            with self.subTest(params=params):
                output = renderer.consume({"method": "turn/completed", "params": params})
                self.assertEqual(output, [{"kind": "progress", "event": {
                    "method": "turn/completed", "params": {"status": "completed"}
                }}])
        self.assertNotIn("final_attempt", [event["type"] for event in self.host.audit("task-1")])

    def test_failed_or_interrupted_turn_completion_discards_partial_text_without_gating(self):
        for status in ("failed", "interrupted"):
            with self.subTest(status=status):
                task_id = f"task-{status}"
                self.host.create_task(task_id, self.project, actions=[{"id": "write-doc", "operation": "write"}])
                self.host.complete_action(task_id, "write-doc", evidence={"receipt": "host-observed"})
                renderer = self.renderer(task_id)
                renderer.consume({"method": "item/agentMessage/delta", "params": {
                    "itemId": "partial", "delta": "partial text"
                }})

                output = renderer.consume({"method": "turn/completed", "params": {
                    "turn": {"status": status, "items": [{
                        "type": "agentMessage", "text": "complete agent text"
                    }]},
                    "error": {"message": "complete agent text"},
                }})

                self.assertEqual(output, [{"kind": "progress", "event": {
                    "method": "turn/completed", "params": {"status": status}
                }}])
                self.assertNotIn("complete agent text", str(output))
                self.assertNotIn("final_attempt", [
                    event["type"] for event in self.host.audit(task_id) if event["task_id"] == task_id
                ])
                renderer.consume({"method": "item/agentMessage/delta", "params": {
                    "itemId": "final", "delta": "complete text"
                }})
                renderer.consume({"method": "item/completed", "params": {
                    "item": {"id": "final", "type": "agentMessage"}
                }})
                output = renderer.consume({"method": "turn/completed", "params": {"turnId": "turn-1", "status": "completed"}})
                self.assertEqual(output[0]["content"], "complete text")

    def test_conflicting_statuses_never_release_bound_turn_but_consistent_forms_do(self):
        cases = [
            ({"status": "completed", "turn": {"status": other}}, False)
            for other in ("failed", "interrupted", "cancelled", "unknown", None, [], {})
        ] + [
            ({"status": other, "turn": {"status": "completed"}}, False)
            for other in ("failed", "interrupted", "cancelled", "unknown", None, [], {})
        ] + [
            ({"status": "completed"}, True),
            ({"turn": {"status": "completed"}}, True),
            ({"status": "completed", "turn": {"status": "completed"}}, True),
        ]
        for index, (params, release) in enumerate(cases):
            with self.subTest(params=params):
                task_id = f"status-{index}"
                self.host.create_task(task_id, self.project, [])
                renderer = client.SupervisedRenderer(self.host, task_id, thread_id="thread-1", turn_id="turn-1")
                renderer.consume({"method": "item/completed", "params": {
                    "threadId": "thread-1", "turnId": "turn-1", "item": {
                        "id": "final", "type": "agentMessage", "phase": "final_answer", "text": "private candidate",
                    },
                }})
                output = renderer.consume({"method": "turn/completed", "params": {
                    **params, "threadId": "thread-1", "turnId": "turn-1",
                }})
                self.assertEqual(any(item["kind"] == "final" for item in output), release)
                self.assertEqual(self.host.visible_messages(task_id), ["private candidate"] if release else [])
                if not release:
                    self.assertNotIn("private candidate", str(output))
                    self.assertNotIn("final_attempt", [event["type"] for event in self.host.audit(task_id)])

    def test_non_successful_turn_statuses_discard_candidate_text_without_gating(self):
        """Treat missing, cancelled, and unknown statuses as non-successful turns."""
        cases = (
            ("missing", {}, "unknown"),
            ("cancelled", {"status": "cancelled"}, "cancelled"),
            ("unknown", {"turn": {"status": "waiting-for-user"}}, "unknown"),
            ("malformed", {"status": ["completed"]}, "unknown"),
        )
        for name, params, expected_status in cases:
            with self.subTest(status=name):
                task_id = f"task-{name}"
                self.host.create_task(task_id, self.project, actions=[{"id": "write-doc", "operation": "write"}])
                self.host.complete_action(task_id, "write-doc", evidence={"receipt": "host-observed"})
                renderer = self.renderer(task_id)
                renderer.consume({"method": "item/agentMessage/delta", "params": {
                    "itemId": "candidate", "delta": "do not release"
                }})
                renderer.consume({"method": "item/completed", "params": {
                    "item": {"id": "candidate", "type": "agentMessage"}
                }})

                output = renderer.consume({"method": "turn/completed", "params": params})

                self.assertEqual(output, [{"kind": "progress", "event": {
                    "method": "turn/completed", "params": {"status": expected_status}
                }}])
                self.assertNotIn("do not release", str(output))
                self.assertNotIn("final_attempt", [event["type"] for event in self.host.audit(task_id)])


class SupervisedInstallation(unittest.TestCase):
    def test_install_and_upgrade_register_only_with_explicit_supervised_option(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            host_state = base / "host-state"
            project = base / "project"
            installer.install(project, source=installer.SOURCE, home=base / "home", supervised=True,
                              supervisor_state_dir=host_state)
            registration = supervisor.HostSupervisor(host_state).provision(project)
            self.assertEqual(registration["project"], str(project.resolve()))
            self.assertTrue(installer.read_state(project)["supervised"]["enabled"])
            self.assertFalse((project / ".agent-canvas" / "supervisor.json").exists())

            second = base / "second"
            installer.install(second, source=installer.SOURCE, home=base / "home")
            installer.upgrade(second, source=installer.SOURCE, home=base / "home", apply=True,
                              supervised=True, supervisor_state_dir=host_state)
            self.assertTrue(installer.read_state(second)["supervised"]["enabled"])
            self.assertTrue(supervisor.HostSupervisor(host_state).provision(second))
            with self.assertRaisesRegex(ValueError, "outside"):
                installer.install(base / "nested", source=installer.SOURCE, home=base / "home", supervised=True,
                                  supervisor_state_dir=base)


class MultipleProjects(unittest.TestCase):
    def test_unqualified_lookup_skips_registered_project_without_tasks_yet(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            alpha, beta = base / "alpha", base / "beta"
            alpha.mkdir()
            beta.mkdir()
            host = supervisor.HostSupervisor(base / "host-state")
            host.create_task("task-1", alpha, [{"id": "one", "operation": "write"}])
            host.provision(beta)

            self.assertEqual(host.task("task-1")["project"], str(alpha.resolve()))

    def test_unqualified_lookup_rejects_malformed_existing_tasks_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            alpha, beta = base / "alpha", base / "beta"
            alpha.mkdir()
            beta.mkdir()
            host = supervisor.HostSupervisor(base / "host-state")
            host.create_task("task-1", alpha, [{"id": "one", "operation": "write"}])
            host.provision(beta)
            (host._project_dir(beta) / "tasks").write_text("not a directory")

            with self.assertRaisesRegex(ValueError, "task state directory must be a directory"):
                host.task("task-1")

    def test_same_task_id_is_isolated_by_project(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            alpha, beta = base / "alpha", base / "beta"
            alpha.mkdir()
            beta.mkdir()
            host = supervisor.HostSupervisor(base / "host-state")
            host.create_task("task-1", alpha, [{"id": "alpha-action", "operation": "write"}])
            host.create_task("task-1", beta, [{"id": "beta-action", "operation": "write"}])
            host.complete_action("task-1", "alpha-action", evidence={"host": "alpha"}, project=alpha)
            self.assertTrue(host.gate_final("task-1", "alpha-final", "alpha done", project=alpha).release)
            beta_decision = host.gate_final("task-1", "beta-final", "beta done", project=beta)
            self.assertEqual(beta_decision.kind, "continue")
            self.assertEqual(host.visible_messages("task-1", project=alpha), ["alpha done"])
            self.assertEqual(host.visible_messages("task-1", project=beta), [])

    def test_ambiguous_legacy_task_lookup_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            alpha, beta = base / "alpha", base / "beta"
            alpha.mkdir()
            beta.mkdir()
            host = supervisor.HostSupervisor(base / "host-state")
            host.create_task("task-1", alpha, [{"id": "one", "operation": "write"}])
            host.create_task("task-1", beta, [{"id": "two", "operation": "write"}])
            with self.assertRaisesRegex(ValueError, "ambiguous"):
                host.task("task-1")


if __name__ == "__main__":
    unittest.main()
