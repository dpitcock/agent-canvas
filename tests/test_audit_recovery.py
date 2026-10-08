"""Interrupted project audit writes must not poison unrelated tasks."""
import fcntl
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_supervised import supervisor


class AuditRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.project = root / "project"
        self.project.mkdir()
        self.host = supervisor.HostSupervisor(root / "state")
        for task_id in ("first", "second"):
            self.host.create_task(task_id, self.project, [])
        self.path = self.host._event_path("first", self.project)

    def test_read_recovers_partial_utf8_tail_preserving_all_complete_records(self):
        original = self.path.read_bytes()
        with self.path.open("ab") as out:
            out.write(b'{"task_id":"first","message":"\xe2\x82')
        for task_id in ("first", "second"):
            events = self.host.audit(task_id, project=self.project)
            self.assertEqual([event["type"] for event in events], ["task_created"])
            self.assertEqual(events[0]["task_id"], task_id)
        self.assertEqual(self.path.read_bytes(), original)

    def test_append_recovers_partial_tail_before_writing_next_record(self):
        original = self.path.read_bytes()
        with self.path.open("ab") as out:
            out.write(b'{"task_id":"first"')
        event = self.host._event("second", "resumed", project=self.project)
        self.assertEqual(self.path.read_bytes(), original + (json.dumps(event, sort_keys=True) + "\n").encode())
        self.assertEqual([e["type"] for e in self.host.audit("second", project=self.project)],
                         ["task_created", "resumed"])

    def test_short_writes_are_completed_before_unlocking(self):
        original = self.path.read_bytes()
        real_write = os.write
        def short_write(fd, data):
            return real_write(fd, data[:7])
        with patch.object(supervisor.os, "write", side_effect=short_write):
            event = self.host._event("first", "short", project=self.project)
        self.assertEqual(self.path.read_bytes(), original + (json.dumps(event, sort_keys=True) + "\n").encode())

    def test_failed_partial_append_is_recovered_on_next_append(self):
        original = self.path.read_bytes()
        real_write = os.write
        calls = 0
        def interrupted_write(fd, data):
            nonlocal calls
            calls += 1
            if calls == 1:
                return real_write(fd, data[:7])
            raise OSError("interrupted append")
        with patch.object(supervisor.os, "write", side_effect=interrupted_write):
            with self.assertRaises(OSError):
                self.host._event("first", "interrupted", project=self.project)
        self.assertTrue(self.path.read_bytes().startswith(original))
        self.assertGreater(self.path.stat().st_size, len(original))
        event = self.host._event("second", "resumed", project=self.project)
        self.assertEqual(self.path.read_bytes(), original + (json.dumps(event, sort_keys=True) + "\n").encode())

    def test_complete_corruption_fails_closed_for_read_and_append(self):
        original = self.path.read_bytes()
        for corruption in (b'{broken}\n', b'\xff\n'):
            with self.subTest(corruption=corruption):
                contents = original + corruption + original + b'{"partial":'
                self.path.write_bytes(contents)
                for operation in (
                    lambda: self.host.audit("second", project=self.project),
                    lambda: self.host._event("second", "resumed", project=self.project),
                ):
                    with self.assertRaises(ValueError):
                        operation()
                    self.assertEqual(self.path.read_bytes(), contents)

    def test_other_task_read_and_append_wait_for_active_writer(self):
        context = multiprocessing.get_context("fork")
        for operation in ("read", "append"):
            with self.subTest(operation=operation):
                parent, child = context.Pipe()
                def access_audit():
                    parent.close()
                    child.send("started")
                    try:
                        if operation == "read":
                            self.host.audit("second", project=self.project)
                        else:
                            self.host._event("second", "parallel", project=self.project)
                        child.send("finished")
                    except Exception as error:
                        child.send(repr(error))
                    finally:
                        child.close()
                # Split a real append while holding the same file lock writers use.
                # Another process must not discard or read the in-progress record.
                with self.path.open("ab", buffering=0) as writer:
                    fcntl.flock(writer, fcntl.LOCK_EX)
                    writer.write(b'{"at":1,"task_id":"first",')
                    process = context.Process(target=access_audit)
                    process.start()
                    child.close()
                    try:
                        self.assertTrue(parent.poll(5))
                        self.assertEqual(parent.recv(), "started")
                        blocked = not parent.poll(0.2)
                        writer.write(b'"type":"split"}\n')
                        os.fsync(writer.fileno())
                    finally:
                        fcntl.flock(writer, fcntl.LOCK_UN)
                    try:
                        self.assertTrue(parent.poll(5))
                        result = parent.recv()
                        self.assertTrue(blocked, "Other task accessed a partially written audit record")
                        self.assertEqual(result, "finished")
                    finally:
                        process.join(5)
                        if process.is_alive():
                            process.terminate()
                            process.join()
                        parent.close()
                self.assertEqual(process.exitcode, 0)
                events = self.host.audit("first", project=self.project)
                self.assertEqual(sum(e["type"] == "split" for e in events),
                                 1 if operation == "read" else 2)


if __name__ == "__main__":
    unittest.main()
