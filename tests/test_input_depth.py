from pathlib import Path
import tempfile
import unittest
from test_container_executor import executor


class InputDepthTests(unittest.TestCase):
    def test_duplicate_paths_are_rejected_and_partial_stage_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            project = base / "project"
            staging = base / "staging"
            project.mkdir()
            staging.mkdir()
            (project / "input").write_text("data")
            with executor.PinnedProject.open(project, executor.ProjectIdentity.capture(project)) as pinned:
                with self.assertRaises(executor.InputRejected):
                    executor.stage_inputs(pinned, ["input", "input"], staging, max_bytes=1024, max_files=2)
            self.assertEqual(list(staging.iterdir()), [])
            self.assertEqual((project / "input").read_text(), "data")

    def test_invalid_encoding_is_rejected_and_partial_stage_removed(self):
        for relative in ("bad\0input", "bad\ud800input"):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                project = base / "project"
                staging = base / "staging"
                project.mkdir()
                staging.mkdir()
                (project / "first").write_text("data")
                with executor.PinnedProject.open(project, executor.ProjectIdentity.capture(project)) as pinned:
                    with self.assertRaises(executor.InputRejected):
                        executor.stage_inputs(pinned, ["first", relative], staging, max_bytes=1024, max_files=2)
                self.assertEqual(list(staging.iterdir()), [])

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
