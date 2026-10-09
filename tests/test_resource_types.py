from pathlib import Path
import unittest
from test_container_executor import executor


class ResourceTypes(unittest.TestCase):
    def request(self, **values):
        return executor.ExecutionRequest(action_id="a", attempt_id="b", project=Path("/tmp/project"),
                                         image="alpine@sha256:" + "a" * 64,
                                         command=["true"], inputs=[], **values)

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
