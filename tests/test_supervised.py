"""Host-supervised task tests; all state lives in temporary host directories."""
import importlib.util
import multiprocessing
from pathlib import Path
import sys
import tempfile
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


if __name__ == "__main__":
    unittest.main()
