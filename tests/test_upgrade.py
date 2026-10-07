"""Offline upgrade behavior tests: temporary files, no network or subprocesses."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("upgrade_installer", Path(__file__).resolve().parents[1] / "scripts/install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


class UpgradeSmoke(unittest.TestCase):
    def test_install_rejects_fifo_state_without_reading_it(self):
        """A FIFO state file must not block an install before it is rejected."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            state_path = root / installer.STATE
            state_path.parent.mkdir(parents=True)
            os.mkfifo(state_path)

            original_read_text = Path.read_text

            def read_text(path, *args, **kwargs):
                if path == state_path:
                    raise AssertionError("state FIFO was read")
                return original_read_text(path, *args, **kwargs)

            with patch.object(Path, "read_text", read_text):
                with self.assertRaisesRegex(ValueError, "non-regular .agent-canvas/state.json"):
                    installer.install(root)

    def test_optimized_python_rejects_invalid_saved_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            state_path = root / installer.STATE
            state_path.parent.mkdir(parents=True)
            state_path.write_text(json.dumps({"schema_version": 999}))
            program = f'''\
import importlib.util
from pathlib import Path
spec = importlib.util.spec_from_file_location("installer", {str(Path(__file__).resolve().parents[1] / "scripts/install.py")!r})
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
try:
    installer.read_state(Path({str(root)!r}))
except ValueError:
    pass
else:
    raise SystemExit("invalid state was accepted")
'''
            run = subprocess.run([sys.executable, "-O", "-c", program], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)

    def test_upgrade_records_provenance_for_new_managed_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = self.source(base)
            target = base / "project"
            installer.install(target, source=source, home=base / "home", apply=True)

            introduced = ".owner-override.example"
            (target / introduced).unlink()
            state = installer.read_state(target)
            state["baselines"].pop(introduced)
            state["provenance"]["managed_files"].remove(introduced)
            installer.save_state(target, state)

            installer.upgrade(target, source=source, apply=True)

            self.assertTrue((target / introduced).is_file())
            self.assertIn(introduced, installer.read_state(target)["provenance"]["managed_files"])

    def test_reverted_preview_refreshes_handoff_without_changing_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = self.source(base)
            target = base / "project"
            installer.install(target, source=source, home=base / "home")
            example = target / ".owner-override.example"
            example.write_text("# Project customization\n")
            upstream = source / ".owner-override.example"
            original = upstream.read_text()
            upstream.write_text("# Conflicting release\n")
            followup = target / "INSTALL-FOLLOWUP.md"
            followup.write_text(followup.read_text() + "\nKeep my notes.\n")
            installer.upgrade(target, source=source)
            self.assertIn("CONFLICT .owner-override.example", followup.read_text())
            upstream.write_text(original)
            before = snapshot(target)
            installer.upgrade(target, source=source, apply=True)
            after = snapshot(target)
            self.assertEqual({k: v for k, v in before.items() if k != "INSTALL-FOLLOWUP.md"},
                             {k: v for k, v in after.items() if k != "INSTALL-FOLLOWUP.md"})
            self.assertNotIn("CONFLICT .owner-override.example", followup.read_text())
            self.assertIn("Pending files: none.", followup.read_text())
            self.assertIn("Keep my notes.", followup.read_text())
            installer.upgrade(target, source=source, apply=True)
            self.assertEqual(after, snapshot(target))

    def source(self, base):
        source = base / "package"
        for name in installer.MANAGED:
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((installer.SOURCE / name).read_bytes())
        (source / "AGENTS.md").write_text(
            "# Rules\n\n## Lanes\n- Toolkit\n\n## Scope, risk, and environment\n- Be safe\n\n"
            "## Planning\n- Original planning\n\n## Reviews\n- Original reviews\n")
        return source

    def test_customizations_and_resolutions_survive_multiple_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = self.source(base)
            target = base / "project"
            installer.install(target, source=source, home=base / "home", role="toolkit-authoring",
                              environment="production", workspace="keep-name", slack="keep-channel")
            rules = target / "AGENTS.md"
            rules.write_text(rules.read_text().replace("Original planning", "Project-specific planning"))
            override = target / ".owner-override"
            override.write_text('OWNER_OVERRIDE="pause"\n')
            followup = target / "INSTALL-FOLLOWUP.md"
            followup.write_text(followup.read_text() + "\nMy earlier resolution notes must survive.\n")
            upstream = source / "AGENTS.md"
            upstream.write_text(upstream.read_text().replace("Original reviews", "Improved reviews"))

            before = snapshot(target)
            _, actions = installer.upgrade(target, source=source)
            self.assertTrue(any(a.startswith("WOULD UPDATE AGENTS.md") for a in actions))
            after = snapshot(target)
            self.assertEqual({k: v for k, v in before.items() if k != "INSTALL-FOLLOWUP.md"},
                             {k: v for k, v in after.items() if k != "INSTALL-FOLLOWUP.md"})
            installer.upgrade(target, source=source, apply=True)
            self.assertIn("Project-specific planning", rules.read_text())
            self.assertIn("Improved reviews", rules.read_text())
            self.assertIn('target_environment: "production"', (target / "config/workspace-config.yml").read_text())
            self.assertIn('workspace: "keep-name"', (target / "config/workspace-config.yml").read_text())
            self.assertEqual(override.read_text(), 'OWNER_OVERRIDE="pause"\n')
            before = snapshot(target)
            installer.upgrade(target, source=source, apply=True)
            self.assertEqual(before, snapshot(target))

            # Same line changes in both versions: retain project text and track only the pending file.
            upstream.write_text(upstream.read_text().replace("Original planning", "New upstream planning"))
            example = source / ".owner-override.example"
            example.write_text(example.read_text() + "\n# New example guidance\n")
            installer.upgrade(target, source=source, apply=True)
            state = installer.read_state(target)
            self.assertEqual(set(state["pending"]), {"AGENTS.md"})
            self.assertIn("Project-specific planning", rules.read_text())
            self.assertIn("New example guidance", (target / ".owner-override.example").read_text())
            before = snapshot(target)
            installer.upgrade(target, source=source, apply=True)
            self.assertEqual(before, snapshot(target))
            installer.upgrade(target, source=source, apply=True, resolve=["AGENTS.md"],
                              reason="Keep the project's established planning convention")
            state = installer.read_state(target)
            self.assertFalse(state["pending"])
            self.assertEqual(len(state["resolutions"]), 1)

            # Later disjoint changes land without asking about the resolved planning decision again.
            upstream.write_text(upstream.read_text().replace("Improved reviews", "Latest reviews"))
            installer.upgrade(target, source=source, apply=True)
            self.assertIn("Latest reviews", rules.read_text())
            self.assertIn("Project-specific planning", rules.read_text())
            self.assertFalse(installer.read_state(target)["pending"])
            self.assertEqual(installer.read_state(target)["resolutions"], state["resolutions"])
            self.assertIn("My earlier resolution notes", followup.read_text())
            upstream.write_text(upstream.read_text().replace("New upstream planning", "Changed again planning"))
            installer.upgrade(target, source=source, apply=True)
            self.assertEqual(set(installer.read_state(target)["pending"]), {"AGENTS.md"})
            self.assertEqual(installer.read_state(target)["resolutions"], state["resolutions"])

    def test_legacy_install_deleted_files_and_dependency_decisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = self.source(base)
            target = base / "older-project"
            target.mkdir()
            (target / "AGENTS.md").write_text("Existing resolved project rules\n")
            installer.upgrade(target, source=source)
            self.assertFalse((target / installer.STATE).exists())
            installer.upgrade(target, source=source, apply=True)
            self.assertEqual((target / "AGENTS.md").read_text(), "Existing resolved project rules\n")
            self.assertEqual(set(installer.read_state(target)["pending"]), {"AGENTS.md"})
            installer.upgrade(target, source=source, apply=True, resolve=["AGENTS.md"], reason="Preserve older resolution")
            example = target / ".owner-override.example"
            example.unlink()
            installer.upgrade(target, source=source, apply=True)
            self.assertFalse(example.exists())
            upstream_example = source / ".owner-override.example"
            upstream_example.write_text(upstream_example.read_text() + "\n# Changed\n")
            ref = source / "skills/addyosmani-agent-skills.ref"
            ref.write_text("a" * 40 + "\n")
            original_ref = (target / "skills/addyosmani-agent-skills.ref").read_text()
            installer.upgrade(target, source=source, apply=True)
            self.assertFalse(example.exists())
            self.assertEqual((target / "skills/addyosmani-agent-skills.ref").read_text(), original_ref)
            self.assertEqual(set(installer.read_state(target)["pending"]),
                             {".owner-override.example", "skills/addyosmani-agent-skills.ref"})
            ref.write_text("b" * 40 + "\n")
            before = snapshot(target)
            with self.assertRaises(ValueError):
                installer.upgrade(target, source=source, apply=True,
                                  resolve=["skills/addyosmani-agent-skills.ref"], reason="Stale decision")
            self.assertEqual(before, snapshot(target))

    def test_damaged_history_or_external_paths_never_overwrite_project_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = self.source(base)
            target = base / "project"
            installer.install(target, source=source, home=base / "home")
            state_path = target / installer.STATE
            state_path.write_text("invalid json")
            before = snapshot(target)
            with self.assertRaises(ValueError):
                installer.upgrade(target, source=source, apply=True)
            self.assertEqual(before, snapshot(target))
            state_path.unlink()
            outside = base / "outside.json"
            outside.write_text("Do not change")
            state_path.symlink_to(outside)
            with self.assertRaises(ValueError):
                installer.upgrade(target, source=source, apply=True)
            self.assertEqual(outside.read_text(), "Do not change")


if __name__ == "__main__":
    unittest.main()
