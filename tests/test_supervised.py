"""Host-supervised task tests; all state lives in temporary host directories."""
import importlib.util
import multiprocessing
from pathlib import Path
import sys
import tempfile
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

    def test_valid_completion_runs_fresh_host_validator_then_releases_buffer(self):
        self.task(validators=[{"id": "check", "command": [sys.executable, "-c", "print('ok')"], "timeout_s": 2}])
        self.host.complete_action("task-1", "write-doc", evidence={"receipt": "host-observed"})
        decision = self.host.gate_final("task-1", "attempt-1", "finished")
        self.assertTrue(decision.release)
        self.assertEqual(self.host.visible_messages("task-1"), ["finished"])
        receipt = self.host.task("task-1")["evidence"]["validators"]["check"]
        self.assertEqual(receipt["exit_status"], 0)
        self.assertIn("digest", receipt)

    def test_genuine_blocker_is_released_only_after_independent_actions_finish(self):
        self.task(blockers=[{"id": "owner-choice", "owner_action": "Choose the deployment region."}])
        self.assertEqual(self.host.gate_final("task-1", "attempt-1", "blocked").kind, "continue")
        self.host.complete_action("task-1", "write-doc", evidence={"receipt": "host-observed"})
        decision = self.host.gate_final("task-1", "attempt-2", "blocked")
        self.assertTrue(decision.release)
        self.assertEqual(decision.kind, "blocker")
        self.assertEqual(self.host.visible_messages("task-1"), ["Choose the deployment region."])

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
        """A timeout-compatible byte stream still yields a text receipt and digest."""
        timeout = supervisor.subprocess.TimeoutExpired(["ignored"], 1, output=b"validator output")
        with patch.object(supervisor.subprocess, "run", side_effect=timeout):
            receipt = self.host._validator_receipt({"id": "check", "command": ["ignored"]})

        self.assertEqual(receipt["output"], "validator output")
        self.assertEqual(receipt["digest"], "af0c829e3106013e4ef9555521848126776f8a7054fc42e1b64b94dbc4627de1")

    def test_continuation_cannot_broaden_default_prohibited_operations(self):
        self.task()
        result = self.host.request_operation("task-1", "push")
        self.assertEqual(result.kind, "blocker")
        self.assertIn("explicit owner authorization", result.message)
        self.assertNotIn("push", self.host.task("task-1")["authorization"]["permitted_operations"])

    def test_child_evidence_must_be_joined_before_parent_finalizes(self):
        self.task(actions=[{"id": "child", "operation": "delegate", "child_task_id": "child-1"}])
        self.assertEqual(self.host.gate_final("task-1", "attempt-1", "done").kind, "continue")
        self.host.join_child("task-1", "child", "child-1", evidence={"validator": "passed"})
        self.assertTrue(self.host.gate_final("task-1", "attempt-2", "done").release)

    def test_audit_log_records_all_gate_decisions_and_renderer_never_leaks_deltas(self):
        self.task()
        renderer = client.SupervisedRenderer(self.host, "task-1")
        self.assertEqual(renderer.consume({"method": "item/agentMessage/delta", "params": {"delta": "secret final"}}), [])
        output = renderer.consume({"method": "turn/completed", "params": {}})
        self.assertEqual(output[0]["kind"], "continuation")
        events = [event["type"] for event in self.host.audit("task-1")]
        self.assertIn("final_attempt", events)
        self.assertIn("continuation_queued", events)

    def test_renderer_withholds_completed_agent_message_notifications_until_the_gate(self):
        self.task()
        renderer = client.SupervisedRenderer(self.host, "task-1")

        for event in (
            {"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": "secret final"}}},
            {"method": "item/agentMessage/completed", "params": {"text": "another secret final"}},
        ):
            with self.subTest(method=event["method"]):
                self.assertEqual(renderer.consume(event), [])
        output = renderer.consume({"method": "turn/completed", "params": {}})
        self.assertEqual(output[0]["kind"], "continuation")
        self.assertNotIn("secret final", str(output))

    def test_failed_or_interrupted_turn_completion_discards_partial_text_without_gating(self):
        for status in ("failed", "interrupted"):
            with self.subTest(status=status):
                task_id = f"task-{status}"
                self.host.create_task(task_id, self.project, actions=[{"id": "write-doc", "operation": "write"}])
                self.host.complete_action(task_id, "write-doc", evidence={"receipt": "host-observed"})
                renderer = client.SupervisedRenderer(self.host, task_id)
                renderer.consume({"method": "item/agentMessage/delta", "params": {"delta": "partial text"}})

                output = renderer.consume({"method": "turn/completed", "params": {"status": status}})

                self.assertEqual(output[0]["kind"], "progress")
                self.assertNotIn("final_attempt", [
                    event["type"] for event in self.host.audit(task_id) if event["task_id"] == task_id
                ])
                renderer.consume({"method": "item/agentMessage/delta", "params": {"delta": "complete text"}})
                output = renderer.consume({"method": "turn/completed", "params": {"status": "completed"}})
                self.assertEqual(output[0]["content"], "complete text")


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
