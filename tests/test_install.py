"""One offline smoke test: no subprocesses, network, or nested runners."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("installer", Path(__file__).resolve().parents[1] / "scripts/install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallSmoke(unittest.TestCase):
    def test_new_existing_repeated_and_conflicting_installations(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            home = base / "empty-home"
            calls = []

            def local_pack(destination, revision):
                calls.append(revision)
                skill = destination / "skills/example"
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text("---\nname: example\n---\nExample")
                (destination / "references").mkdir()
                (destination / "references/checklist.md").write_text("Shared reference")

            fresh = base / "new app"
            active, _ = installer.install(fresh, skills=True, home=home, downloader=local_pack,
                                           workspace='my "app"', slack="my-channel")
            self.assertTrue(active)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0], (installer.SOURCE / "skills/addyosmani-agent-skills.ref").read_text().strip())
            self.assertIn('workspace: ' + json.dumps('my "app"'), (fresh / "config/workspace-config.yml").read_text())
            self.assertIn("repo_role: application", (fresh / "config/workspace-config.yml").read_text())
            self.assertTrue((fresh / ".agents/skills/addy-example/SKILL.md").is_file())
            self.assertIn("Prompt to run", (fresh / "INSTALL-FOLLOWUP.md").read_text())
            before = {p.relative_to(fresh): p.read_bytes() for p in fresh.rglob("*") if p.is_file()}
            installer.install(fresh, apply=True, skills=True, home=home, downloader=local_pack)
            self.assertEqual(len(calls), 1)
            self.assertEqual(before, {p.relative_to(fresh): p.read_bytes() for p in fresh.rglob("*") if p.is_file()})

            existing = base / "existing"
            (existing / "config").mkdir(parents=True)
            (existing / "AGENTS.md").write_text("Project-specific rules\n")
            (existing / "config/workspace-config.yml").write_text("workspace: keep-me\n")
            (existing / ".gitignore").write_text("custom-ignore")
            (existing / "app.py").write_text("print('preserve')\n")
            custom = existing / ".agents/skills/renamed"
            custom.mkdir(parents=True)
            (custom / "SKILL.md").write_text("---\nname: planning-and-task-breakdown\n---\n")
            original = {p.relative_to(existing): p.read_bytes() for p in existing.rglob("*") if p.is_file()}
            active, _ = installer.install(existing, skills=True, home=home, downloader=local_pack)
            self.assertFalse(active)
            self.assertEqual({p.relative_to(existing) for p in existing.rglob("*") if p.is_file()}, set(original) | {Path("INSTALL-FOLLOWUP.md")})
            self.assertTrue(all((existing / p).read_bytes() == data for p, data in original.items()))
            installer.install(existing, apply=True, skills=True, home=home, downloader=local_pack)
            self.assertEqual(len(calls), 1)  # Existing renamed skills prevent a duplicate download.
            self.assertEqual((existing / "AGENTS.md").read_text(), "Project-specific rules\n")
            self.assertEqual((existing / "config/workspace-config.yml").read_text(), "workspace: keep-me\n")
            self.assertEqual((existing / "app.py").read_bytes(), original[Path("app.py")])
            self.assertTrue((existing / ".gitignore").read_text().startswith("custom-ignore\n"))

            linked = base / "linked"
            linked.mkdir()
            outside = base / "outside"
            outside.mkdir()
            (linked / "config").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                installer.install(linked, apply=True, home=home)
            self.assertEqual(list(outside.iterdir()), [])
            self.assertFalse((linked / "AGENTS.md").exists())

            def unavailable(destination, revision):
                raise OSError("offline")

            failed = base / "failed-download"
            _, actions = installer.install(failed, skills=True, home=home, downloader=unavailable)
            self.assertTrue(any(action.startswith("FAILED") for action in actions))
            self.assertIn("FAILED skill installation", (failed / "INSTALL-FOLLOWUP.md").read_text())

            user_skill = home / ".agents/skills/existing/SKILL.md"
            user_skill.parent.mkdir(parents=True)
            user_skill.write_text("Existing user skill")
            _, actions = installer.install(base / "with-global-skills", skills=True, home=home, downloader=local_pack)
            self.assertEqual(len(calls), 1)
            self.assertTrue(any(action.startswith("DECIDE skill installation") for action in actions))


if __name__ == "__main__":
    unittest.main()
