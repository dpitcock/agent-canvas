import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("creation_supervisor", Path(__file__).resolve().parents[1] / "scripts/supervisor.py")
supervisor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(supervisor)


class CreationTests(unittest.TestCase):
    def test_foreign_owned_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(supervisor.os, "geteuid", return_value=root.stat().st_uid + 1):
                with self.assertRaises(ValueError):
                    supervisor.HostSupervisor(root)._ensure_root()
            self.assertEqual(list(root.iterdir()), [])

    def test_existing_shared_root_is_rejected_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root.chmod(0o755)
            with self.assertRaises(ValueError):
                supervisor.HostSupervisor(root)._ensure_root()
            self.assertEqual(root.stat().st_mode & 0o777, 0o755)
            self.assertEqual(list(root.iterdir()), [])

    def test_unrelated_private_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "personal.txt").write_text("keep")
            with self.assertRaises(ValueError):
                supervisor.HostSupervisor(root)._ensure_root()
            self.assertEqual((root / "personal.txt").read_text(), "keep")

    def test_creation_evidence_survives_unavailable_audit_append_and_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            project = base / "project"
            project.mkdir()
            host = supervisor.HostSupervisor(base / "host")
            with patch.object(host, "_event", side_effect=OSError("audit unavailable")):
                first = host.create_task("one", project, [])
                again = host.create_task("one", project, [])
            self.assertEqual(first, again)
            events = [e for e in host.audit("one", project=project) if e["type"] == "task_created"]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["authorization_digest"], first["authorization"]["digest"])
