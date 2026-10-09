from pathlib import Path
import unittest
from test_container_executor import executor


class ResourceTypes(unittest.TestCase):
    def request(self, **values):
        command = values.pop("command", ["true"])
        return executor.ExecutionRequest(action_id="a", attempt_id="b", project=Path("/tmp/project"),
                                         image="alpine@sha256:" + "a" * 64,
                                         command=command, inputs=[], **values)

    def test_command_rejects_nul_and_surrogates(self):
        for value in ("bad\0argument", "bad\ud800argument"):
            for command in ([value], ["echo", value]):
                with self.subTest(command=command):
                    with self.assertRaises(executor.ConfigurationError):
                        self.request(command=command)

    def test_command_accepts_utf8(self):
        self.assertEqual(self.request(command=["echo", "café"]).command, ("echo", "café"))

    def test_boolean_numeric_limits_are_rejected(self):
        for field in ("timeout_s", "max_output_bytes", "pids", "cpus"):
            for value in (True, False):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(executor.ConfigurationError):
                        self.request(**{field: value})

    def test_numeric_bounds_remain_accepted(self):
        request = self.request(timeout_s=1, max_output_bytes=0, pids=1, cpus=0.5)
        self.assertEqual((request.timeout_s, request.max_output_bytes, request.pids, request.cpus),
                         (1, 0, 1, "0.5"))
