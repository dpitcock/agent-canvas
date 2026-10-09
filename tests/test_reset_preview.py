from pathlib import Path
import tempfile
import unittest
from test_install_concurrency import nuke
from test_adapter_rollback import installer


class ResetPreviewTests(unittest.TestCase):
    def test_tracked_adapter_links_are_previewed_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "project"
            config = root / "config/workspace-config.yml"
            config.parent.mkdir(parents=True)
            config.write_text("agentic_envs:\n  codex: true\n  cline: true\n")
            def download(destination, revision):
                skill = destination / "skills/example"
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text("---\nname: example\n---\nExample\n")
            installer.install(root, apply=True, skills=True, home=base / "home", downloader=download)
            links = (".agents/skills/addy-example", ".cline/skills/example")
            for link in links:
                self.assertTrue((root / link).is_symlink())
            _, preview = nuke._uninstall.uninstall(root, mode="remove-all")
            for link in links:
                self.assertEqual(preview.count("WOULD REMOVE " + link), 1)
                self.assertTrue((root / link).is_symlink())
            _, applied = nuke._uninstall.uninstall(root, mode="remove-all", apply=True)
            for link in links:
                self.assertEqual(applied.count("REMOVE " + link), 1)
                self.assertFalse((root / link).is_symlink())

    def test_remove_all_retains_unlisted_shared_parents(self):
        for relative in ("agents/review-coordinator.md", "config/workspace-config.yml",
                         "skills/addyosmani-agent-skills.ref"):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / relative
                path.parent.mkdir()
                path.write_text("managed")
                _, preview = nuke._uninstall.uninstall(root, mode="remove-all")
                self.assertTrue(path.exists())
                _, applied = nuke._uninstall.uninstall(root, mode="remove-all", apply=True)
                self.assertFalse(path.exists())
                self.assertTrue(path.parent.is_dir())
                self.assertEqual([a.replace("WOULD REMOVE", "REMOVE") for a in preview], applied)

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
