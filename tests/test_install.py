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
    @unittest.skipIf(os.geteuid() == 0, "Root bypasses directory write permissions")
    def test_skills_install_in_writable_project_with_read_only_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            parent = base / "read-only"
            root = parent / "project"
            root.mkdir(parents=True)

            def download(destination, revision):
                skill = destination / "skills/example"
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text("example")
                script = skill / "run.sh"
                script.write_text("#!/bin/sh\n")
                script.chmod(0o755)
                (destination / ".opencode").mkdir()
                (destination / ".opencode/skills").symlink_to("../skills")

            parent.chmod(0o555)
            try:
                with self.assertRaises(PermissionError):
                    (parent / "unwritable").mkdir()
                _, actions = installer.install(root, skills=True, home=base / "home", downloader=download)
                self.assertFalse([action for action in actions if action.startswith("FAILED")], actions)
                pack = root / "skills/addyosmani-agent-skills"
                self.assertEqual((pack / "skills/example/SKILL.md").read_text(), "example")
                self.assertEqual((pack / "skills/example/run.sh").stat().st_mode & 0o777, 0o755)
                self.assertEqual(os.readlink(pack / ".opencode/skills"), "../skills")
                self.assertTrue((root / ".agents/skills/addy-example").is_symlink())
            finally:
                parent.chmod(0o755)

    def test_provision_rejects_project_replaced_inside_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "project"
            root.mkdir()
            selected = root.stat()
            spec = importlib.util.spec_from_file_location("identity_test_supervisor", installer.SOURCE / "scripts/supervisor.py")
            supervisor = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(supervisor)
            project_identity = supervisor.HostSupervisor._project_identity

            def replace_then_identify(project):
                root.rename(base / "original")
                root.mkdir()
                return project_identity(project)

            with patch.object(supervisor.HostSupervisor, "_project_identity", side_effect=replace_then_identify):
                with self.assertRaisesRegex(ValueError, "Project directory changed"):
                    supervisor.HostSupervisor(base / "host-state").provision(
                        root, expected_identity=(selected.st_dev, selected.st_ino))
            self.assertFalse((base / "host-state").exists())

    def test_adapter_mutations_preserve_replacement_project(self):
        for operation in ("add", "remove"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "project"
                root.mkdir()
                root, identity = installer.select_project(root)
                root.rename(root.with_name("original"))
                link = root / ".agents/skills/addy-example"
                link.parent.mkdir(parents=True)
                if operation == "remove":
                    link.symlink_to("keep-target")
                with self.assertRaisesRegex(ValueError, "Project directory changed"):
                    installer.apply_adapters([(operation, link, "new-target")], root=root, root_identity=identity)
                if operation == "remove":
                    self.assertEqual(os.readlink(link), "keep-target")
                else:
                    self.assertFalse(link.is_symlink())

    def test_install_preserves_replacement_at_late_mutation_boundaries(self):
        for hook in ("add_gitignore_entries", "apply_adapters", "save_state", "provision_supervised"):
            with self.subTest(hook=hook), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                root = base / "project"
                original_call = getattr(installer, hook)
                replaced = False

                def swap_then_call(*args, **kwargs):
                    nonlocal replaced
                    if not replaced:
                        replaced = True
                        root.rename(base / "original")
                        root.mkdir()
                        (root / "application.txt").write_text("keep")
                    return original_call(*args, **kwargs)

                with patch.object(installer, hook, side_effect=swap_then_call):
                    with self.assertRaisesRegex(ValueError, "Project directory changed"):
                        installer.install(root, home=base / "home", supervised=hook == "provision_supervised",
                                          supervisor_state_dir=base / "host-state")
                self.assertEqual(sorted(p.name for p in root.iterdir()), ["application.txt"])
                self.assertFalse((base / "host-state").exists())

    def test_download_does_not_place_pack_in_replacement_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "project"

            def download(destination, revision):
                destination.mkdir(parents=True)
                (destination / "pack.txt").write_text("downloaded")
                root.rename(base / "original")
                root.mkdir()
                (root / "application.txt").write_text("keep")

            with self.assertRaisesRegex(ValueError, "Project directory changed"):
                installer.install(root, skills=True, home=base / "home", downloader=download)
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["application.txt"])

    def test_pack_copy_does_not_follow_project_replaced_during_placement(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "project"

            def download(destination, revision):
                skill = destination / "skills/example"
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text("example")
                (skill / "notes.txt").write_text("notes")

            copy_file = installer.shutil.copyfileobj
            replaced = False

            def replace_then_copy(incoming, destination):
                nonlocal replaced
                if not replaced:
                    replaced = True
                    root.rename(base / "original")
                    root.mkdir()
                    (root / "application.txt").write_text("keep")
                return copy_file(incoming, destination)

            with patch.object(installer.shutil, "copyfileobj", side_effect=replace_then_copy):
                with self.assertRaisesRegex(ValueError, "Project directory changed"):
                    installer.install(root, skills=True, home=base / "home", downloader=download)
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["application.txt"])
            original = base / "original/skills/addyosmani-agent-skills/skills/example"
            self.assertEqual((original / "SKILL.md").read_text(), "example")
            self.assertEqual((original / "notes.txt").read_text(), "notes")

    def test_failed_pack_copy_is_reported_and_never_linked_or_tracked(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "project"

            def download(destination, revision):
                skill = destination / "skills/example"
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text("example")

            with patch.object(installer.shutil, "copyfileobj", side_effect=OSError("Disk full")):
                _, actions = installer.install(root, skills=True, home=base / "home", downloader=download)
            self.assertTrue(any(action.startswith("FAILED skill installation:") and "Disk full" in action
                                for action in actions))
            self.assertNotIn("pack", installer.read_state(root)["adapters"])
            self.assertFalse((root / ".agents/skills/addy-example").is_symlink())
            self.assertIn("FAILED skill installation:", (root / "INSTALL-FOLLOWUP.md").read_text())
            self.assertEqual(sorted(p.name for p in (root / "skills").iterdir()),
                             ["addyosmani-agent-skills.ref"])
            _, actions = installer.install(root, apply=True, skills=True, home=base / "home", downloader=download)
            self.assertFalse([action for action in actions if action.startswith("FAILED")], actions)
            self.assertEqual((root / "skills/addyosmani-agent-skills/skills/example/SKILL.md").read_text(),
                             "example")
            self.assertTrue((root / ".agents/skills/addy-example").is_symlink())
            self.assertEqual(sorted(p.name for p in (root / "skills").iterdir()),
                             ["addyosmani-agent-skills", "addyosmani-agent-skills.ref"])

    def test_invalid_download_does_not_block_valid_retry(self):
        for invalid in ("empty", "symlink"):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                root = base / "project"

                def download(destination, revision):
                    skill = destination / "skills/example"
                    skill.mkdir(parents=True)
                    (skill / "SKILL.md").write_text("example")

                def invalid_download(destination, revision):
                    download(destination, revision)
                    if invalid == "empty":
                        (destination / "skills/example/SKILL.md").unlink()
                    else:
                        (destination / "unexpected").symlink_to("skills")

                _, actions = installer.install(root, skills=True, home=base / "home", downloader=invalid_download)
                self.assertTrue(any(action.startswith("FAILED skill installation:") for action in actions))
                self.assertEqual(sorted(p.name for p in (root / "skills").iterdir()),
                                 ["addyosmani-agent-skills.ref"])
                _, actions = installer.install(root, apply=True, skills=True, home=base / "home", downloader=download)
                self.assertFalse([action for action in actions if action.startswith("FAILED")], actions)
                self.assertTrue((root / ".agents/skills/addy-example").is_symlink())

    def test_failed_copy_cleanup_stays_in_original_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "project"

            def download(destination, revision):
                skill = destination / "skills/example"
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text("example")

            def replace_then_fail(incoming, destination):
                root.rename(base / "original")
                root.mkdir()
                (root / "application.txt").write_text("keep")
                raise OSError("Disk full")

            with patch.object(installer.shutil, "copyfileobj", side_effect=replace_then_fail):
                with self.assertRaisesRegex(ValueError, "Project directory changed"):
                    installer.install(root, skills=True, home=base / "home", downloader=download)
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["application.txt"])
            self.assertEqual(sorted(p.name for p in (base / "original/skills").iterdir()),
                             ["addyosmani-agent-skills.ref"])

    def test_pack_publication_preserves_destination_created_during_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "project"
            pack = root / "skills/addyosmani-agent-skills"

            def download(destination, revision):
                skill = destination / "skills/example"
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text("example")

            copy_file = installer.shutil.copyfileobj
            created = None

            def create_then_copy(incoming, destination):
                nonlocal created
                pack.mkdir()
                created = pack.stat().st_ino
                return copy_file(incoming, destination)

            with patch.object(installer.shutil, "copyfileobj", side_effect=create_then_copy):
                _, actions = installer.install(root, skills=True, home=base / "home", downloader=download)
            self.assertTrue(any(action.startswith("FAILED skill installation:") for action in actions))
            self.assertEqual(pack.stat().st_ino, created)
            self.assertEqual(list(pack.iterdir()), [])
            self.assertNotIn("pack", installer.read_state(root)["adapters"])
            self.assertEqual(sorted(p.name for p in pack.parent.iterdir()),
                             ["addyosmani-agent-skills", "addyosmani-agent-skills.ref"])

    def test_new_project_can_be_installed_with_supervision(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "new" / "project"
            active, _ = installer.install(root, home=base / "home", supervised=True,
                                          supervisor_state_dir=base / "host-state")
            self.assertTrue(active)
            self.assertTrue(installer.read_state(root)["supervised"]["enabled"])

    def test_install_and_upgrade_preserve_replacement_project(self):
        for operation in (installer.install, installer.upgrade):
            for after_first in (False, True):
                with self.subTest(operation=operation.__name__, after_first=after_first), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp) / "project"
                    root.mkdir()
                    original = root.with_name("original")
                    swapped = False
                    render = installer.render_files
                    write = installer.write_regular_text

                    def swap():
                        nonlocal swapped
                        if not swapped:
                            swapped = True
                            root.rename(original)
                            root.mkdir()
                            (root / "application.txt").write_text("unrelated project")

                    def render_then_swap(*args, **kwargs):
                        result = render(*args, **kwargs)
                        swap()
                        return result

                    def write_then_swap(*args, **kwargs):
                        result = write(*args, **kwargs)
                        swap()
                        return result

                    hook = "write_regular_text" if after_first else "render_files"
                    with patch.object(installer, hook, side_effect=write_then_swap if after_first else render_then_swap):
                        with self.assertRaisesRegex(ValueError, "Project directory changed"):
                            operation(root, apply=True, home=Path(tmp) / "home")
                    self.assertEqual(sorted(p.name for p in root.iterdir()), ["application.txt"])
                    self.assertEqual((root / "application.txt").read_text(), "unrelated project")

    def test_install_does_not_recreate_disappeared_selected_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            render = installer.render_files

            def disappear(*args, **kwargs):
                result = render(*args, **kwargs)
                root.rename(root.with_name("original"))
                return result

            with patch.object(installer, "render_files", side_effect=disappear):
                with self.assertRaisesRegex(ValueError, "Project directory changed"):
                    installer.install(root, home=Path(tmp) / "home")
            self.assertFalse(root.exists())

    def test_cleanup_rejects_project_replacement_after_selection_and_between_deletions(self):
        for module in (uninstaller, agent_nuke):
            for after_first in (False, True):
                with self.subTest(module=module.__name__, after_first=after_first), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp).resolve() / "project"
                    root.mkdir()
                    original = root.with_name("original")
                    for relative in ("AGENTS.md", "config/workspace-config.yml"):
                        path = root / relative
                        path.parent.mkdir(exist_ok=True)
                        path.write_text("original")

                    def swap():
                        root.rename(original)
                        root.mkdir()
                        for relative in ("AGENTS.md", "config/workspace-config.yml"):
                            path = root / relative
                            path.parent.mkdir(exist_ok=True)
                            path.write_text("replacement")

                    select = module.root_path
                    remove = module.remove_path
                    swapped = False

                    def select_then_swap(*args, **kwargs):
                        result = select(*args, **kwargs)
                        swap()
                        return result

                    def remove_then_swap(*args, **kwargs):
                        nonlocal swapped
                        result = remove(*args, **kwargs)
                        if not swapped:
                            swapped = True
                            swap()
                        return result

                    hook = "remove_path" if after_first else "root_path"
                    with patch.object(module, hook, side_effect=remove_then_swap if after_first else select_then_swap):
                        try:
                            if module is uninstaller:
                                module.uninstall(root, mode="remove-all", apply=True)
                            else:
                                module.nuke(root, apply=True)
                        except (OSError, ValueError):
                            pass
                    for relative in ("AGENTS.md", "config/workspace-config.yml"):
                        self.assertTrue((root / relative).exists(), f"replacement deleted: {relative}")
                        self.assertEqual((root / relative).read_text(), "replacement")
                    self.assertEqual((original / "config/workspace-config.yml").read_text(), "original")
                    if not after_first:
                        self.assertEqual((original / "AGENTS.md").read_text(), "original")

    def test_cleanup_does_not_rewrite_files_in_replacement_project(self):
        for mode in ("preserve", "remove-all"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve() / "project"
                root.mkdir()
                original = root.with_name("original")
                select = uninstaller.root_path
                ignore = "application\n/.owner-override\n"
                followup = "notes\n" + uninstaller.BEGIN + "\nmanaged\n" + uninstaller.END + "\n"

                def swap(*args, **kwargs):
                    selected = select(*args, **kwargs)
                    root.rename(original)
                    root.mkdir()
                    (root / ".gitignore").write_text(ignore)
                    (root / "INSTALL-FOLLOWUP.md").write_text(followup)
                    (root / ".agent-canvas").mkdir()
                    (root / ".agent-canvas/state.json").write_text(json.dumps({
                        "schema_version": 1, "baselines": {},
                        "provenance": {"gitignore_entries": ["/.owner-override"]},
                    }))
                    return selected

                with patch.object(uninstaller, "root_path", side_effect=swap):
                    try:
                        uninstaller.uninstall(root, mode=mode, apply=True)
                    except (OSError, ValueError):
                        pass
                self.assertEqual((root / ".gitignore").read_text(), ignore)
                self.assertEqual((root / "INSTALL-FOLLOWUP.md").read_text(), followup)

    def test_remove_all_cleans_dangling_standard_links_without_pack_or_state(self):
        for damaged in (False, True):
            with self.subTest(damaged=damaged), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                links = []
                for directory, name in ((".agents/skills", "addy-writing-plans"), (".cline/skills", "writing-plans")):
                    link = root / directory / name
                    link.parent.mkdir(parents=True)
                    link.symlink_to("../../skills/addyosmani-agent-skills/skills/writing-plans")
                    links.append(link)
                custom = root / ".agents/skills/addy-custom"
                custom.symlink_to("../../application/skills/custom")
                if damaged:
                    state = root / ".agent-canvas/state.json"
                    state.parent.mkdir()
                    state.write_text("broken json")
                _, preview = uninstaller.uninstall(root, mode="remove-all")
                for link in links:
                    self.assertTrue(link.is_symlink())
                    self.assertTrue(any(str(link.relative_to(root)) in action for action in preview))
                uninstaller.uninstall(root, mode="remove-all", apply=True)
                for link in links:
                    self.assertFalse(link.is_symlink())
                self.assertTrue(custom.is_symlink())

    def test_reset_shared_file_replaced_by_directory_never_recurses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            target = root / "agents/review-coordinator.md"
            target.parent.mkdir()
            target.write_text("installed artifact")
            original_check = agent_nuke.safe_path
            swapped = False

            def replace_after_check(project, path):
                nonlocal swapped
                original_check(project, path)
                if path == target and not swapped:
                    swapped = True
                    target.rename(root / "old-artifact")
                    target.mkdir()
                    (target / "application.py").write_text("keep")

            with patch.object(agent_nuke, "safe_path", side_effect=replace_after_check):
                try:
                    agent_nuke.nuke(root, apply=True)
                except (OSError, ValueError):
                    pass
            self.assertTrue(swapped)
            self.assertTrue((target / "application.py").exists())
            self.assertEqual((target / "application.py").read_text(), "keep")

    def test_remove_all_rejects_ancestor_swapped_before_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            root = base / "project"
            config = root / "config"
            config.mkdir(parents=True)
            target = config / "workspace-config.yml"
            target.write_text("managed")
            outside = base / "outside"
            outside.mkdir()
            victim = outside / target.name
            victim.mkdir()
            (victim / "application.txt").write_text("keep")
            safe_path = uninstaller.safe_path
            _, root_identity = uninstaller.root_path(root, with_identity=True)
            swapped = False

            def swap_after_check(checked_root, path):
                nonlocal swapped
                safe_path(checked_root, path)
                if path == target and not swapped:
                    swapped = True
                    config.rename(root / "held")
                    config.symlink_to(outside, target_is_directory=True)

            with patch.object(uninstaller, "safe_path", swap_after_check):
                try:
                    uninstaller.planned_removal(root, target, [], True, root_identity=root_identity)
                except (OSError, ValueError):
                    pass
            self.assertTrue(swapped)
            self.assertTrue(victim.exists())
            self.assertEqual((victim / "application.txt").read_text(), "keep")

    def test_remove_all_stays_with_parent_pinned_before_swap(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            root = base / "project"
            config = root / "config"
            config.mkdir(parents=True)
            target = config / "workspace-config.yml"
            target.write_text("managed")
            outside = base / "outside"
            outside.mkdir()
            victim = outside / target.name
            victim.write_text("keep")
            held = root / "held"
            _, root_identity = uninstaller.root_path(root, with_identity=True)
            real_open = os.open

            def swap_after_open(path, flags, *args, **kwargs):
                fd = real_open(path, flags, *args, **kwargs)
                if path == "config" and flags & os.O_DIRECTORY:
                    config.rename(held)
                    config.symlink_to(outside, target_is_directory=True)
                return fd

            with patch.object(os, "open", swap_after_open):
                uninstaller.planned_removal(root, target, [], True, root_identity=root_identity)
            self.assertEqual(victim.read_text(), "keep")
            self.assertTrue(config.is_symlink())
            self.assertFalse((held / target.name).exists())

    def test_remove_all_fails_closed_without_safe_recursive_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            pack = root / "skills/addyosmani-agent-skills"
            pack.mkdir(parents=True)
            keep = pack / "SKILL.md"
            keep.write_text("keep")
            _, root_identity = uninstaller.root_path(root, with_identity=True)
            with patch.object(uninstaller.shutil.rmtree, "avoids_symlink_attacks", False):
                try:
                    uninstaller.planned_removal(root, pack, [], True, root_identity=root_identity)
                except (OSError, ValueError):
                    pass
            self.assertTrue(keep.exists())

    def test_remove_all_leaf_swapped_to_symlink_never_follows_target(self):
        for directory in (False, True):
            with self.subTest(directory=directory), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp).resolve()
                root = base / "project"
                root.mkdir()
                target = root / "AGENTS.md"
                if directory:
                    target.mkdir()
                else:
                    target.write_text("managed")
                outside = base / "outside"
                outside.mkdir()
                victim = outside / "application.txt"
                victim.write_text("keep")
                real_stat = os.stat
                _, root_identity = uninstaller.root_path(root, with_identity=True)
                swapped = False

                def swap_after_stat(path, *args, **kwargs):
                    nonlocal swapped
                    result = real_stat(path, *args, **kwargs)
                    if path == target.name and kwargs.get("dir_fd") is not None and not swapped:
                        swapped = True
                        target.rename(root / "held")
                        target.symlink_to(outside, target_is_directory=True)
                    return result

                with patch.object(os, "stat", swap_after_stat):
                    try:
                        uninstaller.planned_removal(root, target, [], True, root_identity=root_identity)
                    except (OSError, ValueError):
                        pass
                self.assertTrue(swapped)
                self.assertEqual(victim.read_text(), "keep")

    def test_remove_all_prunes_empty_parents_and_keeps_unrelated_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            config = root / "config"
            config.mkdir()
            (config / "workspace-config.yml").write_text("managed")
            (config / "application.yml").write_text("keep")
            pack = root / "skills/addyosmani-agent-skills/skills/example"
            pack.mkdir(parents=True)
            (pack / "SKILL.md").write_text("managed")
            uninstaller.uninstall(root, mode="remove-all", apply=True)
            self.assertEqual((config / "application.yml").read_text(), "keep")
            self.assertFalse((config / "workspace-config.yml").exists())
            self.assertFalse((root / "skills").exists())

    def test_preserve_adapter_replacement_directory_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "project"
            link = root / ".agents/skills/addy-example"
            link.parent.mkdir(parents=True)
            expected = "../../skills/addyosmani-agent-skills/skills/example"
            link.symlink_to(expected)
            state = root / ".agent-canvas/state.json"
            state.parent.mkdir()
            state.write_text(json.dumps({"schema_version": 1, "baselines": {},
                                         "adapters": {"links": {".agents/skills/addy-example": expected}}}))
            readlink = os.readlink

            def replace_after_read(path, *args, **kwargs):
                value = readlink(path, *args, **kwargs)
                if path == link:
                    link.unlink()
                    link.mkdir()
                    (link / "application.txt").write_text("keep")
                return value

            with patch.object(os, "readlink", replace_after_read):
                uninstaller.uninstall(root, apply=True)
            self.assertTrue((link / "application.txt").exists())

    def test_remove_all_cleans_fixed_ignore_entries_without_valid_state(self):
        for damaged in (False, True):
            with self.subTest(damaged=damaged), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                ignore = root / ".gitignore"
                original = "app-cache\n/.owner-override\n/.agents/skills/addy-*/\n/skills/addyosmani-agent-skills/\n/.cline/skills/custom\n"
                ignore.write_text(original)
                if damaged:
                    state = root / ".agent-canvas/state.json"
                    state.parent.mkdir()
                    state.write_text("broken json")
                _, preview = uninstaller.uninstall(root, mode="remove-all")
                self.assertEqual(ignore.read_text(), original)
                self.assertTrue(any(action.startswith("WOULD UPDATE .gitignore") for action in preview))
                uninstaller.uninstall(root, mode="remove-all", apply=True)
                self.assertEqual(ignore.read_text(), "app-cache\n/.cline/skills/custom\n")

    def test_missing_managed_file_creation_rejects_swapped_parent(self):
        for fresh in (False, True):
            with self.subTest(fresh=fresh), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp).resolve()
                root = base / "project"
                root.mkdir()
                config = root / "config"
                config.mkdir()
                outside = base / "outside"
                outside.mkdir()
                destination = config / "workspace-config.yml"
                safe_destination = installer.safe_destination
                swapped = False
                checks = 0

                def swap_after_check(checked_root, path):
                    nonlocal swapped, checks
                    safe_destination(checked_root, path)
                    if path == destination:
                        checks += 1
                    if path == destination and checks == (2 if fresh else 1) and not swapped:
                        swapped = True
                        config.rmdir()
                        config.symlink_to(outside, target_is_directory=True)

                with patch.object(installer, "safe_destination", swap_after_check):
                    try:
                        if fresh:
                            installer.install(root, skills=False, apply=True, home=base / "home")
                        else:
                            installer.write_regular_text(root, destination, "managed", create=True)
                    except (ValueError, OSError):
                        pass
                self.assertTrue(swapped)
                self.assertFalse((outside / "workspace-config.yml").exists())

    def test_managed_write_stays_with_pinned_parent_after_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            config = root / "config"
            config.mkdir()
            outside = root / "outside"
            outside.mkdir()
            held = root / "original-config"
            open_descriptor = os.open

            def replace_opened_parent(path, flags, *args, **kwargs):
                descriptor = open_descriptor(path, flags, *args, **kwargs)
                if path == "config" and flags & os.O_DIRECTORY:
                    config.rename(held)
                    config.symlink_to(outside, target_is_directory=True)
                return descriptor

            with patch.object(os, "open", replace_opened_parent):
                installer.write_regular_text(root, config / "workspace-config.yml", "managed", create=True)
            self.assertFalse((outside / "workspace-config.yml").exists())
            self.assertEqual((held / "workspace-config.yml").read_text(), "managed")

    def test_preserve_uninstall_keeps_hardlinked_ignore_and_followup_in_preview_and_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "project"
            installer.install(root, skills=False, home=base / "home")
            originals = {}
            for name in (".gitignore", "INSTALL-FOLLOWUP.md"):
                originals[name] = (root / name).read_text()
                os.link(root / name, base / name)
            for apply in (False, True):
                _, actions = uninstaller.uninstall(root, apply=apply)
                for name, original in originals.items():
                    self.assertEqual((base / name).read_text(), original)
                    self.assertTrue(any(action.startswith(f"PRESERVE {name}:") for action in actions))
                    self.assertFalse(any(action.startswith(f"WOULD UPDATE {name}:") for action in actions))

    def test_uninstall_writer_rejects_hardlink_created_after_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "project"
            root.mkdir()
            path = root / ".gitignore"
            path.write_text("original")
            _, identity = uninstaller.regular_text_snapshot(root, path)
            os.link(path, base / "external")
            with self.assertRaisesRegex(ValueError, "Cannot safely update"):
                uninstaller.replace_regular_snapshot(
                    root, path, "changed", identity,
                    root_identity=uninstaller.root_path(root, with_identity=True)[1])
            self.assertEqual((base / "external").read_text(), "original")

    def test_preserve_pack_replacement_after_fingerprint_survives_preview_and_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            def local_pack(destination, revision):
                skill = destination / "skills/example"
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text("example")

            fingerprint = uninstaller.pack_fingerprint
            for apply in (False, True):
                root = base / str(apply)
                installer.install(root, skills=True, home=base / "home", downloader=local_pack)
                pack = root / "skills/addyosmani-agent-skills"
                def replace_after_fingerprint(path):
                    result = fingerprint(path)
                    path.rename(root / "original-pack")
                    path.mkdir()
                    (path / "application-file").write_text("keep replacement")
                    return result
                with patch.object(uninstaller, "pack_fingerprint", replace_after_fingerprint):
                    _, actions = uninstaller.uninstall(root, apply=apply)
                self.assertEqual((pack / "application-file").read_text(), "keep replacement")
                self.assertIn("PRESERVE skills/addyosmani-agent-skills: cleanup requires --mode remove-all", actions)
                uninstaller.uninstall(root, mode="remove-all", apply=True)
                self.assertFalse(pack.exists())

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
            self.assertTrue(any(action.startswith("PRESERVE .agents/skills/") for action in actions))
            uninstaller.uninstall(preserved, mode="preserve", apply=True)
            self.assertEqual((preserved / "AGENTS.md").read_text(), "Project additions\n")
            self.assertEqual((preserved / ".gitignore").read_text(), "app-cache\n")
            self.assertTrue((preserved / ".agent-canvas/state.json").is_file())
            self.assertTrue((preserved / "skills/addyosmani-agent-skills").exists())

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

    def test_preserve_uninstall_does_not_remove_directory_replacing_owned_file_during_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            target = base / "project"
            installer.install(target, home=base / "home")
            managed = target / "AGENTS.md"
            original_removal = uninstaller.planned_regular_removal

            def replace_before_removal(root, path, actions, apply, identity):
                if path == managed.resolve():
                    managed.unlink()
                    managed.mkdir()
                    (managed / "application-file").write_text("preserve")
                return original_removal(root, path, actions, apply, identity)

            with patch.object(uninstaller, "planned_regular_removal", replace_before_removal):
                uninstaller.uninstall(target, mode="preserve", apply=True)

            self.assertTrue(managed.is_dir())
            self.assertEqual((managed / "application-file").read_text(), "preserve")

    def test_read_state_rejects_a_state_file_swapped_to_a_symlink_after_lstat(self):
        """State must be parsed from a no-follow descriptor, not a reopened path."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            state_path = root / ".agent-canvas/state.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(json.dumps({"schema_version": 1, "baselines": {}}))
            outside = Path(directory) / "outside.json"
            outside.write_text(json.dumps({"schema_version": 1, "baselines": {"outside": "state"}}))
            original_lstat = Path.lstat

            def swap_after_lstat(path, *args, **kwargs):
                result = original_lstat(path, *args, **kwargs)
                if path == state_path and not state_path.is_symlink():
                    state_path.unlink()
                    state_path.symlink_to(outside)
                return result

            with patch.object(Path, "lstat", swap_after_lstat):
                with self.assertRaisesRegex(ValueError, "Cannot safely read"):
                    uninstaller.read_state(root)

    def test_planned_regular_removal_preserves_a_regular_replacement_after_validation(self):
        """The pathname must not be unlinked after its verified fd is released."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            managed = root / "AGENTS.md"
            managed.write_text("owned")
            identity = (managed.stat().st_dev, managed.stat().st_ino)
            original_close = os.close
            swapped = False

            def swap_after_close(descriptor):
                nonlocal swapped
                original_close(descriptor)
                if not swapped:
                    swapped = True
                    managed.unlink()
                    managed.write_text("replacement")

            actions = []
            with patch.object(os, "close", swap_after_close):
                removed = uninstaller.planned_regular_removal(root, managed, actions, True, identity)

            self.assertTrue(removed)
            self.assertEqual(managed.read_text(), "replacement")
            self.assertEqual(actions, ["PRESERVE AGENTS.md: cleanup requires --mode remove-all"])

    def test_preserve_preview_does_not_promise_a_racy_regular_file_removal(self):
        """Preserve previews must describe the same fail-closed outcome as apply."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            managed = root / "AGENTS.md"
            managed.write_text("owned")
            identity = (managed.stat().st_dev, managed.stat().st_ino)

            actions = []
            handled = uninstaller.planned_regular_removal(root, managed, actions, False, identity)

            self.assertTrue(handled)
            self.assertEqual(actions, ["PRESERVE AGENTS.md: cleanup requires --mode remove-all"])

    def test_preserve_uninstall_does_not_remove_state_replaced_after_read(self):
        """Preserve mode must not delete a state path after it has been read."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            state_path = root / ".agent-canvas/state.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(json.dumps({"schema_version": 1, "baselines": {}}))

            def replace_state(*_args, **_kwargs):
                state_path.unlink()
                state_path.mkdir()
                (state_path / "user-file").write_text("preserve")
                return False

            with patch.object(uninstaller, "remove_followup_block", replace_state):
                uninstaller.uninstall(root, mode="preserve", apply=True)

            self.assertTrue(state_path.is_dir())
            self.assertEqual((state_path / "user-file").read_text(), "preserve")

    def test_remove_all_unlinks_dangling_managed_file_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            target.mkdir()
            managed = target / "AGENTS.md"
            managed.symlink_to("missing-agent-canvas-file")

            uninstaller.uninstall(target, mode="remove-all", apply=True)

            self.assertFalse(managed.exists() or managed.is_symlink())

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

    def test_preserve_uninstall_keeps_unchanged_pack_with_approved_opencode_alias(self):
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

            self.assertTrue((target / "skills/addyosmani-agent-skills").exists())

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

    def test_remove_all_recovers_state_without_adapter_links(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            state_path = target / ".agent-canvas/state.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(json.dumps({
                "schema_version": 1, "baselines": {}, "adapters": {},
            }))
            managed = target / "AGENTS.md"
            managed.write_text("managed")
            unrelated = target / "application.txt"
            unrelated.write_text("keep")

            _, preview = uninstaller.uninstall(target, mode="remove-all", apply=False)
            self.assertIn("WOULD REMOVE AGENTS.md", preview)
            self.assertTrue(managed.exists())
            self.assertTrue(state_path.exists())
            uninstaller.uninstall(target, mode="remove-all", apply=True)
            self.assertFalse(managed.exists())
            self.assertFalse(state_path.exists())
            self.assertEqual(unrelated.read_text(), "keep")

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

    def test_uninstall_rejects_fifo_state_without_reading_it(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            state_path = target / ".agent-canvas/state.json"
            state_path.parent.mkdir(parents=True)
            os.mkfifo(state_path)

            with patch.object(Path, "read_text", side_effect=AssertionError("state FIFO was read")):
                with self.assertRaisesRegex(ValueError, "Cannot safely read"):
                    uninstaller.uninstall(target, mode="preserve", apply=True)
                _, actions = uninstaller.uninstall(target, mode="remove-all", apply=True)

            self.assertFalse(state_path.exists())
            self.assertIn("REMOVE .agent-canvas/state.json", actions)

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
            self.assertTrue(state_path.is_file())
            self.assertIn("PRESERVE .gitignore: it is not a regular file", actions)
            self.assertIn("PRESERVE .agent-canvas/state.json: state cleanup requires --mode remove-all", actions)

    def test_apply_adapters_rechecks_gitignore_after_preflight_before_reading(self):
        """A .gitignore swapped after planning must not be opened as a FIFO/dir."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            link = root / ".cline/skills/example"
            ignore = root / ".gitignore"
            link.parent.mkdir(parents=True)

            # Model the gap after a successful preflight and before apply.
            installer.safe_destination(root, ignore)
            ignore.mkdir()

            with patch.object(Path, "read_text", side_effect=AssertionError("non-regular .gitignore was read")):
                with self.assertRaisesRegex(ValueError, "regular file"):
                    installer.apply_adapters([("add", link, "../../skills/example")])

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
            self.assertTrue(state_path.is_file())
            self.assertIn("PRESERVE INSTALL-FOLLOWUP.md: it is not a regular file", actions)
            self.assertIn("PRESERVE .agent-canvas/state.json: state cleanup requires --mode remove-all", actions)

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

    def test_read_state_rejects_adapter_link_whose_name_disagrees_with_target_skill(self):
        """A recorded link may only name the skill it targets."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            state_path = root / ".agent-canvas/state.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(json.dumps({
                "schema_version": 1,
                "options": {"workspace": "project", "environment": "local", "role": "application", "slack": ""},
                "baselines": {}, "pending": {}, "resolutions": [],
                "adapters": {"links": {
                    ".agents/skills/addy-unrelated": "../../skills/addyosmani-agent-skills/skills/example",
                }, "pending": []},
            }))

            with self.assertRaisesRegex(ValueError, "upgrade history"):
                installer.read_state(root)

    def test_gitignore_descriptor_helper_rejects_fifo_without_opening_it(self):
        """A path swapped to a FIFO cannot make install wait or write elsewhere."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            ignore = root / ".gitignore"
            os.mkfifo(ignore)

            with self.assertRaisesRegex(ValueError, "regular file"):
                installer.add_gitignore_entries(root, ignore, ["/.owner-override"])

    def test_uninstall_regular_text_helper_rejects_fifo_without_opening_it(self):
        """Cleanup refuses a substituted FIFO instead of blocking on it."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            followup = root / "INSTALL-FOLLOWUP.md"
            os.mkfifo(followup)

            with self.assertRaisesRegex(ValueError, "regular file"):
                uninstaller.read_regular_text(root, followup)

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
            (root / "agents/review-coordinator.md").write_text("remove")
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

    def test_agent_nuke_preserves_unknown_files_in_shared_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preserved = (
                "agents/worker.py", "skills/catalog.json", "rules/pricing.py",
                "commands/import.py", "agents/review.md",
                "skills/addyosmani-agent-skills/user-note.md",
            )
            removed = ("agents/review-coordinator.md", "skills/addyosmani-agent-skills.ref")
            for relative in (*preserved, *removed):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(relative)

            _, preview = agent_nuke.nuke(root)
            for relative in preserved:
                self.assertNotIn(f"WOULD REMOVE {relative}", preview)
            for relative in removed:
                self.assertTrue((root / relative).exists())
                self.assertIn(f"WOULD REMOVE {relative}", preview)

            agent_nuke.nuke(root, apply=True)
            for relative in preserved:
                self.assertEqual((root / relative).read_text(), relative)
            for relative in removed:
                self.assertFalse((root / relative).exists())

    def test_agent_nuke_removes_plan_named_framework_skills(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for owner in (".agents", ".cline", ".codex", ".claude", ".cursor"):
                skill = root / owner / "skills/writing-plans/SKILL.md"
                skill.parent.mkdir(parents=True)
                skill.write_text("framework implementation")
            for relative in ("plans/current.md", "epics/current.md", "tasks/current.md",
                             "specs/current.md", "docs/superpowers/specs/design.md",
                             "docs/superpowers/implementation-plan.md"):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("project work")

            agent_nuke.nuke(root, apply=True)

            for owner in (".agents", ".cline", ".codex", ".claude", ".cursor"):
                self.assertFalse((root / owner).exists())
            for relative in ("plans/current.md", "epics/current.md", "tasks/current.md",
                             "specs/current.md", "docs/superpowers/specs/design.md",
                             "docs/superpowers/implementation-plan.md"):
                self.assertEqual((root / relative).read_text(), "project work")

    def test_agent_nuke_preserves_shared_symlinks_and_directory_replacements(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            outside = Path(directory) / "outside"
            root.mkdir()
            outside.mkdir()
            (outside / "review-coordinator.md").write_text("outside")
            (root / "agents").symlink_to(outside, target_is_directory=True)
            replacement = root / "skills/addyosmani-agent-skills.ref/user-data.txt"
            replacement.parent.mkdir(parents=True)
            replacement.write_text("keep")

            agent_nuke.nuke(root, apply=True)

            self.assertTrue((root / "agents").is_symlink())
            self.assertEqual((outside / "review-coordinator.md").read_text(), "outside")
            self.assertEqual(replacement.read_text(), "keep")

    def test_agent_nuke_preserves_special_file_replacements(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            special = root / "AGENTS.md"
            os.mkfifo(special)
            (root / "CLAUDE.md").write_text("remove")

            _, actions = agent_nuke.nuke(root, apply=True)

            self.assertTrue(special.exists())
            self.assertFalse((root / "CLAUDE.md").exists())
            self.assertIn("PRESERVE SPECIAL AGENTS.md", actions)


if __name__ == "__main__":
    unittest.main()
