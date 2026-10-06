"""Host-supervised task tests; all state lives in temporary host directories."""
import importlib.util
import multiprocessing
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


supervisor = load("supervisor")
client = load("supervised_client")
installer = load("install")


def claim_from_process(state_dir, project, result):
    host = supervisor.HostSupervisor(state_dir)
    result.put(host.claim_action("task-1", "effect", "other-attempt", project=project).kind)


def pause_from_process(state_dir, project, result):
    host = supervisor.HostSupervisor(state_dir)
    host.pause("task-1", project=project)
    result.put("paused")


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

    def test_early_final_is_withheld_and_queues_exact_remaining_action(self):
        self.task()
        decision = self.host.gate_final("task-1", "attempt-1", "I am done")
        self.assertFalse(decision.release)
        self.assertEqual(decision.kind, "continue")
        self.assertEqual(decision.next_action["id"], "write-doc")
        self.assertNotIn("I am done", self.host.visible_messages("task-1"))

    def test_host_owner_can_release_a_specific_withheld_candidate(self):
        self.task()
        self.assertEqual(self.host.gate_final("task-1", "attempt-1", "I am done").kind, "continue")
        released = self.host.release_withheld_final("task-1", owner="Dennis", reason="Emergency handoff")
        self.assertTrue(released.release)
        self.assertEqual(released.message, "I am done")
        self.assertEqual(self.host.visible_messages("task-1"), ["I am done"])
        events = [event["type"] for event in self.host.audit("task-1")]
        self.assertIn("owner_override_final_released", events)

    def test_workspace_task_file_claim_cannot_complete_host_action(self):
        self.task()
        (self.project / "tasks.md").write_text("write-doc: complete\n")
        self.assertEqual(self.host.gate_final("task-1", "attempt-1", "done").kind, "continue")

    def test_restart_recovers_rejected_final_and_leased_action(self):
        self.task()
        self.host.gate_final("task-1", "attempt-1", "done")
        self.assertEqual(self.host.claim_action("task-1", "write-doc", "lease-1").kind, "dispatch")
        recovered = supervisor.HostSupervisor(self.base / "host-state")
        self.assertEqual(recovered.recover("task-1").next_action["id"], "write-doc")
        self.assertEqual(recovered.claim_action("task-1", "write-doc", "lease-2").kind, "reconcile")

    def test_valid_completion_runs_fresh_host_validator_then_releases_buffer(self):
        self.task(validators=[{"id": "check", "command": [sys.executable, "-c", "print('ok')"], "timeout_s": 2}])
        self.host.complete_action("task-1", "write-doc", evidence={"receipt": "host-observed"})
        decision = self.host.gate_final("task-1", "attempt-1", "finished")
        self.assertTrue(decision.release)
        self.assertEqual(self.host.visible_messages("task-1"), ["finished"])
        receipt = self.host.task("task-1")["evidence"]["validators"]["check"]
        self.assertEqual(receipt["exit_status"], 0)
        self.assertIn("digest", receipt)

    def test_validator_runs_from_the_registered_project_directory(self):
        (self.project / "project-marker").write_text("present")
        self.task(validators=[{
            "id": "project-cwd",
            "command": [sys.executable, "-c", "import pathlib, sys; sys.exit(not pathlib.Path('project-marker').is_file())"],
            "timeout_s": 2,
        }])
        self.host.complete_action("task-1", "write-doc", evidence={"receipt": "host-observed"})
        self.assertTrue(self.host.gate_final("task-1", "attempt-1", "finished").release)

    def test_timeout_receipt_decodes_and_bounds_partial_output(self):
        self.task(validators=[{
            "id": "timeout",
            "command": [sys.executable, "-c", "import sys, time; print('partial output', flush=True); print('x' * 20000, flush=True); time.sleep(1)"],
            "timeout_s": 0.05,
        }])
        self.host.complete_action("task-1", "write-doc", evidence={"receipt": "host-observed"})
        decision = self.host.gate_final("task-1", "attempt-1", "finished")
        self.assertEqual(decision.kind, "continue")
        receipt = self.host.task("task-1")["evidence"]["validators"]["timeout"]
        self.assertEqual(receipt["exit_status"], "timeout")
        self.assertIn("partial output", receipt["output"])
        self.assertLessEqual(len(receipt["output"].encode()), 8192)

    def test_failed_validator_queues_a_bounded_remediation_action(self):
        self.task(validators=[{
            "id": "repair-me",
            "command": [sys.executable, "-c", "import sys; print('repair this'); sys.exit(1)"],
            "timeout_s": 2,
        }])
        self.host.complete_action("task-1", "write-doc", evidence={"receipt": "host-observed"})
        decision = self.host.gate_final("task-1", "attempt-1", "finished")
        self.assertEqual(decision.kind, "continue")
        self.assertEqual(decision.next_action["id"], "validator:repair-me")
        self.assertEqual(decision.next_action["exit_status"], 1)
        self.assertIn("repair this", decision.next_action["output"])

    def test_project_relative_validator_receipt_binds_the_executable_that_ran(self):
        binary = self.project / "bin" / "check"
        binary.parent.mkdir()
        binary.write_text("#!/bin/sh\nprintf '#!/bin/sh\\nexit 1\\n' > ./bin/check\necho checked\n")
        binary.chmod(0o700)
        expected_digest = supervisor.hashlib.sha256(binary.read_bytes()).hexdigest()
        self.task(validators=[{"id": "relative-binary", "command": ["./bin/check"], "timeout_s": 2}])
        self.host.complete_action("task-1", "write-doc", evidence={"receipt": "host-observed"})
        self.assertTrue(self.host.gate_final("task-1", "attempt-1", "finished").release)
        receipt = self.host.task("task-1")["evidence"]["validators"]["relative-binary"]
        self.assertEqual(receipt["binary_digest"], expected_digest)

    def test_absolute_workspace_validator_receipt_binds_the_executable_that_ran(self):
        binary = self.project / "bin" / "check"
        binary.parent.mkdir()
        binary.write_text("#!/bin/sh\nprintf '#!/bin/sh\\nexit 1\\n' > ./bin/check\necho checked\n")
        binary.chmod(0o700)
        expected_digest = supervisor.hashlib.sha256(binary.read_bytes()).hexdigest()
        self.task(validators=[{"id": "absolute-binary", "command": [str(binary)], "timeout_s": 2}])
        self.host.complete_action("task-1", "write-doc", evidence={"receipt": "host-observed"})
        self.assertTrue(self.host.gate_final("task-1", "attempt-1", "finished").release)
        receipt = self.host.task("task-1")["evidence"]["validators"]["absolute-binary"]
        self.assertEqual(receipt["binary_digest"], expected_digest)

    def test_pause_interrupts_a_validator_before_final_release(self):
        self.task(validators=[{
            "id": "slow",
            "command": [sys.executable, "-c", "import pathlib, time; pathlib.Path('validator-start').touch(); time.sleep(0.2)"],
            "timeout_s": 2,
        }])
        self.host.complete_action("task-1", "write-doc", evidence={"receipt": "host-observed"})
        result = {}
        thread = threading.Thread(target=lambda: result.setdefault(
            "decision", self.host.gate_final("task-1", "attempt-1", "finished")))
        thread.start()
        for _ in range(40):
            if (self.project / "validator-start").exists():
                break
            time.sleep(0.01)
        self.assertTrue((self.project / "validator-start").exists())
        self.host.pause("task-1")
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result["decision"].kind, "paused")
        self.assertEqual(self.host.visible_messages("task-1"), [])

    def test_pause_wins_over_failed_validator_remediation(self):
        self.task(validators=[{
            "id": "slow-failure",
            "command": [sys.executable, "-c", "import pathlib, sys, time; pathlib.Path('validator-start').touch(); time.sleep(0.2); sys.exit(1)"],
            "timeout_s": 2,
        }])
        self.host.complete_action("task-1", "write-doc", evidence={"receipt": "host-observed"})
        result = {}
        thread = threading.Thread(target=lambda: result.setdefault(
            "decision", self.host.gate_final("task-1", "attempt-1", "finished")))
        thread.start()
        for _ in range(40):
            if (self.project / "validator-start").exists():
                break
            time.sleep(0.01)
        self.assertTrue((self.project / "validator-start").exists())
        self.host.pause("task-1")
        thread.join(timeout=2)
        self.assertEqual(result["decision"].kind, "paused")

    def test_resume_does_not_clear_a_pending_pause_request(self):
        self.task()
        self.host._request_interrupt("task-1", "paused")
        with self.assertRaisesRegex(ValueError, "pending"):
            self.host.resume("task-1")
        self.assertEqual(self.host.gate_final("task-1", "attempt-1", "finished").kind, "paused")

    def test_resume_waiting_for_task_lock_does_not_block_gate_interrupt_check(self):
        self.task()
        self.host.pause("task-1")
        resumed = threading.Thread(target=lambda: self.host.resume("task-1"))
        with self.host._locked_task("task-1"):
            resumed.start()
            time.sleep(0.05)
            observed = {}
            checker = threading.Thread(target=lambda: observed.setdefault(
                "status", self.host._interrupt_status("task-1")))
            checker.start()
            checker.join(timeout=0.5)
            self.assertFalse(checker.is_alive())
            self.assertEqual(observed["status"], "paused")
        resumed.join(timeout=1)
        self.assertFalse(resumed.is_alive())

    def test_resume_cannot_clear_a_pending_cancellation(self):
        self.task()
        self.host.pause("task-1")
        self.host._request_interrupt("task-1", "cancelled")
        with self.assertRaisesRegex(ValueError, "cancellation"):
            self.host.resume("task-1")
        decision = self.host.gate_final("task-1", "attempt-1", "finished")
        self.assertEqual(decision.kind, "cancelled")
        self.assertFalse(decision.release)

    def test_pause_cannot_overwrite_a_pending_cancellation(self):
        self.task()
        self.host.pause("task-1")
        self.host._request_interrupt("task-1", "cancelled")
        self.host._request_interrupt("task-1", "paused")
        with self.assertRaisesRegex(ValueError, "cancellation"):
            self.host.resume("task-1")
        decision = self.host.gate_final("task-1", "attempt-1", "finished")
        self.assertEqual(decision.kind, "cancelled")
        self.assertFalse(decision.release)

    def test_genuine_blocker_is_released_only_after_independent_actions_finish(self):
        self.task(blockers=[{"id": "owner-choice", "owner_action": "Choose the deployment region."}])
        self.assertEqual(self.host.gate_final("task-1", "attempt-1", "blocked").kind, "continue")
        self.host.complete_action("task-1", "write-doc", evidence={"receipt": "host-observed"})
        decision = self.host.gate_final("task-1", "attempt-2", "blocked")
        self.assertTrue(decision.release)
        self.assertEqual(decision.kind, "blocker")
        self.assertEqual(self.host.visible_messages("task-1"), ["Choose the deployment region."])

    def test_pause_and_cancel_interrupt_and_prevent_automatic_continuation(self):
        self.task()
        self.host.pause("task-1")
        self.assertEqual(self.host.gate_final("task-1", "attempt-1", "done").kind, "paused")
        self.host.resume("task-1")
        self.host.cancel("task-1")
        self.assertEqual(self.host.gate_final("task-1", "attempt-2", "done").kind, "cancelled")

    def test_crash_after_dispatch_requires_reconciliation_not_duplicate_dispatch(self):
        self.task(actions=[{"id": "send", "operation": "write", "side_effect": True}])
        self.assertEqual(self.host.claim_action("task-1", "send", "attempt-1").kind, "dispatch")
        restarted = supervisor.HostSupervisor(self.base / "host-state")
        self.assertEqual(restarted.claim_action("task-1", "send", "attempt-2").kind, "reconcile")
        restarted.reconcile_action("task-1", "send", succeeded=True, receipt={"provider_id": "one"})
        self.assertEqual(restarted.task("task-1")["actions"]["send"]["status"], "complete")

    def test_continuation_cannot_broaden_default_prohibited_operations(self):
        self.task()
        result = self.host.request_operation("task-1", "push")
        self.assertEqual(result.kind, "blocker")
        self.assertIn("explicit owner authorization", result.message)
        self.assertNotIn("push", self.host.task("task-1")["authorization"]["permitted_operations"])

    def test_unauthorized_action_cannot_be_leased_or_dispatched(self):
        self.task(actions=[{"id": "merge-release", "operation": "merge"}])
        decision = self.host.claim_action("task-1", "merge-release", "attempt-1")
        self.assertEqual(decision.kind, "blocker")
        self.assertEqual(self.host.task("task-1")["actions"]["merge-release"]["status"], "pending")
        self.assertEqual(self.host.gate_final("task-1", "final-1", "done").kind, "blocker")
        events = self.host.audit("task-1")
        self.assertIn("authorization_blocked", [event["type"] for event in events])
        self.assertNotIn("action_dispatched", [event["type"] for event in events])

    def test_child_evidence_must_be_joined_before_parent_finalizes(self):
        self.task(actions=[{"id": "child", "operation": "delegate", "child_task_id": "child-1"}])
        self.assertEqual(self.host.gate_final("task-1", "attempt-1", "done").kind, "continue")
        self.host.join_child("task-1", "child", "child-1", evidence={"validator": "passed"})
        self.assertTrue(self.host.gate_final("task-1", "attempt-2", "done").release)

    def test_audit_log_records_all_gate_decisions_and_renderer_never_leaks_deltas(self):
        self.task()
        renderer = client.SupervisedRenderer(self.host, "task-1")
        self.assertEqual(renderer.consume({"method": "item/agentMessage/delta", "params": {"delta": "secret final"}}), [])
        self.assertEqual(renderer.consume({"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": " completed final"}}}), [])
        output = renderer.consume({"method": "turn/completed", "params": {"turn": {"status": "completed"}}})
        self.assertEqual(output[0]["kind"], "continuation")
        self.assertNotIn("secret final", str(output))
        self.assertNotIn("completed final", str(output))
        events = [event["type"] for event in self.host.audit("task-1")]
        self.assertIn("final_attempt", events)
        self.assertIn("continuation_queued", events)

    def test_renderer_does_not_gate_failed_or_interrupted_turns(self):
        self.task()
        for status in ("failed", "interrupted"):
            renderer = client.SupervisedRenderer(self.host, "task-1")
            renderer.consume({"method": "item/agentMessage/delta", "params": {"delta": "partial final"}})
            result = renderer.consume({"method": "turn/completed", "params": {"turn": {"status": status}}})
            self.assertEqual(result[0]["kind"], "turn_incomplete")
        self.assertEqual(self.host.visible_messages("task-1"), [])
        self.assertNotIn("final_attempt", [event["type"] for event in self.host.audit("task-1")])

    def test_renderer_releases_only_the_buffered_completed_agent_message(self):
        self.task()
        self.host.complete_action("task-1", "write-doc", evidence={"receipt": "host-observed"})
        renderer = client.SupervisedRenderer(self.host, "task-1")
        renderer.consume({"method": "item/agentMessage/delta", "params": {"delta": "streamed duplicate"}})
        self.assertEqual(renderer.consume({"method": "item/completed", "params": {
            "item": {"type": "agentMessage", "text": "authoritative final"}}}), [])
        released = renderer.consume({"method": "turn/completed", "params": {"turn": {"status": "completed"}}})
        self.assertEqual(released, [{"kind": "final", "content": "authoritative final", "decision": "complete"}])


