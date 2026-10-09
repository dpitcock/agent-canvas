from pathlib import Path
import tempfile
import unittest
from test_container_executor import executor


class InputDepthTests(unittest.TestCase):
    def test_excessive_depth_is_rejected_and_partial_stage_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            project = base / "project"
            staging = base / "staging"
            project.mkdir()
            staging.mkdir()
            (project / "first").write_text("first")
            path = project.joinpath(*(["d"] * 64), "input")
            path.parent.mkdir(parents=True)
            path.write_text("deep")
            with executor.PinnedProject.open(project, executor.ProjectIdentity.capture(project)) as pinned:
                with self.assertRaisesRegex(executor.InputRejected, "depth"):
                    snapshot = executor.stage_inputs(pinned, ["first", str(path.relative_to(project))], staging,
                                                     max_bytes=1024, max_files=2)
                    snapshot.remove()
                    snapshot.close()
            self.assertEqual(list(staging.iterdir()), [])

    def test_supported_depth_can_be_staged_and_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            project = base / "project"
            staging = base / "staging"
            project.mkdir()
            staging.mkdir()
            relative = "/".join(["d"] * 63 + ["input"])
            path = project / relative
            path.parent.mkdir(parents=True)
            path.write_text("data")
            with executor.PinnedProject.open(project, executor.ProjectIdentity.capture(project)) as pinned:
                snapshot = executor.stage_inputs(pinned, [relative], staging, max_bytes=1024, max_files=1)
                try:
                    self.assertEqual((snapshot.root / relative).read_text(), "data")
                    self.assertTrue(snapshot.remove())
                finally:
                    snapshot.close()
            self.assertEqual(list(staging.iterdir()), [])
