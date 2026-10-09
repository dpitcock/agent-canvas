"""Cleanup rewrites preserve user files on concurrent edits and I/O failure."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("cleanup", Path(__file__).resolve().parents[1] / "scripts/uninstall.py")
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)


class CleanupWriteTests(unittest.TestCase):
    def test_concurrent_edits_are_preserved(self):
        for name in (".gitignore", "INSTALL-FOLLOWUP.md"):
            for timing in ("before", "during"):
                with self.subTest(name=name, timing=timing), tempfile.TemporaryDirectory() as tmp:
                    root, root_id = cleanup.root_path(tmp, with_identity=True)
                    path = root / name
                    path.write_text("original")
                    _, identity = cleanup.regular_text_snapshot(root, path)
                    sync = os.fsync
                    def edit(fd):
                        sync(fd)
                        path.write_text("owner edit")
                    if timing == "before":
                        path.write_text("owner edit")
                    with patch.object(cleanup.os, "fsync", edit if timing == "during" else sync):
                        with self.assertRaises(ValueError):
                            cleanup.replace_regular_snapshot(root, path, "cleaned", identity, root_identity=root_id)
                    self.assertEqual(path.read_text(), "owner edit")
                    self.assertEqual(list(root.iterdir()), [path])

    def test_failed_preparation_preserves_original(self):
        for name in (".gitignore", "INSTALL-FOLLOWUP.md"):
            for failure in ("encoding", "write", "fsync", "replace"):
                with self.subTest(name=name, failure=failure), tempfile.TemporaryDirectory() as tmp:
                    root, root_id = cleanup.root_path(tmp, with_identity=True)
                    path = root / name
                    path.write_text("original")
                    _, identity = cleanup.regular_text_snapshot(root, path)
                    text = "bad\ud800" if failure == "encoding" else "cleaned"
                    method = "fsync" if failure == "encoding" else failure
                    with patch.object(cleanup.os, method, side_effect=OSError("injected I/O failure")):
                        with self.assertRaises((OSError, ValueError)):
                            cleanup.replace_regular_snapshot(root, path, text, identity, root_identity=root_id)
                    self.assertEqual(path.read_text(), "original")
                    self.assertEqual(list(root.iterdir()), [path])

    def test_success_preserves_mode_and_publishes_complete_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, root_id = cleanup.root_path(tmp, with_identity=True)
            path = root / ".gitignore"
            path.write_text("original")
            path.chmod(0o640)
            inode = path.stat().st_ino
            _, identity = cleanup.regular_text_snapshot(root, path)
            cleanup.replace_regular_snapshot(root, path, "cleaned", identity, root_identity=root_id)
            self.assertEqual(path.read_text(), "cleaned")
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)
            self.assertNotEqual(path.stat().st_ino, inode)
            self.assertEqual(list(root.iterdir()), [path])
