"""Publication cleanup and normalized Docker CPU arguments."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_install import installer
from test_container_executor import executor


class PublicationCpu(unittest.TestCase):
    def test_cpu_values_become_valid_cli_strings(self):
        with tempfile.TemporaryDirectory() as tmp:
            for value, expected in ((1, "1"), (0.5, "0.5"), ("1e0", "1")):
                with self.subTest(value=value):
                    request = executor.ExecutionRequest(action_id="a", attempt_id="b", project=Path(tmp),
                        image="example/tool@sha256:" + "a" * 64, command=["true"], inputs=[], cpus=value)
                    plan = executor.ContainerExecutor.plan(request, tmp, "test", runtime="/usr/bin/docker")
                    self.assertEqual(plan[plan.index("--cpus") + 1], expected)

    def test_published_pack_keeps_provenance_after_cleanup_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "project"
            def download(destination, revision):
                skill = destination / "skills/example"
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text("example")
            remove = installer.shutil.rmtree
            def fail_staging_cleanup(path, *args, **kwargs):
                if str(path).startswith(".agent-canvas-pack-"):
                    raise OSError("cleanup failed")
                return remove(path, *args, **kwargs)
            with patch.object(installer.shutil, "rmtree", side_effect=fail_staging_cleanup):
                _, actions = installer.install(root, skills=True, home=base / "home", downloader=download)
            self.assertFalse(any(a.startswith("FAILED") for a in actions), actions)
            self.assertTrue(any(a.startswith("WARNING") for a in actions), actions)
            state = installer.read_state(root)
            self.assertEqual(state["adapters"]["pack"]["fingerprint"],
                             installer.pack_fingerprint(root / "skills/addyosmani-agent-skills"))
            self.assertTrue((root / ".agents/skills/addy-example").is_symlink())
