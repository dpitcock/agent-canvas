from pathlib import Path
import tempfile
import unittest
from test_install_concurrency import nuke


class ResetPreviewTests(unittest.TestCase):
    def test_preview_lists_every_removed_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = root / ".github/workflows/x.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text("workflow")
            _, preview = nuke.nuke(root)
            self.assertEqual(preview, ["WOULD REMOVE .github/workflows/x.yml",
                                       "WOULD REMOVE .github/workflows/", "WOULD REMOVE .github/"])
            self.assertTrue(workflow.exists())
            _, applied = nuke.nuke(root, apply=True)
            self.assertEqual(applied, ["REMOVE .github/workflows/x.yml",
                                       "REMOVE .github/workflows/", "REMOVE .github/"])
            self.assertFalse((root / ".github").exists())

    def test_preserved_plan_keeps_ancestors_in_both_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / "docs/superpowers"
            directory.mkdir(parents=True)
            (directory / "plan.md").write_text("keep")
            (directory / "remove.md").write_text("remove")
            _, preview = nuke.nuke(root)
            _, applied = nuke.nuke(root, apply=True)
            self.assertEqual(preview, ["PRESERVE PLAN docs/superpowers/plan.md",
                                       "WOULD REMOVE docs/superpowers/remove.md"])
            self.assertEqual(applied, ["PRESERVE PLAN docs/superpowers/plan.md",
                                       "REMOVE docs/superpowers/remove.md"])
            self.assertEqual((directory / "plan.md").read_text(), "keep")

    def test_shared_file_removal_does_not_prune_unlisted_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "agents/review-coordinator.md"
            path.parent.mkdir()
            path.write_text("managed")
            _, preview = nuke.nuke(root)
            _, applied = nuke.nuke(root, apply=True)
            self.assertEqual(preview, ["WOULD REMOVE agents/review-coordinator.md"])
            self.assertEqual(applied, ["REMOVE agents/review-coordinator.md"])
            self.assertTrue(path.parent.is_dir())
