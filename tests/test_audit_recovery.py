"""Interrupted project audit writes must not poison unrelated tasks."""
import fcntl
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import tracemalloc
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

    def test_append_preserves_complete_corruption_and_read_reports_it(self):
        original = self.path.read_bytes()
        for corruption in (b'{broken}\n', b'\xff\n'):
            with self.subTest(corruption=corruption):
                contents = original + corruption + original + b'{"partial":'
                self.path.write_bytes(contents)
                event = self.host._event("second", "resumed", project=self.project)
                self.assertEqual(self.path.read_bytes(), original + corruption + original
                                 + (json.dumps(event, sort_keys=True) + "\n").encode())
                with self.assertRaises(ValueError):
                    self.host.audit("second", project=self.project)
                self.assertIn(corruption, self.path.read_bytes())

    def test_append_recovers_empty_or_entirely_unfinished_log(self):
        for tail in (b"", b'{"partial":"' + b'x' * 150000 + b'\xe2\x82'):
            with self.subTest(tail_size=len(tail)):
                self.path.write_bytes(tail)
                event = self.host._event("second", "resumed", project=self.project)
                self.assertEqual(self.path.read_bytes(),
                                 (json.dumps(event, sort_keys=True) + "\n").encode())
                self.assertEqual(self.host.audit("second", project=self.project), [event])

    def test_append_reads_only_bounded_tail_independent_of_history_size(self):
        real_open = Path.open
        reads = []
        testcase = self

        class ObservedFile:
            def __init__(self, stream):
                self.stream = stream

            def __getattr__(self, name):
                return getattr(self.stream, name)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def read(self, size=-1):
                testcase.assertGreaterEqual(size, 0, "Audit append read the entire history")
                testcase.assertLessEqual(size, 65536)
                data = self.stream.read(size)
                reads.append(len(data))
                return data

        def observed_open(path, *args, **kwargs):
            stream = real_open(path, *args, **kwargs)
            return ObservedFile(stream) if path == self.path else stream

        record = b'{"task_id":"other","at":0,"type":"history"}\n'
        for tail in (b"", b'{"partial":"' + b'x' * 150000 + b'\xe2\x82'):
            totals = []
            for count in (2048, 65536):
                history = record * count
                self.path.write_bytes(history + tail)
                reads.clear()
                with patch.object(Path, "open", observed_open):
                    event = self.host._event("second", "resumed", project=self.project)
                totals.append(sum(reads))
                self.assertEqual(self.path.read_bytes(), history
                                 + (json.dumps(event, sort_keys=True) + "\n").encode())
            self.assertEqual(totals[0], totals[1], "Append I/O grew with complete history")
            self.assertLessEqual(totals[1], len(tail) + 65536)

    def test_audit_does_not_retain_unrelated_task_history(self):
        original = self.path.read_bytes()
        record = (json.dumps({"task_id": "other", "at": 0, "type": "history",
                              "message": "x" * 256}) + "\n").encode()
        self.path.write_bytes(original + record * 20000)
        tracemalloc.start()
        try:
            events = self.host.audit("first", project=self.project)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual([event["type"] for event in events], ["task_created"])
        self.assertLess(peak, 2 * 1024 * 1024, "Audit retained unrelated history in memory")

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
