"""Project adapter lifecycle checks with local fixtures; no apps or network."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("env_installer", Path(__file__).resolve().parents[1] / "scripts/install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def snapshot(root):
    return {str(p.relative_to(root)): ("link", str(p.readlink())) if p.is_symlink()
            else ("file", p.read_bytes()) for p in root.rglob("*") if p.is_symlink() or p.is_file()}


class EnvironmentInstall(unittest.TestCase):
    def source(self, base, mapping="  codex: true\n  cline: true\n  claude_code: false\n"):
        source = base / "package"
        for name in installer.MANAGED:
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((installer.SOURCE / name).read_bytes())
        config = source / "config/workspace-config.yml"
        config.write_text("workspace: package\n" + ("agentic_envs:\n" + mapping if mapping else "") +
                          "target_environment: local\nrepo_role: application\nslack_channel_name: shared-channel\n")
        return source

    def pack(self, destination, revision):
        skill = destination / "skills/example"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: example\ndescription: Example skill.\n---\nUse references.\n")
        (destination / "references").mkdir()
        (destination / "references/checklist.md").write_text("Shared reference\n")

    def test_both_environments_share_source_and_preserve_repeated_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = self.source(base)
            target = base / "project"
            installer.install(target, source=source, home=base / "home", skills=True, downloader=self.pack)
            codex = target / ".agents/skills/addy-example"
            cline = target / ".cline/skills/example"
            self.assertTrue(codex.is_symlink())
            self.assertTrue(cline.is_symlink())
            self.assertEqual(codex.resolve(), cline.resolve())
            self.assertFalse((target / ".clinerules").exists())
            self.assertFalse((target / ".owner-override").exists())
            before = snapshot(target)
            installer.install(target, source=source, home=base / "home", skills=True,
                              downloader=lambda *_: self.fail("Repeated installation downloaded skills"), apply=True)
            self.assertEqual(before, snapshot(target))
            # Removing package defaults must not silently disable a selected assistant.
            upstream = source / "config/workspace-config.yml"
            upstream.write_text("workspace: package\ntarget_environment: local\n"
                                "repo_role: application\nslack_channel_name: shared-channel\n")
            installer.upgrade(target, source=source, home=base / "home", apply=True)
            self.assertIn("cline: true", (target / "config/workspace-config.yml").read_text())
            self.assertTrue(cline.is_symlink())

    def test_enabling_disabling_and_modified_link_preservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = self.source(base, "  codex: true\n  cline: false\n")
            target = base / "project"
            installer.install(target, source=source, home=base / "home", skills=True, downloader=self.pack)
            config = target / "config/workspace-config.yml"
            config.write_text(config.read_text().replace("cline: false", "cline: true"))
            before = snapshot(target)
            installer.upgrade(target, source=source, home=base / "home")
            after = snapshot(target)
            self.assertEqual({k: v for k, v in before.items() if k != "INSTALL-FOLLOWUP.md"},
                             {k: v for k, v in after.items() if k != "INSTALL-FOLLOWUP.md"})
            cline = target / ".cline/skills/example"
            self.assertFalse(cline.exists())
            installer.upgrade(target, source=source, home=base / "home", apply=True)
            self.assertTrue(cline.is_symlink())
            before = snapshot(target)
            installer.upgrade(target, source=source, home=base / "home", apply=True)
            self.assertEqual(before, snapshot(target))
            config.write_text(config.read_text().replace("cline: true", "cline: false"))
            installer.upgrade(target, source=source, home=base / "home", apply=True)
            self.assertFalse(cline.is_symlink())
            self.assertTrue((target / ".agents/skills/addy-example").is_symlink())
            config.write_text(config.read_text().replace("cline: false", "cline: true"))
            installer.upgrade(target, source=source, home=base / "home", apply=True)
            cline.unlink()
            cline.mkdir()
            (cline / "SKILL.md").write_text("Project-owned replacement\n")
            config.write_text(config.read_text().replace("cline: true", "cline: false"))
            installer.upgrade(target, source=source, home=base / "home", apply=True)
            self.assertEqual((cline / "SKILL.md").read_text(), "Project-owned replacement\n")
            self.assertIn(".cline/skills/example", (target / "INSTALL-FOLLOWUP.md").read_text())

    def test_invalid_environment_config_fails_before_writes(self):
        for mapping in ("  unknown: true\n", "  codex: yes\n", "  codex: 'true'\n",
                        "  codex: true\n  codex: false\n", "  claude_code: true\n"):
            with self.subTest(mapping=mapping), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                source = self.source(base, mapping)
                target = base / "project"
                with self.assertRaises(ValueError):
                    installer.install(target, source=source, home=base / "home", apply=True)
                self.assertFalse(target.exists())

    def test_legacy_config_and_changed_pack_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = self.source(base, "")
            target = base / "project"
            installer.install(target, source=source, home=base / "home", skills=True, downloader=self.pack)
            self.assertTrue((target / ".agents/skills/addy-example").is_symlink())
            self.assertFalse((target / ".cline").exists())
            upstream_config = source / "config/workspace-config.yml"
            upstream_config.write_text(upstream_config.read_text() + "agentic_envs:\n  codex: true\n  cline: true\n")
            installer.upgrade(target, source=source, home=base / "home", apply=True)
            self.assertFalse((target / ".cline/skills/example").exists())
            config = target / "config/workspace-config.yml"
            text = config.read_text()
            if "agentic_envs:" not in text:
                text += "agentic_envs:\n  codex: true\n  cline: true\n"
            else:
                text = text.replace("cline: false", "cline: true")
            config.write_text(text)
            manifest = target / "skills/addyosmani-agent-skills/skills/example/SKILL.md"
            manifest.write_text("Locally edited pack\n")
            installer.upgrade(target, source=source, home=base / "home", apply=True)
            self.assertFalse((target / ".cline/skills/example").exists())
            self.assertEqual(manifest.read_text(), "Locally edited pack\n")

    def test_existing_cline_skills_and_external_paths_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = self.source(base, "  codex: true\n  cline: false\n")
            target = base / "project"
            home = base / "home"
            installer.install(target, source=source, home=home, skills=True, downloader=self.pack)
            config = target / "config/workspace-config.yml"
            config.write_text(config.read_text().replace("cline: false", "cline: true"))
            global_skill = home / ".cline/skills/custom/SKILL.md"
            global_skill.parent.mkdir(parents=True)
            global_skill.write_text("Existing skill\n")
            installer.upgrade(target, source=source, home=home, apply=True)
            self.assertFalse((target / ".cline/skills/example").exists())
            self.assertIn("global skills", (target / "INSTALL-FOLLOWUP.md").read_text())
            self.assertEqual(global_skill.read_text(), "Existing skill\n")
            global_skill.unlink()
            alternate = target / ".claude/skills/custom/SKILL.md"
            alternate.parent.mkdir(parents=True)
            alternate.write_text("Shared existing skill\n")
            installer.upgrade(target, source=source, home=home, apply=True)
            self.assertFalse((target / ".cline/skills/example").exists())
            self.assertIn("alternate skill directory", (target / "INSTALL-FOLLOWUP.md").read_text())

            outside = base / "external-ignore"
            outside.write_text("Do not modify\n")
            ignore = target / ".gitignore"
            ignore.unlink()
            ignore.symlink_to(outside)
            before = snapshot(target)
            with self.assertRaises(ValueError):
                installer.upgrade(target, source=source, home=home, apply=True)
            self.assertEqual(before, snapshot(target))
            self.assertEqual(outside.read_text(), "Do not modify\n")


if __name__ == "__main__":
    unittest.main()
