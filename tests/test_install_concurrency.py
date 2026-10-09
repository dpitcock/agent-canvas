import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

installer = load("install")
nuke = load("agent-nuke")


class InstallConcurrency(unittest.TestCase):
    def test_stale_snapshot_rejects_in_place_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "AGENTS.md"
            path.write_text("before")
            _, identity = installer.regular_text_snapshot(root, path)
            path.write_text("edited")
            with self.assertRaises(ValueError):
                installer.write_regular_text(root, path, "stale merge", create=True, identity=identity)
            self.assertEqual(path.read_text(), "edited")

    def test_ignore_change_after_read_is_preserved_and_not_reported_successful(self):
        for replacement in (False, True):
            with self.subTest(replacement=replacement), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / ".gitignore"
                path.write_text("old\n")
                real_fdopen = installer.os.fdopen
                class Reader:
                    def __init__(self, handle):
                        self.handle = handle
                    def __enter__(self):
                        self.handle.__enter__()
                        return self
                    def __exit__(self, *args):
                        return self.handle.__exit__(*args)
                    def __getattr__(self, key):
                        return getattr(self.handle, key)
                    def read(self, *args):
                        text = self.handle.read(*args)
                        if replacement:
                            other = root / "replacement"
                            other.write_text("owner replacement\n")
                            other.replace(path)
                        else:
                            with path.open("a") as out:
                                out.write("owner addition\n")
                        return text
                def opened(fd, mode, **kwargs):
                    handle = real_fdopen(fd, mode, **kwargs)
                    return Reader(handle) if "r" in mode else handle
                with patch.object(installer.os, "fdopen", opened):
                    with self.assertRaises(ValueError):
                        installer.add_gitignore_entries(root, path, ["/new-tool-entry"])
                self.assertEqual(path.read_text(), "owner replacement\n" if replacement else "old\nowner addition\n")

    def test_symlinked_github_preview_matches_apply_without_touching_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "project"
            root.mkdir()
            external = base / "external"
            external.mkdir()
            (external / "CODEOWNERS").write_text("owner")
            (root / ".github").symlink_to(external, target_is_directory=True)
            _, preview = nuke.nuke(root)
            self.assertEqual(preview, ["WOULD REMOVE .github"])
            self.assertTrue((root / ".github").is_symlink())
            _, applied = nuke.nuke(root, apply=True)
            self.assertEqual(applied, ["REMOVE .github"])
            self.assertEqual((external / "CODEOWNERS").read_text(), "owner")
