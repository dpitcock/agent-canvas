"""Unit tests for the standalone, host-owned container execution boundary."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


def load_executor():
    spec = importlib.util.spec_from_file_location(
        "container_executor", Path(__file__).resolve().parents[1] / "scripts" / "container_executor.py"
    )
    module = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


executor = load_executor()


class StagingTests(unittest.TestCase):
    def test_stages_declared_regular_file_from_pinned_root_after_path_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project, replacement = base / "project", base / "replacement"
            project.mkdir()
            replacement.mkdir()
            (project / "input.txt").write_text("authorized")
            (replacement / "input.txt").write_text("attacker")
            identity = executor.ProjectIdentity.capture(project)
            pinned = executor.PinnedProject.open(project, identity)
            project.rename(base / "old-project")
            replacement.rename(project)
            with tempfile.TemporaryDirectory() as staging:
                snapshot = executor.stage_inputs(pinned, ["input.txt"], Path(staging), max_bytes=1024, max_files=1)
                self.assertEqual((snapshot.root / "input.txt").read_text(), "authorized")
                self.assertNotEqual(snapshot.root, project)

    def test_rejects_traversal_links_special_files_and_hard_linked_files(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as staging:
            project = Path(directory) / "project"
            project.mkdir()
            (project / "safe.txt").write_text("safe")
            (project / "hard.txt").hardlink_to(project / "safe.txt")
            (project / "link.txt").symlink_to("safe.txt")
            os.mkfifo(project / "blocked.fifo")
            pinned = executor.PinnedProject.open(project, executor.ProjectIdentity.capture(project))
            for declared in ("../safe.txt", "link.txt", "hard.txt", "blocked.fifo"):
                with self.subTest(declared=declared):
                    with self.assertRaises(executor.InputRejected):
                        executor.stage_inputs(pinned, [declared], Path(staging), max_bytes=1024, max_files=2)

    def test_rejects_oversized_inputs_and_binds_a_stable_digest(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as staging:
            project = Path(directory) / "project"
            project.mkdir()
            (project / "large.txt").write_bytes(b"x" * 32)
            pinned = executor.PinnedProject.open(project, executor.ProjectIdentity.capture(project))
            with self.assertRaises(executor.InputRejected):
                executor.stage_inputs(pinned, ["large.txt"], Path(staging), max_bytes=31, max_files=1)
            snapshot = executor.stage_inputs(pinned, ["large.txt"], Path(staging), max_bytes=32, max_files=1)
            self.assertEqual(len(snapshot.digest), 64)
            self.assertEqual(snapshot.files, ("large.txt",))

    def test_stages_nested_inputs_with_traversable_ancestors_under_restrictive_umask(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as staging:
            project = Path(directory) / "project"
            source = project / "nested" / "deeper" / "input.txt"
            source.parent.mkdir(parents=True)
            source.write_text("authorized")
            pinned = executor.PinnedProject.open(project, executor.ProjectIdentity.capture(project))
            previous_umask = os.umask(0o077)
            try:
                snapshot = executor.stage_inputs(
                    pinned, ["nested/deeper/input.txt"], Path(staging), max_bytes=1024, max_files=1
                )
            finally:
                os.umask(previous_umask)
            for ancestor in (snapshot.root, snapshot.root / "nested", snapshot.root / "nested" / "deeper"):
                with self.subTest(ancestor=ancestor):
                    self.assertTrue(ancestor.stat().st_mode & 0o001)

    def test_rejected_stage_reports_cleanup_failure(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as staging:
            project = Path(directory) / "project"
            project.mkdir()
            (project / "input.txt").write_text("input")
            with executor.PinnedProject.open(project, executor.ProjectIdentity.capture(project)) as pinned:
                with mock.patch.object(executor.shutil, "rmtree", side_effect=OSError("busy")):
                    with self.assertRaises(executor.ConfigurationError):
                        executor.stage_inputs(pinned, ["missing.txt"], Path(staging), max_bytes=1024, max_files=1)


class RuntimeValidationTests(unittest.TestCase):
    def test_pre_cancelled_execution_does_not_create_or_start_a_container(self):
        """Removing the pre-create cancellation check would launch Docker."""
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as staging:
            project = Path(directory) / "project"
            project.mkdir()
            (project / "input.txt").write_text("input")
            request = executor.ExecutionRequest(
                action_id="cancelled", attempt_id="before-create", project=project,
                image="example/tool@sha256:" + "a" * 64, command=["tool"], inputs=["input.txt"],
            )
            cancellation = executor.threading.Event()
            cancellation.set()
            with mock.patch.object(executor.ContainerExecutor, "_check_runtime", return_value=("27", "sha256:image")), \
                 mock.patch.object(executor.subprocess, "Popen", side_effect=AssertionError("container must not start")), \
                 mock.patch.object(executor.subprocess, "run", side_effect=AssertionError("container must not be created")):
                receipt = executor.ContainerExecutor.run(
                    request, executor.ProjectIdentity.capture(project), Path(staging), cancellation=cancellation
                )
            self.assertTrue(receipt.cancelled)
            self.assertEqual(receipt.exit_status, "cancelled")
            self.assertFalse(receipt.timed_out)
            self.assertTrue(receipt.verify())
            self.assertEqual(list(Path(staging).iterdir()), [])

    def test_cancellation_after_create_removes_container_without_starting_it(self):
        """Removing the pre-start cancellation check would execute the created container."""
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as staging:
            project = Path(directory) / "project"
            project.mkdir()
            (project / "input.txt").write_text("input")
            request = executor.ExecutionRequest(
                action_id="cancelled", attempt_id="before-start", project=project,
                image="example/tool@sha256:" + "a" * 64, command=["tool"], inputs=["input.txt"],
            )
            cancellation = executor.threading.Event()

            def docker_run(argv, **_kwargs):
                if argv[1] == "create":
                    cancellation.set()
                    return mock.Mock(returncode=0)
                if argv[1:3] == ["rm", "-f"]:
                    return mock.Mock(returncode=0)
                raise AssertionError(f"unexpected Docker command: {argv}")

            with mock.patch.object(executor.ContainerExecutor, "_check_runtime", return_value=("27", "sha256:image")), \
                 mock.patch.object(executor.subprocess, "run", side_effect=docker_run), \
                 mock.patch.object(executor.subprocess, "Popen", side_effect=AssertionError("container must not start")):
                receipt = executor.ContainerExecutor.run(
                    request, executor.ProjectIdentity.capture(project), Path(staging), cancellation=cancellation
                )
            self.assertTrue(receipt.cancelled)
            self.assertEqual(receipt.exit_status, "cancelled")
            self.assertTrue(receipt.verify())
            self.assertEqual(list(Path(staging).iterdir()), [])

    def test_requires_digest_pinned_image_and_rejects_host_fallback(self):
        with self.assertRaises(executor.ConfigurationError):
            executor.ExecutionRequest(action_id="a", attempt_id="one", project=Path("/tmp/project"),
                                      image="python:3.14", command=["true"], inputs=[])
        request = executor.ExecutionRequest(action_id="a", attempt_id="one", project=Path("/tmp/project"),
                                            image="example.invalid/tool@sha256:" + "a" * 64,
                                            command=["true"], inputs=[])
        self.assertEqual(request.command, ("true",))

    def test_receipt_is_bound_to_attempt_command_image_and_snapshot(self):
        receipt = executor.ExecutionReceipt.build(action_id="action", attempt_id="attempt", input_digest="1" * 64,
                                                  command=("tool", "--check"), image="example/tool@sha256:" + "2" * 64,
                                                  runtime="docker", runtime_version="27", exit_status=0,
                                                  timed_out=False, cancelled=False, output="ok", output_truncated=False)
        self.assertTrue(receipt.verify())
        self.assertFalse(receipt.matches(action_id="action", attempt_id="other", input_digest="1" * 64,
                                         command=("tool", "--check"), image="example/tool@sha256:" + "2" * 64))

    def test_runtime_plan_has_no_host_mounts_and_requires_all_isolation_controls(self):
        image = "example/tool@sha256:" + "a" * 64
        request = executor.ExecutionRequest(action_id="action", attempt_id="attempt", project=Path("/tmp/project"),
                                            image=image, command=["tool", "--check"], inputs=[])
        with tempfile.TemporaryDirectory() as directory:
            staged = Path(directory) / "inputs"
            staged.mkdir()
            plan = executor.ContainerExecutor.plan(request, staged, "agent-canvas-test")
        self.assertIn("--network", plan)
        self.assertIn("none", plan)
        self.assertIn("--read-only", plan)
        self.assertIn("--cap-drop", plan)
        self.assertIn("ALL", plan)
        self.assertIn("no-new-privileges", " ".join(plan))
        mounts = [plan[index + 1] for index, value in enumerate(plan[:-1]) if value == "--mount"]
        self.assertEqual(len(mounts), 1)
        self.assertIn("dst=/inputs,readonly", mounts[0])
        self.assertNotIn(str(request.project), " ".join(plan))
        self.assertIn("--pull=never", plan)
        self.assertIn("--log-driver", plan)
        self.assertIn("none", plan)

    def test_rejects_unbounded_or_invalid_resource_limits(self):
        image = "example/tool@sha256:" + "a" * 64
        for changes in ({"pids": -1}, {"pids": 257}, {"memory": "0m"}, {"memory": "2g"}, {"cpus": "0"}, {"cpus": "4.1"}):
            with self.subTest(changes=changes):
                with self.assertRaises(executor.ConfigurationError):
                    executor.ExecutionRequest(action_id="action", attempt_id="attempt", project=Path("/tmp/project"),
                                              image=image, command=["true"], inputs=[], **changes)

    def test_container_removal_failure_is_reported_without_skipping_stage_cleanup(self):
        with mock.patch.object(executor.subprocess, "run", side_effect=executor.subprocess.TimeoutExpired("docker", 10)):
            self.assertFalse(executor.ContainerExecutor._remove_container("agent-canvas-test"))

    def test_container_cleanup_removes_anonymous_volumes(self):
        """Dropping -v would leave image-declared anonymous volumes behind."""
        removed = mock.Mock(returncode=0)
        with mock.patch.object(executor.subprocess, "run", return_value=removed) as run:
            self.assertTrue(executor.ContainerExecutor._remove_container("agent-canvas-test"))
        self.assertEqual(run.call_args.args[0], ["docker", "rm", "-f", "-v", "agent-canvas-test"])

    def test_container_cleanup_accepts_confirmed_absence_after_create_ambiguity(self):
        failed_remove = mock.Mock(returncode=1)
        absent = mock.Mock(returncode=1, stderr=b"Error: No such container: agent-canvas-test\n")
        with mock.patch.object(executor.subprocess, "run", side_effect=[failed_remove, absent]) as run:
            self.assertTrue(executor.ContainerExecutor._remove_container("agent-canvas-test"))
        self.assertEqual(run.call_args_list[1].args[0][-2:], ["inspect", "agent-canvas-test"])

    def test_container_cleanup_accepts_inspect_missing_object_diagnostic(self):
        failed_remove = mock.Mock(returncode=1)
        absent = mock.Mock(returncode=1, stderr=b"Error: No such object: agent-canvas-test\n")
        with mock.patch.object(executor.subprocess, "run", side_effect=[failed_remove, absent]):
            self.assertTrue(executor.ContainerExecutor._remove_container("agent-canvas-test"))

    def test_container_cleanup_rejects_ambiguous_inspect_error(self):
        failed_remove = mock.Mock(returncode=1)
        daemon_error = mock.Mock(returncode=1, stderr=b"Cannot connect to the Docker daemon")
        with mock.patch.object(executor.subprocess, "run", side_effect=[failed_remove, daemon_error]):
            self.assertFalse(executor.ContainerExecutor._remove_container("agent-canvas-test"))

    def test_stage_removal_failure_is_explicit(self):
        with mock.patch.object(executor.shutil, "rmtree", side_effect=OSError("busy")):
            self.assertFalse(executor.ContainerExecutor._remove_stage(Path("/host-owned/stage")))


@unittest.skipUnless(os.environ.get("AGENT_CANVAS_CONTAINER_IMAGE"), "set a digest-pinned container image to run integration tests")
class DockerIntegrationTests(unittest.TestCase):
    """Exercises the Docker Desktop containment controls, not hostile-host isolation."""

    def test_staged_input_is_read_only_and_network_and_socket_are_unavailable(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as staging:
            project = Path(directory) / "project"
            project.mkdir()
            (project / "fixture.txt").write_text("trusted stage")
            request = executor.ExecutionRequest(
                action_id="integration", attempt_id="one", project=project,
                image=os.environ["AGENT_CANVAS_CONTAINER_IMAGE"], inputs=["fixture.txt"],
                command=["/bin/sh", "-c", "test \"$(id -u)\" = 65532 && test \"$(cat fixture.txt)\" = \"trusted stage\" && test ! -w fixture.txt && "
                         "test ! -e /var/run/docker.sock && test -w /outputs && ! wget -q -T 1 http://1.1.1.1"],
                timeout_s=10,
            )
            receipt = executor.ContainerExecutor.run(
                request, executor.ProjectIdentity.capture(project), Path(staging)
            )
            self.assertEqual(receipt.exit_status, 0, receipt.output)
            self.assertTrue(receipt.verify())


if __name__ == "__main__":
    unittest.main()
