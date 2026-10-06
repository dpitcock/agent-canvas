"""Regressions for stable host registration and descriptor-bound overrides."""
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from tests.test_supervised import supervisor, installer


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.a, self.b = self.base / "a", self.base / "b"
        self.a.mkdir()
        self.b.mkdir()
        self.host = supervisor.HostSupervisor(self.base / "state")

    def test_project_symlink_swap_cannot_select_another_registration(self):
        for project in (self.a, self.b):
            self.host.create_task("same", project, [{"id": "work", "operation": "write"}])
        self.host.complete_action("same", "work", evidence={}, project=self.b)
        self.a.rename(self.base / "original-a")
        self.a.symlink_to(self.b, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "identity"):
            self.host.gate_final("same", "try", "wrong", project=self.a)
        self.assertEqual(self.host.visible_messages("same", project=self.b), [])

    def test_override_rejects_regular_directory_swap_before_open(self):
        (self.a / ".owner-override").write_text("OWNER_OVERRIDE=pause")
        (self.b / ".owner-override").write_text("OWNER_OVERRIDE=reset")
        actual = os.open
        def swap(path, flags, *args, **kwargs):
            if str(path) == self.a.name:
                self.a.rename(self.base / "original-a")
                self.b.rename(self.a)
            return actual(path, flags, *args, **kwargs)
        with mock.patch.object(installer.os, "open", side_effect=swap):
            with self.assertRaisesRegex(ValueError, "identity"):
                installer.root_owner_override(self.a)

    def test_override_rejects_shared_hardlink(self):
        outside = self.b / ".owner-override"
        outside.write_text("OWNER_OVERRIDE=reset")
        os.link(outside, self.a / ".owner-override")
        with self.assertRaisesRegex(ValueError, "shared|link"):
            installer.root_owner_override(self.a)

    def test_provision_cannot_overwrite_a_concurrent_override_import(self):
        entered, allow_write, imported = threading.Event(), threading.Event(), threading.Event()
        errors = []
        original = self.host._write
        def write(path, value):
            if path.name == "registration.json" and "owner_override" not in value and not entered.is_set():
                entered.set()
                if not allow_write.wait(2):
                    raise RuntimeError("test write barrier timed out")
            return original(path, value)
        def run_provision():
            try:
                self.host.provision(self.a)
            except Exception as error:
                errors.append(error)
        def run_import():
            try:
                self.host.import_owner_override(self.a, ["pause"], source_digest="a" * 64)
                imported.set()
            except Exception as error:
                errors.append(error)
        with mock.patch.object(self.host, "_write", side_effect=write):
            first = threading.Thread(target=run_provision)
            first.start()
            self.assertTrue(entered.wait(1))
            second = threading.Thread(target=run_import)
            second.start()
            try:
                self.assertFalse(imported.wait(0.05))
            finally:
                allow_write.set()
                first.join(3)
                second.join(3)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.host.provision(self.a)["owner_override"]["modes"], ["pause"])
