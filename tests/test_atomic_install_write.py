"""Installer updates preserve old data when preparing a replacement fails."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("atomic_installer", Path(__file__).resolve().parents[1] / "scripts/install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class AtomicInstallWriteTests(unittest.TestCase):
    def test_replace_failure_preserves_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "AGENTS.md"
            path.write_text("original")
            with patch.object(installer.os, "replace", side_effect=OSError("replace failed")):
                with self.assertRaises(ValueError):
                    installer.write_regular_text(root, path, "replacement")
            self.assertEqual(path.read_text(), "original")
            self.assertEqual(list(root.iterdir()), [path])

    def test_new_file_publication_does_not_overwrite_concurrent_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "AGENTS.md"
            sync = os.fsync
            def create_during_sync(fd):
                sync(fd)
                path.write_text("owner creation")
            with patch.object(installer.os, "fsync", create_during_sync):
                with self.assertRaises(ValueError):
                    installer.write_regular_text(root, path, "replacement", create=True)
            self.assertEqual(path.read_text(), "owner creation")
            self.assertEqual(list(root.iterdir()), [path])

    def test_encoding_failure_preserves_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "AGENTS.md"
            path.write_text("owner instructions")
            with self.assertRaises(UnicodeError):
                installer.write_regular_text(root, path, "bad\ud800")
            self.assertEqual(path.read_text(), "owner instructions")
            self.assertEqual(list(root.iterdir()), [path])

    def test_sync_failure_preserves_original_and_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "AGENTS.md"
            path.write_text("owner instructions")
            with patch.object(installer.os, "fsync", side_effect=OSError("disk failure")):
                with self.assertRaises((OSError, ValueError)):
                    installer.write_regular_text(root, path, "new instructions")
            self.assertEqual(path.read_text(), "owner instructions")
            self.assertEqual(list(root.iterdir()), [path])

    def test_success_replaces_inode_and_preserves_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "AGENTS.md"
            path.write_text("old")
            path.chmod(0o640)
            old = path.stat()
            installer.write_regular_text(root, path, "new")
            self.assertEqual(path.read_text(), "new")
            self.assertNotEqual(path.stat().st_ino, old.st_ino)
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)

    def test_changed_destination_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "AGENTS.md"
            path.write_text("old")
            sync = os.fsync
            def change_during_sync(fd):
                sync(fd)
                path.write_text("concurrent edit")
            with patch.object(installer.os, "fsync", change_during_sync):
                with self.assertRaises(ValueError):
                    installer.write_regular_text(root, path, "new")
            self.assertEqual(path.read_text(), "concurrent edit")
            self.assertEqual(list(root.iterdir()), [path])
