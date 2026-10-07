"""One offline smoke test: no subprocesses, network, or nested runners."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("installer", Path(__file__).resolve().parents[1] / "scripts/install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)

uninstall_spec = importlib.util.spec_from_file_location("uninstaller", Path(__file__).resolve().parents[1] / "scripts/uninstall.py")
uninstaller = importlib.util.module_from_spec(uninstall_spec)
uninstall_spec.loader.exec_module(uninstaller)

nuke_spec = importlib.util.spec_from_file_location("agent_nuke", Path(__file__).resolve().parents[1] / "scripts/agent-nuke.py")
agent_nuke = importlib.util.module_from_spec(nuke_spec)
nuke_spec.loader.exec_module(agent_nuke)


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
            self.assertIn('repo_role: "application"', (fresh / "config/workspace-config.yml").read_text())
            self.assertTrue((fresh / ".agents/skills/addy-example/SKILL.md").is_file())
            self.assertEqual((fresh / ".owner-override.example").read_bytes(),
                             (installer.SOURCE / ".owner-override.example").read_bytes())
            self.assertFalse((fresh / ".owner-override").exists())
            self.assertEqual((fresh / "agents/review-coordinator.md").read_bytes(),
                             (installer.SOURCE / "agents/review-coordinator.md").read_bytes())
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
            (existing / ".owner-override").write_text('OWNER_OVERRIDE="pause"\n')
            (existing / ".owner-override.example").write_text("# Project-specific example\n")
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
            self.assertEqual((existing / ".owner-override").read_bytes(), original[Path(".owner-override")])
            self.assertEqual((existing / ".owner-override.example").read_bytes(), original[Path(".owner-override.example")])
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

            # Installing another project must not inherit an override from the
            # source toolkit, a sibling project, a parent, the home, or the env.
            source = base / "toolkit-source"
            for name in installer.MANAGED:
                destination = source / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes((installer.SOURCE / name).read_bytes())

            def snapshot(path):
                return {p.relative_to(path): p.read_bytes() for p in path.rglob("*") if p.is_file()}

            baseline = base / "clean-project"
            with patch.dict(os.environ, {"OWNER_OVERRIDE": ""}):
                installer.install(baseline, source=source, home=home, workspace="isolation-test")
            baseline_files = snapshot(baseline)
            foreign_overrides = [source / ".owner-override", base / ".owner-override",
                                 home / ".config/agent-governance/override"]
            for path in foreign_overrides:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('OWNER_OVERRIDE="pause,bypass-review,reset" # foreign override\n')
            isolated = base / "isolated-project"
            original_open = Path.open

            def reject_foreign_reads(path, *args, **kwargs):
                self.assertNotIn(path, foreign_overrides + [existing / ".owner-override"])
                return original_open(path, *args, **kwargs)

            with patch.dict(os.environ, {"OWNER_OVERRIDE": "pause,bypass-review,reset"}):
                with patch.object(Path, "open", reject_foreign_reads):
                    installer.install(isolated, source=source, home=home, workspace="isolation-test")
            self.assertEqual(snapshot(isolated), baseline_files)
            self.assertFalse((isolated / ".owner-override").exists())
            self.assertEqual((existing / ".owner-override").read_bytes(), original[Path(".owner-override")])

    def test_uninstall_can_force_remove_or_preserve_modified_shared_files(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            home = base / "empty-home"

            def local_pack(destination, revision):
                skill = destination / "skills/example"
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text("example")

            preserved = base / "preserved"
            installer.install(preserved, skills=True, home=home, downloader=local_pack)
            (preserved / "AGENTS.md").write_text("Project additions\n")
            (preserved / ".gitignore").write_text("app-cache\n" + (preserved / ".gitignore").read_text())
            active, actions = uninstaller.uninstall(preserved, mode="preserve")
            self.assertFalse(active)
            self.assertTrue(any(action.startswith("WOULD REMOVE") for action in actions))
            uninstaller.uninstall(preserved, mode="preserve", apply=True)
            self.assertEqual((preserved / "AGENTS.md").read_text(), "Project additions\n")
            self.assertEqual((preserved / ".gitignore").read_text(), "app-cache\n")
            self.assertFalse((preserved / ".agent-canvas").exists())
            self.assertFalse((preserved / "skills/addyosmani-agent-skills").exists())

            forced = base / "forced"
            installer.install(forced, skills=False, home=home)
            (forced / "AGENTS.md").write_text("Modified package rules\n")
            uninstaller.uninstall(forced, mode="remove-all", apply=True)
            self.assertFalse((forced / "AGENTS.md").exists())
            self.assertFalse((forced / "config/workspace-config.yml").exists())
            self.assertFalse((forced / "INSTALL-FOLLOWUP.md").exists())

    def test_preserve_uninstall_keeps_pack_with_added_directory_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)

            def local_pack(destination, revision):
                skill = destination / "skills/example"
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text("example")

            target = base / "project"
            installer.install(target, skills=True, home=base / "home", downloader=local_pack)
            pack = target / "skills/addyosmani-agent-skills"
            link = pack / "skills/example/added-directory-link"
            link.symlink_to("..", target_is_directory=True)

            _, actions = uninstaller.uninstall(target, mode="preserve", apply=True)

            self.assertTrue(pack.exists())
            self.assertTrue(link.is_symlink())
            self.assertIn("PRESERVE skills/addyosmani-agent-skills: not proven to be an unchanged Agent Canvas pack", actions)

    def test_preserve_uninstall_keeps_matching_legacy_baseline_without_created_file_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            target.mkdir()
            content = (installer.SOURCE / "AGENTS.md").read_text()
            (target / "AGENTS.md").write_text(content)
            state_path = target / ".agent-canvas/state.json"
            state_path.parent.mkdir()
            state_path.write_text(json.dumps({
                "schema_version": 1,
                "baselines": {"AGENTS.md": content},
            }))

            uninstaller.uninstall(target, mode="preserve", apply=True)

            self.assertEqual((target / "AGENTS.md").read_text(), content)

    def test_preserve_uninstall_keeps_legacy_ignore_entries_without_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            target.mkdir()
            (target / ".gitignore").write_text("/.owner-override\n")
            state_path = target / ".agent-canvas/state.json"
            state_path.parent.mkdir()
            state_path.write_text(json.dumps({"schema_version": 1, "baselines": {}}))

            uninstaller.uninstall(target, mode="preserve", apply=True)

            self.assertEqual((target / ".gitignore").read_text(), "/.owner-override\n")

    def test_preserve_uninstall_keeps_pack_referenced_by_modified_adapter_link(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            pack = target / "skills/addyosmani-agent-skills"
            (pack / "skills/example").mkdir(parents=True)
            (pack / "skills/other").mkdir()
            (pack / "skills/example/SKILL.md").write_text("example")
            (pack / "skills/other/SKILL.md").write_text("other")
            link = target / ".agents/skills/example"
            link.parent.mkdir(parents=True)
            link.symlink_to("../../skills/addyosmani-agent-skills/skills/other")
            state_path = target / ".agent-canvas/state.json"
            state_path.parent.mkdir()
            state_path.write_text(json.dumps({
                "schema_version": 1,
                "baselines": {},
                "adapters": {
                    "links": {".agents/skills/example": "../../skills/addyosmani-agent-skills/skills/example"},
                    "pending": [],
                    "pack": {"fingerprint": uninstaller.pack_fingerprint(pack), "revision": "a" * 40},
                },
            }))

            uninstaller.uninstall(target, mode="preserve", apply=True)

            self.assertTrue(pack.exists())
            self.assertTrue(link.is_symlink())

    def test_managed_file_replaced_with_directory_is_preserved_or_removed_by_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)

            for mode, should_exist in (("preserve", True), ("remove-all", False)):
                with self.subTest(mode=mode):
                    target = base / mode
                    installer.install(target, home=base / "home")
                    managed = target / "AGENTS.md"
                    managed.unlink()
                    managed.mkdir()

                    uninstaller.uninstall(target, mode=mode, apply=True)

                    self.assertEqual(managed.exists(), should_exist)
                    if should_exist:
                        self.assertTrue(managed.is_dir())

    def test_preserve_uninstall_keeps_pack_referenced_by_unrecorded_adapter_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)

            def local_pack(destination, revision):
                skill = destination / "skills/example"
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text("example")

            target = base / "project"
            installer.install(target, skills=True, home=base / "home", downloader=local_pack)
            pack = target / "skills/addyosmani-agent-skills"
            alias = target / ".cline/skills/unrecorded"
            alias.parent.mkdir(parents=True, exist_ok=True)
            alias.symlink_to("../../skills/addyosmani-agent-skills/skills/example")

            uninstaller.uninstall(target, mode="preserve", apply=True)

            self.assertTrue(pack.exists())
            self.assertTrue(alias.is_symlink())

    def test_preserve_uninstall_keeps_pack_for_rejected_recorded_adapter_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)

            def local_pack(destination, revision):
                skill = destination / "skills/example"
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text("example")

            target = base / "project"
            installer.install(target, skills=True, home=base / "home", downloader=local_pack)
            pack = target / "skills/addyosmani-agent-skills"
            alias = target / ".agents/skills/example"
            alias.symlink_to("../../skills/addyosmani-agent-skills/skills/example")
            state_path = target / ".agent-canvas/state.json"
            state = json.loads(state_path.read_text())
            state["adapters"]["links"][".agents/skills/example"] = (
                "../../skills/addyosmani-agent-skills/skills/example"
            )
            state_path.write_text(json.dumps(state))

            uninstaller.uninstall(target, mode="preserve", apply=True)

            self.assertTrue(pack.exists())
            self.assertTrue(alias.is_symlink())

    def test_preserve_uninstall_removes_unchanged_pack_with_approved_opencode_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)

            def local_pack(destination, revision):
                skill = destination / "skills/example"
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text("example")
                (destination / ".opencode").mkdir()
                (destination / ".opencode/skills").symlink_to("../skills")

            target = base / "project"
            installer.install(target, skills=True, home=base / "home", downloader=local_pack)

            uninstaller.uninstall(target, mode="preserve", apply=True)

            self.assertFalse((target / "skills/addyosmani-agent-skills").exists())

    def test_remove_all_ignores_adapter_path_that_escapes_project(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            target = base / "project"
            target.mkdir()
            victim = base / "victim"
            victim.write_text("do not remove")
            state_path = target / ".agent-canvas/state.json"
            state_path.parent.mkdir()
            state_path.write_text(json.dumps({
                "schema_version": 1,
                "baselines": {},
                "adapters": {"links": {"../victim": "adapter-target"}},
            }))

            uninstaller.uninstall(target, mode="remove-all", apply=True)

            self.assertEqual(victim.read_text(), "do not remove")
            self.assertFalse(state_path.exists())

    def test_remove_all_ignores_adapter_records_outside_supported_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            target = base / "project"
            target.mkdir()
            owned = target / ".agents/skills/addy-example"
            owned.parent.mkdir(parents=True)
            owned.symlink_to("../../skills/addyosmani-agent-skills/skills/example", target_is_directory=True)
            unrelated = target / ".claude/skills/victim"
            unrelated.parent.mkdir(parents=True)
            unrelated.write_text("do not remove")
            state_path = target / ".agent-canvas/state.json"
            state_path.parent.mkdir()
            state_path.write_text(json.dumps({
                "schema_version": 1,
                "baselines": {},
                "adapters": {"links": {
                    ".agents/skills/addy-example": "../../skills/addyosmani-agent-skills/skills/example",
                    ".claude/skills/victim": "adapter-target",
                }},
            }))

            uninstaller.uninstall(target, mode="remove-all", apply=True)

            self.assertFalse(owned.exists() or owned.is_symlink())
            self.assertEqual(unrelated.read_text(), "do not remove")

    def test_remove_all_preserves_adapter_path_when_state_target_is_not_installer_shaped(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            target.mkdir()
            adapter = target / ".agents/skills/addy-example"
            adapter.parent.mkdir(parents=True)
            adapter.symlink_to("../../application-skills/example", target_is_directory=True)
            state_path = target / ".agent-canvas/state.json"
            state_path.parent.mkdir()
            state_path.write_text(json.dumps({
                "schema_version": 1,
                "baselines": {},
                "adapters": {"links": {
                    ".agents/skills/addy-example": "../../application-skills/example",
                }},
            }))

            uninstaller.uninstall(target, mode="remove-all", apply=True)

            self.assertTrue(adapter.is_symlink())
            self.assertEqual(os.readlink(adapter), "../../application-skills/example")

    def test_remove_all_preserves_adapter_directory_despite_installer_shaped_state(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            target.mkdir()
            adapter = target / ".agents/skills/addy-example"
            adapter.mkdir(parents=True)
            (adapter / "application-file").write_text("preserve")
            state_path = target / ".agent-canvas/state.json"
            state_path.parent.mkdir()
            state_path.write_text(json.dumps({
                "schema_version": 1,
                "baselines": {},
                "adapters": {"links": {
                    ".agents/skills/addy-example": "../../skills/addyosmani-agent-skills/skills/example",
                }},
            }))

            uninstaller.uninstall(target, mode="remove-all", apply=True)

            self.assertEqual((adapter / "application-file").read_text(), "preserve")

    def test_preserve_uninstall_keeps_adapter_link_with_non_installer_target(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            target.mkdir()
            link = target / ".agents/skills/company"
            link.parent.mkdir(parents=True)
            link.symlink_to("../../elsewhere")
            state_path = target / ".agent-canvas/state.json"
            state_path.parent.mkdir()
            state_path.write_text(json.dumps({
                "schema_version": 1,
                "baselines": {},
                "adapters": {"links": {".agents/skills/company": "../../elsewhere"}},
            }))

            uninstaller.uninstall(target, mode="preserve", apply=True)

            self.assertTrue(link.is_symlink())

    def test_remove_all_recovers_from_damaged_state_using_fixed_targets_only(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            target = base / "project"
            target.mkdir()
            (target / "AGENTS.md").write_text("remove")
            adapter = target / ".agents/skills/untrusted-record"
            adapter.parent.mkdir(parents=True)
            adapter.write_text("preserve")
            state_path = target / ".agent-canvas/state.json"
            state_path.parent.mkdir()
            state_path.write_text("{not valid json")

            uninstaller.uninstall(target, mode="remove-all", apply=True)

            self.assertFalse((target / "AGENTS.md").exists())
            self.assertFalse(state_path.exists())
            self.assertEqual(adapter.read_text(), "preserve")

    def test_remove_all_recovers_standard_adapter_links_without_state(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            pack = target / "skills/addyosmani-agent-skills"
            (pack / "skills/example").mkdir(parents=True)
            (pack / "skills/example/SKILL.md").write_text("example")
            codex_link = target / ".agents/skills/addy-example"
            cline_link = target / ".cline/skills/example"
            unrelated = target / ".agents/skills/addy-unrelated"
            codex_link.parent.mkdir(parents=True)
            cline_link.parent.mkdir(parents=True)
            codex_link.symlink_to("../../skills/addyosmani-agent-skills/skills/example")
            cline_link.symlink_to("../../skills/addyosmani-agent-skills/skills/example")
            unrelated.symlink_to("../../elsewhere")

            uninstaller.uninstall(target, mode="remove-all", apply=True)

            self.assertFalse(pack.exists())
            self.assertFalse(codex_link.exists() or codex_link.is_symlink())
            self.assertFalse(cline_link.exists() or cline_link.is_symlink())
            self.assertTrue(unrelated.is_symlink())

    def test_remove_all_recovers_when_state_has_wrong_field_types(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            target.mkdir()
            (target / "AGENTS.md").write_text("remove")
            (target / "skills/addyosmani-agent-skills").mkdir(parents=True)
            state_path = target / ".agent-canvas/state.json"
            state_path.parent.mkdir()
            state_path.write_text(json.dumps({
                "schema_version": 1,
                "baselines": {},
                "provenance": "not an object",
            }))

            uninstaller.uninstall(target, mode="remove-all", apply=True)

            self.assertFalse((target / "AGENTS.md").exists())
            self.assertFalse(state_path.exists())

    def test_remove_all_recovers_from_invalid_adapter_pack_record(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            target.mkdir()
            (target / "AGENTS.md").write_text("remove")
            (target / "skills/addyosmani-agent-skills").mkdir(parents=True)
            state_path = target / ".agent-canvas/state.json"
            state_path.parent.mkdir()
            state_path.write_text(json.dumps({
                "schema_version": 1,
                "baselines": {},
                "adapters": {"links": {}, "pack": "not an object"},
            }))

            uninstaller.uninstall(target, mode="remove-all", apply=True)

            self.assertFalse((target / "AGENTS.md").exists())
            self.assertFalse(state_path.exists())

    def test_remove_all_unlinks_a_symlinked_state_directory_without_following_it(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            target = base / "project"
            target.mkdir()
            (target / "AGENTS.md").write_text("remove")
            external = base / "external-state"
            external.mkdir()
            (external / "state.json").write_text("{not valid json")
            (target / ".agent-canvas").symlink_to(external, target_is_directory=True)

            uninstaller.uninstall(target, mode="remove-all", apply=True)

            self.assertFalse((target / ".agent-canvas").exists() or (target / ".agent-canvas").is_symlink())
            self.assertEqual((external / "state.json").read_text(), "{not valid json")

    def test_preserve_uninstall_ignores_unrecognized_gitignore_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            target.mkdir()
            (target / ".gitignore").write_text("important-entry\n")
            state_path = target / ".agent-canvas/state.json"
            state_path.parent.mkdir()
            state_path.write_text(json.dumps({
                "schema_version": 1,
                "baselines": {},
                "provenance": {"gitignore_entries": ["important-entry"]},
            }))

            uninstaller.uninstall(target, mode="preserve", apply=True)

            self.assertEqual((target / ".gitignore").read_text(), "important-entry\n")

    def test_preserve_uninstall_keeps_non_regular_gitignore_and_completes_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            target.mkdir()
            (target / ".gitignore").mkdir()
            state_path = target / ".agent-canvas/state.json"
            state_path.parent.mkdir()
            state_path.write_text(json.dumps({
                "schema_version": 1,
                "baselines": {},
                "provenance": {"managed_files": [], "gitignore_entries": ["/.owner-override"]},
            }))

            _, actions = uninstaller.uninstall(target, mode="preserve", apply=True)

            self.assertTrue((target / ".gitignore").is_dir())
            self.assertFalse(state_path.exists())
            self.assertIn("PRESERVE .gitignore: it is not a regular file", actions)

    def test_preserve_uninstall_keeps_non_regular_followup_and_completes_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            target.mkdir()
            (target / "INSTALL-FOLLOWUP.md").mkdir()
            state_path = target / ".agent-canvas/state.json"
            state_path.parent.mkdir()
            state_path.write_text(json.dumps({
                "schema_version": 1,
                "baselines": {},
                "provenance": {"managed_files": [], "gitignore_entries": []},
            }))

            _, actions = uninstaller.uninstall(target, mode="preserve", apply=True)

            self.assertTrue((target / "INSTALL-FOLLOWUP.md").is_dir())
            self.assertFalse(state_path.exists())
            self.assertIn("PRESERVE INSTALL-FOLLOWUP.md: it is not a regular file", actions)

    def test_preserve_uninstall_keeps_preexisting_matching_files_and_ignore_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            target = base / "project"
            target.mkdir()
            (target / "AGENTS.md").write_text((installer.SOURCE / "AGENTS.md").read_text())
            preexisting_ignore = installer.IGNORE[0]
            (target / ".gitignore").write_text(preexisting_ignore + "\n")

            installer.install(target, apply=True, home=base / "home")
            state = installer.read_state(target)
            self.assertNotIn("AGENTS.md", state["provenance"]["managed_files"])
            self.assertNotIn(preexisting_ignore, state["provenance"]["gitignore_entries"])
            self.assertIn("config/workspace-config.yml", state["provenance"]["managed_files"])

            uninstaller.uninstall(target, mode="preserve", apply=True)

            self.assertTrue((target / "AGENTS.md").exists())
            self.assertEqual((target / ".gitignore").read_text(), preexisting_ignore + "\n")

    def test_preserve_uninstall_accepts_installer_shaped_codex_adapter_target(self):
        state = {
            "adapters": {
                "links": {
                    ".agents/skills/addy-example": "../../skills/addyosmani-agent-skills/skills/example",
                    ".cline/skills/example": "../../skills/addyosmani-agent-skills/skills/example",
                },
            },
        }

        self.assertEqual(uninstaller.adapter_links(state), state["adapters"]["links"])

    def test_followup_block_removal_preserves_surrounding_whitespace(self):
        original = (
            "    Project-owned indented note\n\n"
            f"{uninstaller.BEGIN}\nAgent Canvas content\n{uninstaller.END}\n\n"
            "Trailing project note\n\n"
        )

        updated, found = uninstaller.followup_without_agent_canvas_block(original)

        self.assertTrue(found)
        self.assertEqual(updated, "    Project-owned indented note\n\nTrailing project note\n\n")

    def test_agent_nuke_preserves_plans_but_removes_agent_workflow_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            (root / "plans/current").mkdir(parents=True)
            (root / "plans/current/implementation-plan.md").write_text("keep")
            (root / "docs/superpowers/specs").mkdir(parents=True)
            (root / "docs/superpowers/specs/design.md").write_text("keep")
            (root / ".agents/skills/example").mkdir(parents=True)
            (root / ".agents/skills/example/SKILL.md").write_text("remove")
            (root / "agents").mkdir()
            (root / "agents/review.md").write_text("remove")
            (root / ".github/workflows").mkdir(parents=True)
            (root / ".github/workflows/ci.yml").write_text("remove")
            (root / "AGENTS.md").write_text("remove")
            (root / "app.py").write_text("keep")
            agent_nuke.nuke(root, apply=True)
            self.assertTrue((root / "plans/current/implementation-plan.md").is_file())
            self.assertTrue((root / "docs/superpowers/specs/design.md").is_file())
            self.assertFalse((root / ".agents").exists())
            self.assertFalse((root / "agents").exists())
            self.assertFalse((root / ".github").exists())
            self.assertFalse((root / "AGENTS.md").exists())
            self.assertEqual((root / "app.py").read_text(), "keep")

    def test_agent_nuke_preserves_root_application_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            (root / "settings.json").write_text('{"theme": "dark"}')
            (root / "config.toml").write_text('[application]\nport = 8080\n')
            (root / "hooks.json").write_text('{"hooks": ["pre-commit"]}')
            (root / "AGENTS.md").write_text("remove")

            agent_nuke.nuke(root, apply=True)

            self.assertEqual((root / "settings.json").read_text(), '{"theme": "dark"}')
            self.assertEqual((root / "config.toml").read_text(), '[application]\nport = 8080\n')
            self.assertEqual((root / "hooks.json").read_text(), '{"hooks": ["pre-commit"]}')
            self.assertFalse((root / "AGENTS.md").exists())


if __name__ == "__main__":
    unittest.main()
