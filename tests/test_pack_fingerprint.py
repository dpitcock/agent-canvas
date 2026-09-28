"""Fingerprint compatibility and link safety, using offline pack fixtures."""
import hashlib
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("pack_installer", Path(__file__).resolve().parents[1] / "scripts/install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class PackFingerprint(unittest.TestCase):
    def pack(self, base):
        pack = base / "pack"
        (pack / "skills/example").mkdir(parents=True)
        (pack / "skills/example/SKILL.md").write_text("Example skill\n")
        (pack / ".opencode").mkdir()
        return pack

    def test_known_alias_preserves_legacy_fingerprint_and_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            pack = self.pack(Path(tmp))
            expected = installer.digest({"skills/example/SKILL.md": hashlib.sha256(b"Example skill\n").hexdigest()})
            self.assertEqual(installer.pack_fingerprint(pack), expected)
            alias = pack / ".opencode/skills"
            alias.symlink_to("../skills")
            self.assertEqual(installer.pack_fingerprint(pack), expected)
            self.assertEqual(str(alias.readlink()), "../skills")
            alias.unlink()
            alias.symlink_to("../skills/")  # Literal target stored in the pinned Git tree.
            self.assertEqual(installer.pack_fingerprint(pack), expected)
            self.assertEqual(os.readlink(alias), "../skills/")
            (pack / "skills/example/SKILL.md").write_text("Changed skill\n")
            self.assertNotEqual(installer.pack_fingerprint(pack), expected)

    def test_rejects_other_links_even_with_known_alias(self):
        cases = [(".opencode/skills", "../../outside"),
                 (".opencode/skills", "../missing"),
                 (".opencode/skills", "skills"),
                 (".opencode/skills", "../skills/example/SKILL.md"),
                 (".opencode/skills", "../skills/../skills"),
                 ("alias", "skills"),
                 (".git", "skills"),
                 ("skills/example/alias", "SKILL.md"),
                 ("skills/example/cycle", "../..")]
        for name, target in cases:
            with self.subTest(name=name, target=target), tempfile.TemporaryDirectory() as tmp:
                pack = self.pack(Path(tmp))
                if name != ".opencode/skills":
                    (pack / ".opencode/skills").symlink_to("../skills")
                (pack / name).symlink_to(target)
                with self.assertRaisesRegex(ValueError, "symlinks"):
                    installer.pack_fingerprint(pack)

    def test_rejects_linked_pack_or_opencode_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            pack = self.pack(base)
            alias = base / "alias"
            alias.symlink_to(pack)
            with self.assertRaisesRegex(ValueError, "symlinks"):
                installer.pack_fingerprint(alias)
            (pack / ".opencode").rmdir()
            (pack / ".opencode").symlink_to("skills")
            with self.assertRaisesRegex(ValueError, "symlinks"):
                installer.pack_fingerprint(pack)

    def test_known_alias_requires_real_skills_directory(self):
        for target in (None, "file", "outside", "cycle"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                pack = base / "pack"
                (pack / ".opencode").mkdir(parents=True)
                (pack / ".opencode/skills").symlink_to("../skills")
                if target == "file":
                    (pack / "skills").write_text("Not a directory")
                elif target == "outside":
                    (base / "outside").mkdir()
                    (pack / "skills").symlink_to(base / "outside")
                elif target == "cycle":
                    (pack / "skills").symlink_to(".opencode/skills")
                with self.assertRaisesRegex(ValueError, "symlinks"):
                    installer.pack_fingerprint(pack)


if __name__ == "__main__":
    unittest.main()
