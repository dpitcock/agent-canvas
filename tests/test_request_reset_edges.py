"""Regression coverage for malformed requests and workflow reset names."""
from pathlib import Path
import tempfile
import unittest

from test_container_executor import executor
from test_install import agent_nuke


class RequestResetEdges(unittest.TestCase):
    def test_identifiers_rejected_before_execution(self):
        for field in ("action_id", "attempt_id"):
            for value in (None, 1, True, [], {}, "", " ", "\ud800"):
                with self.subTest(field=field, value=repr(value)):
                    fields = dict(action_id="action", attempt_id="attempt", project=Path("/tmp/project"),
                                  image="example/tool@sha256:" + "a" * 64, command=["true"], inputs=[])
                    fields[field] = value
                    with self.assertRaises(executor.ConfigurationError):
                        executor.ExecutionRequest(**fields)

    def test_reset_removes_workflow_plan_names_but_preserves_project_plans(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workflow = root / ".github/workflows/deploy-plan.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text("name: deploy")
            plan = root / "docs/superpowers/plans/design.md"
            plan.parent.mkdir(parents=True)
            plan.write_text("keep")
            _, preview = agent_nuke.nuke(root)
            self.assertIn("WOULD REMOVE .github/workflows/deploy-plan.yml", preview)
            self.assertTrue(workflow.exists())
            agent_nuke.nuke(root, apply=True)
            self.assertFalse(workflow.exists())
            self.assertEqual(plan.read_text(), "keep")