class SupervisedInstallation(unittest.TestCase):
    def test_supervised_install_imports_a_root_owner_override_as_a_host_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project = base / "project"
            project.mkdir()
            override = project / ".owner-override"
            override.write_text('OWNER_OVERRIDE="pause,bypass-review" # host import\n')
            state_dir = base / "host-state"

            installer.install(project, source=installer.SOURCE, home=base / "home", apply=True,
                              supervised=True, supervisor_state_dir=state_dir)

            host = supervisor.HostSupervisor(state_dir)
            registration = host.provision(project)
            self.assertEqual(registration["owner_override"]["modes"], ["bypass-review", "pause"])
            self.assertEqual(registration["owner_override"]["source_digest"],
                             supervisor.hashlib.sha256(override.read_bytes()).hexdigest())
            override.write_text("OWNER_OVERRIDE=\n")
            self.assertEqual(host.provision(project)["owner_override"]["modes"], ["bypass-review", "pause"])
            task = host.create_task("task-1", project, [{"id": "write-doc", "operation": "write"}])
            self.assertEqual(task["status"], "paused")

    def test_supervised_upgrade_imports_only_the_target_root_owner_override(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project = base / "project"
            state_dir = base / "host-state"
            installer.install(project, source=installer.SOURCE, home=base / "home")
            (project / ".owner-override").write_text("reset\n")

            installer.upgrade(project, source=installer.SOURCE, home=base / "home", apply=True,
                              supervised=True, supervisor_state_dir=state_dir)

            registration = supervisor.HostSupervisor(state_dir).provision(project)
            self.assertEqual(registration["owner_override"]["modes"], ["reset"])

    def test_imported_reset_cancels_existing_nonterminal_host_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project = base / "project"
            state_dir = base / "host-state"
            installer.install(project, source=installer.SOURCE, home=base / "home", supervised=True,
                              supervisor_state_dir=state_dir)
            host = supervisor.HostSupervisor(state_dir)
            host.create_task("task-1", project, [{"id": "write-doc", "operation": "write"}])
            (project / ".owner-override").write_text("OWNER_OVERRIDE=reset\n")

            installer.upgrade(project, source=installer.SOURCE, home=base / "home", apply=True,
                              supervised=True, supervisor_state_dir=state_dir)

            self.assertEqual(host.task("task-1", project=project)["status"], "cancelled")
            self.assertIn("owner_override_reset", [event["type"] for event in host.audit("task-1", project=project)])

    def test_supervised_install_rejects_a_symlinked_owner_override(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project = base / "project"
            project.mkdir()
            outside = base / "outside-override"
            outside.write_text("OWNER_OVERRIDE=pause\n")
            (project / ".owner-override").symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "symlink"):
                installer.install(project, source=installer.SOURCE, home=base / "home", apply=True,
                                  supervised=True, supervisor_state_dir=base / "host-state")

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
    def test_concurrent_task_creation_is_serialized_before_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project = base / "project"
            project.mkdir()
            host = supervisor.HostSupervisor(base / "host-state")
            host.provision(project)
            lock_path = host._project_dir(project) / "tasks" / "task-1.json.lock"
            lock_path.parent.mkdir(parents=True)
            result = {}
            with lock_path.open("a", encoding="utf-8") as lock:
                supervisor.fcntl.flock(lock.fileno(), supervisor.fcntl.LOCK_EX)
                creator = threading.Thread(target=lambda: result.setdefault(
                    "task", host.create_task("task-1", project, [{"id": "effect", "operation": "write"}])))
                creator.start()
                time.sleep(0.05)
                self.assertNotIn("task", result)
            creator.join(timeout=1)
            self.assertFalse(creator.is_alive())
            self.assertEqual(result["task"]["actions"]["effect"]["status"], "pending")
            self.assertEqual(host.claim_action("task-1", "effect", "first", project=project).kind, "dispatch")
            self.assertEqual(host.claim_action("task-1", "effect", "second", project=project).kind, "reconcile")
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

    def test_task_id_path_traversal_is_rejected_before_project_lookup(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project = base / "project"
            project.mkdir()
            host = supervisor.HostSupervisor(base / "host-state")
            host.create_task("task-1", project, [{"id": "one", "operation": "write"}])
            with self.assertRaisesRegex(ValueError, "task_id"):
                host.task("../task-1", project=project)

    def test_host_state_inside_or_enclosing_workspace_is_rejected_by_supervisor(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project = base / "project"
            project.mkdir()
            with self.assertRaisesRegex(ValueError, "outside"):
                supervisor.HostSupervisor(project / "host-state").provision(project)
            with self.assertRaisesRegex(ValueError, "outside"):
                supervisor.HostSupervisor(base).provision(project)

    def test_interprocess_lease_lock_allows_only_one_side_effect_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project = base / "project"
            project.mkdir()
            host = supervisor.HostSupervisor(base / "host-state")
            host.create_task("task-1", project, [{"id": "effect", "operation": "write", "side_effect": True}])
            context = multiprocessing.get_context("fork")
            result = context.Queue()
            with host._locked_task("task-1", project) as task:
                child = context.Process(target=claim_from_process, args=(base / "host-state", project, result))
                child.start()
                self.assertTrue(result.empty())
            child.join(timeout=2)
            self.assertEqual(child.exitcode, 0)
            self.assertEqual(result.get(timeout=1), "dispatch")
            self.assertEqual(host.claim_action("task-1", "effect", "later-attempt", project=project).kind, "reconcile")

    def test_terminal_final_or_blocker_is_not_released_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project = base / "project"
            project.mkdir()
            host = supervisor.HostSupervisor(base / "host-state")
            host.create_task("task-1", project, [{"id": "done", "operation": "write"}])
            host.complete_action("task-1", "done", evidence={"host": "observed"})
            self.assertTrue(host.gate_final("task-1", "first", "finished", project=project).release)
            duplicate = host.gate_final("task-1", "second", "finished again", project=project)
            self.assertFalse(duplicate.release)
            self.assertEqual(host.visible_messages("task-1", project=project), ["finished"])

    def test_pause_and_resume_cannot_reopen_a_terminal_task(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project = base / "project"
            project.mkdir()
            host = supervisor.HostSupervisor(base / "host-state")
            host.create_task("task-1", project, [{"id": "done", "operation": "write"}])
            host.complete_action("task-1", "done", evidence={"host": "observed"})
            self.assertTrue(host.gate_final("task-1", "first", "finished", project=project).release)
            host.pause("task-1", project=project)
            with self.assertRaisesRegex(ValueError, "terminal"):
                host.resume("task-1", project=project)
            host.cancel("task-1", project=project)
            self.assertEqual(host.task("task-1", project=project)["status"], "complete")
            self.assertFalse(host.gate_final("task-1", "second", "duplicate", project=project).release)

    def test_pause_uses_the_same_lock_as_final_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project = base / "project"
            project.mkdir()
            host = supervisor.HostSupervisor(base / "host-state")
            host.create_task("task-1", project, [{"id": "done", "operation": "write"}])
            host.complete_action("task-1", "done", evidence={"host": "observed"})
            context = multiprocessing.get_context("fork")
            result = context.Queue()
            with host._locked_task("task-1", project):
                child = context.Process(target=pause_from_process, args=(base / "host-state", project, result))
                child.start()
                self.assertTrue(result.empty())
            child.join(timeout=2)
            self.assertEqual(child.exitcode, 0)
            self.assertEqual(result.get(timeout=1), "paused")
            decision = host.gate_final("task-1", "after-pause", "finished", project=project)
            self.assertEqual(decision.kind, "paused")
            self.assertFalse(decision.release)


if __name__ == "__main__":
    unittest.main()
