"""Adapter batches leave prior discovery links intact when an operation fails."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    "rollback_installer", Path(__file__).resolve().parents[1] / "scripts/install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class AdapterRollback(unittest.TestCase):
    def test_install_retry_keeps_links_consistent_with_saved_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, home = base / "project", base / "home"

            def download(destination, revision):
                for name in ("first", "second"):
                    skill = destination / "skills" / name
                    skill.mkdir(parents=True)
                    (skill / "SKILL.md").write_text(f"---\nname: {name}\n---\nExample\n")

            config = root / "config/workspace-config.yml"
            config.parent.mkdir(parents=True)
            config.write_text("agentic_envs:\n  codex: true\n  cline: false\n")
            installer.install(root, apply=True, skills=True, home=home, downloader=download)
            config.write_text("agentic_envs:\n  codex: false\n  cline: true\n")
            state_before = (root / installer.STATE).read_bytes()
            saved_links = json.loads(state_before)["adapters"]["links"]
            symlink = os.symlink

            def fail_second_cline(target, name, **kwargs):
                if name == "second":
                    raise OSError("injected adapter failure")
                return symlink(target, name, **kwargs)

            with patch.object(installer.os, "symlink", side_effect=fail_second_cline):
                with self.assertRaisesRegex(OSError, "injected adapter failure"):
                    installer.install(root, apply=True, home=home)
            self.assertEqual((root / installer.STATE).read_bytes(), state_before)
            for relative, target in saved_links.items():
                self.assertEqual(os.readlink(root / relative), target)
            self.assertFalse((root / ".cline/skills/first").is_symlink())

            installer.install(root, apply=True, home=home)
            saved = json.loads((root / installer.STATE).read_text())
            self.assertEqual(set(saved["adapters"]["links"]), {
                ".cline/skills/first", ".cline/skills/second"})
            for relative, target in saved["adapters"]["links"].items():
                self.assertEqual(os.readlink(root / relative), target)
            self.assertFalse((root / ".agents/skills/addy-first").is_symlink())

    def test_link_failure_does_not_start_gitignore_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            link = root / ".cline/skills/first"
            ignore = root / ".gitignore"
            ignore.write_text("existing\n")
            with self.assertRaises(FileExistsError):
                installer.apply_adapters([
                    ("add", link, "target"), ("add", link, "conflict")], root=root)
            self.assertEqual(ignore.read_text(), "existing\n")

    def test_later_conflict_rolls_back_addition_and_restores_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / ".agents/skills"
            parent.mkdir(parents=True)
            old, added, conflict = (parent / name for name in ("old", "added", "conflict"))
            old.symlink_to("original-target")
            conflict.symlink_to("user-target")

            with self.assertRaises(FileExistsError):
                installer.apply_adapters([
                    ("remove", old, None), ("add", added, "new-target"),
                    ("add", conflict, "unwanted-target")], root=root)

            self.assertTrue(old.is_symlink())
            self.assertEqual(os.readlink(old), "original-target")
            self.assertFalse(added.is_symlink())
            self.assertEqual(os.readlink(conflict), "user-target")

    def test_gitignore_failure_rolls_back_all_completed_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / ".agents/skills/first"
            second = root / ".cline/skills/second"
            (root / ".gitignore").mkdir()
            provenance = {"gitignore_entries": ["existing"]}

            with self.assertRaisesRegex(ValueError, "regular file"):
                installer.apply_adapters([
                    ("add", first, "first-target"),
                    ("add", second, "second-target")], provenance, root=root)

            self.assertFalse(first.is_symlink())
            self.assertFalse(second.is_symlink())
            self.assertEqual(provenance, {"gitignore_entries": ["existing"]})

    def test_retained_directory_descriptors_close_after_success_and_failure(self):
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                link = root / ".agents/skills/first"
                descriptors = []
                real_dup = os.dup

                def remember_dup(descriptor):
                    retained = real_dup(descriptor)
                    descriptors.append(retained)
                    return retained

                operations = [("add", link, "target")]
                if fail:
                    operations.append(("add", link, "conflict"))
                with patch.object(installer.os, "dup", side_effect=remember_dup):
                    if fail:
                        with self.assertRaises(FileExistsError):
                            installer.apply_adapters(operations, root=root)
                    else:
                        installer.apply_adapters(operations, root=root)
                for descriptor in descriptors:
                    with self.assertRaises(OSError):
                        os.fstat(descriptor)
                self.assertEqual(link.is_symlink(), not fail)
