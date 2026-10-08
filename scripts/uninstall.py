#!/usr/bin/env python3
"""Remove Agent Canvas from a project without touching application code."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat


MANAGED = (
    "AGENTS.md",
    "config/workspace-config.yml",
    ".owner-override.example",
    "skills/addyosmani-agent-skills.ref",
    "agents/review-coordinator.md",
)
STATE = ".agent-canvas/state.json"
FOLLOWUP = "INSTALL-FOLLOWUP.md"
BEGIN = "<!-- agent-canvas:upgrade:start -->"
END = "<!-- agent-canvas:upgrade:end -->"
IGNORE = {"/.owner-override", "/.agents/skills/addy-*/", "/skills/addyosmani-agent-skills/"}
ADAPTER_DIRS = {".agents/skills", ".cline/skills"}


def root_path(target):
    root = Path(target).expanduser()
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Target must be an existing, non-symlink project directory")
    return root.resolve()


def safe_path(root, path):
    path = Path(path)
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise ValueError(f"Path is outside the project: {path}") from error
    if ".." in relative.parts:
        raise ValueError(f"Path is outside the project: {path}")
    for ancestor in (path, *path.parents):
        if ancestor == root:
            return
        if ancestor != path and ancestor.is_symlink():
            raise ValueError(f"Preserving symlink; resolve manually: {ancestor.relative_to(root)}")
    raise ValueError(f"Path is outside the project: {path}")


def regular_text_snapshot(root, path):
    """Return text and inode identity from a no-follow regular-file descriptor."""
    safe_path(root, path)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except OSError as error:
        raise ValueError(f"Cannot safely read regular file: {path}") from error
    identity = os.fstat(descriptor)
    if not stat.S_ISREG(identity.st_mode) or identity.st_nlink != 1:
        os.close(descriptor)
        raise ValueError(f"Cannot safely read regular file: {path}")
    with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
        return handle.read(), (identity.st_dev, identity.st_ino)


def read_regular_text(root, path):
    """Read a descriptor-pinned regular file without following a replacement link."""
    return regular_text_snapshot(root, path)[0]


def replace_regular_snapshot(root, path, text, identity):
    """Rewrite only the exact regular file that was read for this operation."""
    safe_path(root, path)
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except OSError as error:
        raise ValueError(f"Cannot safely update regular file: {path}") from error
    with os.fdopen(descriptor, "r+", encoding="utf-8") as handle:
        current = os.fstat(handle.fileno())
        if (not stat.S_ISREG(current.st_mode)
                or current.st_nlink != 1
                or (current.st_dev, current.st_ino) != identity):
            raise ValueError(f"Cannot safely update regular file: {path}")
        handle.seek(0)
        handle.truncate()
        handle.write(text)


def remove_path(root, path, *, recursive=True):
    """Delete relative to no-follow directory descriptors, including cleanup."""
    relative = path.relative_to(root)
    if not relative.parts or ".." in relative.parts:
        raise ValueError(f"Cannot remove the project root or an outside path: {path}")
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise ValueError("Safe descriptor-relative removal is unavailable")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptors = []
    try:
        descriptors.append(os.open(root, flags))
        for name in relative.parts[:-1]:
            descriptors.append(os.open(name, flags, dir_fd=descriptors[-1]))
        parent = descriptors[-1]
        name = relative.name
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if stat.S_ISDIR(current.st_mode) and not recursive:
            os.rmdir(name, dir_fd=parent)
        elif stat.S_ISDIR(current.st_mode):
            if not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
                raise ValueError("Safe recursive removal is unavailable")
            # The safe rmtree implementation pins each descendant and rejects
            # symlinks substituted between its lstat and directory open.
            shutil.rmtree(name, dir_fd=parent)
        else:
            # unlink never follows the leaf, even if it becomes a symlink.
            os.unlink(name, dir_fd=parent)
        remove_empty_parents(relative, descriptors)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def remove_empty_parents(relative, descriptors):
    """Only prune empty parents through the descriptors used for removal."""
    for index in range(len(descriptors) - 1, 0, -1):
        try:
            # rmdir does not follow a substituted symlink, and atomically
            # refuses nonempty directories without a preceding listing race.
            os.rmdir(relative.parts[index - 1], dir_fd=descriptors[index - 1])
        except OSError:
            return


def read_state(root, *, allow_damaged=False):
    state_path = root / STATE
    try:
        safe_path(root, state_path)
        try:
            state_mode = state_path.lstat().st_mode
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(state_mode):
            raise ValueError("Cannot safely read a non-regular .agent-canvas/state.json")
        # The lstat above is only a fast rejection. Read through a pinned,
        # no-follow descriptor so a replacement cannot redirect parsing.
        state = json.loads(read_regular_text(root, state_path))
        if not valid_state(state):
            raise ValueError
        return state
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        if allow_damaged:
            return None
        raise ValueError("Cannot safely read .agent-canvas/state.json; use --mode remove-all after inspecting it") from error


def valid_state(state):
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        return False
    baselines = state.get("baselines")
    if not isinstance(baselines, dict) or any(
            not isinstance(name, str) or not isinstance(value, (str, type(None)))
            for name, value in baselines.items()):
        return False
    provenance = state.get("provenance")
    if provenance is not None and (
            not isinstance(provenance, dict)
            or any(not isinstance(provenance.get(name, []), list)
                   or not all(isinstance(value, str) for value in provenance.get(name, []))
                   for name in ("managed_files", "gitignore_entries"))):
        return False
    adapters = state.get("adapters")
    if adapters is not None:
        if not isinstance(adapters, dict) or not isinstance(adapters.get("links"), dict):
            return False
        if any(not isinstance(relative, str) or not isinstance(expected, str)
               for relative, expected in adapters["links"].items()):
            return False
        if "pack" in adapters and not isinstance(adapters["pack"], dict):
            return False
    return True


def adapter_links(state, *, require_installer_target=True):
    """Return only state entries whose paths are known adapter link locations."""
    adapters = state.get("adapters") if isinstance(state, dict) else None
    links = adapters.get("links") if isinstance(adapters, dict) else None
    if not isinstance(links, dict):
        return {}
    owned = {}
    for relative, expected in links.items():
        if not (isinstance(relative, str) and isinstance(expected, str)
                and str(Path(relative)) == relative
                and str(Path(relative).parent) in ADAPTER_DIRS):
            continue
        directory = str(Path(relative).parent)
        name = Path(relative).name
        skill_name = name.removeprefix("addy-") if directory == ".agents/skills" else name
        target = Path("../../skills/addyosmani-agent-skills/skills") / skill_name
        if (name not in {"", ".", ".."}
                and (directory != ".agents/skills" or name.startswith("addy-"))
                and skill_name
                and (not require_installer_target or expected == str(target))):
            owned[relative] = expected
    return owned


def pack_fingerprint(pack):
    if pack.is_symlink():
        return None
    entries = {}
    for base, dirs, files in os.walk(pack, followlinks=False):
        for name in dirs + files:
            path = Path(base) / name
            if path.is_symlink() and (
                    path.relative_to(pack).as_posix() != ".opencode/skills"
                    or os.readlink(path) not in {"../skills", "../skills/"}
                    or (pack / "skills").is_symlink()
                    or not (pack / "skills").is_dir()):
                return None
        dirs[:] = sorted(name for name in dirs if name != ".git" and not (Path(base) / name).is_symlink())
        for name in sorted(files):
            path = Path(base) / name
            if path.is_symlink() or not path.is_file():
                return None
            entries[str(path.relative_to(pack))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()


def unrecorded_adapter_references_pack(root, state, pack):
    """Return whether an unrecorded supported adapter symlink points into pack."""
    # Entries rejected by adapter_links() cannot authorize removal and must not
    # hide a surviving link from this reference scan.
    recorded = set(adapter_links(state))
    try:
        pack_root = pack.resolve(strict=True)
    except OSError:
        return False
    for directory in ADAPTER_DIRS:
        adapter_dir = root / directory
        safe_path(root, adapter_dir)
        if not adapter_dir.is_dir() or adapter_dir.is_symlink():
            continue
        for base, dirs, files in os.walk(adapter_dir, followlinks=False):
            for name in dirs + files:
                path = Path(base) / name
                relative = path.relative_to(root).as_posix()
                if relative in recorded or not path.is_symlink():
                    continue
                try:
                    path.resolve(strict=False).relative_to(pack_root)
                    return True
                except (OSError, ValueError):
                    pass
    return False


def installer_adapter_links(root, pack):
    """Return discoverable standard adapter links that point at ``pack``."""
    try:
        pack_root = pack.resolve(strict=True)
    except OSError:
        return []
    found = []
    for directory in ADAPTER_DIRS:
        adapter_dir = root / directory
        safe_path(root, adapter_dir)
        if not adapter_dir.is_dir() or adapter_dir.is_symlink():
            continue
        for path in adapter_dir.iterdir():
            name = path.name
            skill_name = name.removeprefix("addy-") if directory == ".agents/skills" else name
            expected = Path("../../skills/addyosmani-agent-skills/skills") / skill_name
            if (not path.is_symlink()
                    or not skill_name
                    or (directory == ".agents/skills" and not name.startswith("addy-"))
                    or os.readlink(path) != str(expected)):
                continue
            try:
                path.resolve(strict=False).relative_to(pack_root)
                found.append(path)
            except (OSError, ValueError):
                pass
    return found


def followup_without_agent_canvas_block(text):
    if BEGIN not in text and END not in text:
        return text, False
    if text.count(BEGIN) != 1 or text.count(END) != 1 or text.index(BEGIN) >= text.index(END):
        return text, None
    before = text[:text.index(BEGIN)]
    after = text[text.index(END) + len(END):]
    if before.endswith(("\r\n\r\n", "\n\n")):
        if after.startswith("\r\n\r\n"):
            after = after[4:]
        elif after.startswith("\n\n"):
            after = after[2:]
    return before + after, True


def planned_removal(root, path, actions, apply):
    safe_path(root, path)
    if not path.exists() and not path.is_symlink():
        return
    actions.append(f"{'REMOVE' if apply else 'WOULD REMOVE'} {path.relative_to(root)}")
    if apply:
        remove_path(root, path)


def planned_regular_removal(root, path, actions, apply, identity):
    """Preserve a verified regular file when portable identity-safe unlink is unavailable."""
    safe_path(root, path)
    if not path.exists() and not path.is_symlink():
        return False
    if apply:
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            return False
        try:
            current = os.fstat(descriptor)
            if (not stat.S_ISREG(current.st_mode)
                    or (current.st_dev, current.st_ino) != identity):
                return False
        finally:
            os.close(descriptor)
        # POSIX exposes no portable unlink-by-descriptor operation. Once this
        # descriptor is closed, unlinking ``path`` could delete an attacker
        # replacement, even if it is another regular file. Preserve it rather
        # than claiming a pathname-based delete is identity-safe. The preview
        # reports the same outcome, so it never promises a removal apply cannot
        # safely perform.
        actions.append(f"PRESERVE {path.relative_to(root)}: cleanup requires --mode remove-all")
        return True
    actions.append(f"PRESERVE {path.relative_to(root)}: cleanup requires --mode remove-all")
    return True


def remove_ignore_entries(root, state, actions, apply, *, remove_all=False):
    path = root / ".gitignore"
    safe_path(root, path)
    if not path.exists():
        return
    if path.is_symlink():
        actions.append("PRESERVE .gitignore: it is a symlink")
        return
    if not path.is_file():
        actions.append("PRESERVE .gitignore: it is not a regular file")
        return
    provenance = state.get("provenance")
    if isinstance(provenance, dict):
        entries = set(provenance.get("gitignore_entries", []))
    else:
        # Legacy state cannot prove which ignore entries the installer created.
        entries = set()
    allowed = set(IGNORE)
    allowed.update("/.cline/skills/" + Path(relative).name
                   for relative in adapter_links(state)
                   if relative.startswith(".cline/skills/"))
    entries &= allowed
    if remove_all:
        # Explicit removal authorizes these exact fixed package entries even
        # when damaged/missing history cannot establish their provenance.
        entries.update(IGNORE)
    try:
        old, identity = regular_text_snapshot(root, path)
    except ValueError:
        actions.append("PRESERVE .gitignore: it changed during cleanup")
        return
    kept = [line for line in old.splitlines(keepends=True) if line.rstrip("\r\n") not in entries]
    new = "".join(kept)
    if old != new:
        if apply:
            try:
                replace_regular_snapshot(root, path, new, identity)
            except ValueError:
                actions.append("PRESERVE .gitignore: it changed during cleanup")
                return
        actions.append(f"{'UPDATE' if apply else 'WOULD UPDATE'} .gitignore: remove Agent Canvas ignore entries")


def remove_followup_block(root, actions, apply):
    path = root / FOLLOWUP
    safe_path(root, path)
    if not path.exists():
        return
    if path.is_symlink():
        actions.append("PRESERVE INSTALL-FOLLOWUP.md: it is a symlink")
        return
    if not path.is_file():
        actions.append("PRESERVE INSTALL-FOLLOWUP.md: it is not a regular file")
        return
    try:
        old, identity = regular_text_snapshot(root, path)
    except ValueError:
        actions.append("PRESERVE INSTALL-FOLLOWUP.md: it changed during cleanup")
        return
    updated, found = followup_without_agent_canvas_block(old)
    if found is None:
        actions.append("PRESERVE INSTALL-FOLLOWUP.md: Agent Canvas markers are malformed")
    elif found:
        if apply:
            try:
                # Keep an empty regular file rather than unlinking a pathname
                # that could have been replaced after its pinned read.
                replace_regular_snapshot(root, path, updated, identity)
            except ValueError:
                actions.append("PRESERVE INSTALL-FOLLOWUP.md: it changed during cleanup")
                return
        actions.append(f"{'UPDATE' if apply else 'WOULD UPDATE'} INSTALL-FOLLOWUP.md: remove Agent Canvas section")


def uninstall(target, *, mode="preserve", apply=False):
    """Return a preview/apply action list for an Agent Canvas removal."""
    if mode not in {"preserve", "remove-all"}:
        raise ValueError("mode must be preserve or remove-all")
    root = root_path(target)
    state_path = root / STATE
    state = read_state(root, allow_damaged=mode == "remove-all") if (state_path.exists() or state_path.is_symlink()) else None
    actions = []
    baselines = state.get("baselines", {}) if state else {}
    provenance = state.get("provenance") if state else None
    managed_files = set(provenance.get("managed_files", [])) if isinstance(provenance, dict) else set()

    for name in MANAGED:
        path = root / name
        safe_path(root, path)
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_symlink():
            if mode == "remove-all":
                planned_removal(root, path, actions, apply)
            else:
                actions.append(f"PRESERVE {name}: it is a symlink")
            continue
        identity = None
        if name in baselines and baselines[name] is not None and name in managed_files:
            try:
                contents, identity = regular_text_snapshot(root, path)
            except ValueError:
                contents = None
            owned_unchanged = contents == baselines[name]
        else:
            owned_unchanged = False
        if mode == "remove-all":
            planned_removal(root, path, actions, apply)
        elif owned_unchanged:
            if not planned_regular_removal(root, path, actions, apply, identity):
                actions.append(f"PRESERVE {name}: it changed during cleanup")
        else:
            actions.append(f"PRESERVE {name}: not proven to be an unchanged Agent Canvas file")

    pack = root / "skills/addyosmani-agent-skills"
    pack_referenced_by_modified_adapter = False
    adapters = adapter_links(state)
    for relative, expected in adapters.items():
        path = root / relative
        safe_path(root, path)
        exact = path.is_symlink() and os.readlink(path) == expected
        if exact:
            if mode == "remove-all":
                planned_removal(root, path, actions, apply)
            else:
                # As with regular files, there is no portable unlink of the
                # verified object. Never recursively remove a replacement.
                actions.append(f"PRESERVE {relative}: cleanup requires --mode remove-all")
        elif path.exists() or path.is_symlink():
            actions.append(f"PRESERVE {relative}: owned link was changed")
            try:
                path.resolve(strict=False).relative_to(pack.resolve())
                pack_referenced_by_modified_adapter = True
            except (OSError, ValueError):
                pass

    if mode == "remove-all":
        for path in installer_adapter_links(root, pack):
            planned_removal(root, path, actions, apply)

    # A legacy or malformed adapter record cannot authorize unlinking a link,
    # but a changed link under a supported adapter directory can still retain a
    # reference to this managed pack. Keep the pack in that ambiguous case.
    if mode != "remove-all":
        links = state.get("adapters", {}).get("links", {}) if isinstance(state, dict) else {}
        if isinstance(links, dict):
            for relative, expected in links.items():
                if not (isinstance(relative, str) and isinstance(expected, str)
                        and str(Path(relative)) == relative
                        and str(Path(relative).parent) in ADAPTER_DIRS):
                    continue
                path = root / relative
                safe_path(root, path)
                if not path.is_symlink() or os.readlink(path) == expected:
                    continue
                try:
                    path.resolve(strict=False).relative_to(pack.resolve())
                    pack_referenced_by_modified_adapter = True
                except (OSError, ValueError):
                    pass
        if unrecorded_adapter_references_pack(root, state, pack):
            pack_referenced_by_modified_adapter = True

    adapters_state = state.get("adapters") if isinstance(state, dict) else None
    pack_record = adapters_state.get("pack") if isinstance(adapters_state, dict) else None
    pack_matches = bool(pack_record and pack.is_dir() and pack_fingerprint(pack) == pack_record.get("fingerprint"))
    if pack.exists() or pack.is_symlink():
        if pack.is_symlink():
            if mode == "remove-all":
                planned_removal(root, pack, actions, apply)
            else:
                actions.append("PRESERVE skills/addyosmani-agent-skills: it is a symlink")
            pack = None
    if pack and pack.exists():
        if mode == "remove-all":
            planned_removal(root, pack, actions, apply)
        elif pack_referenced_by_modified_adapter:
            actions.append("PRESERVE skills/addyosmani-agent-skills: preserved adapter link may reference it")
        elif pack_matches:
            # A fingerprint cannot bind a later recursive pathname deletion to
            # the directory inspected here. Preserve even unchanged packs;
            # remove-all explicitly authorizes removing the current pathname.
            actions.append("PRESERVE skills/addyosmani-agent-skills: cleanup requires --mode remove-all")
        else:
            actions.append("PRESERVE skills/addyosmani-agent-skills: not proven to be an unchanged Agent Canvas pack")

    if state or mode == "remove-all":
        remove_ignore_entries(root, state or {}, actions, apply, remove_all=mode == "remove-all")
    if mode == "remove-all":
        planned_removal(root, root / FOLLOWUP, actions, apply)
    elif state:
        remove_followup_block(root, actions, apply)
    if mode == "preserve" and state:
        actions.append("PRESERVE .agent-canvas/state.json: state cleanup requires --mode remove-all")
    elif state or mode == "remove-all":
        state_root = state_path.parent
        planned_removal(root, state_root if not state_root.is_dir() or state_root.is_symlink() else state_path,
                        actions, apply)
    if not actions:
        actions.append("NO AGENT CANVAS ARTIFACTS FOUND")
    return apply, actions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path, help="existing project directory")
    parser.add_argument("--mode", choices=("preserve", "remove-all"), default="preserve",
                        help="preserve unproven external files, or remove all known Agent Canvas paths")
    parser.add_argument("--apply", action="store_true", help="perform the previewed removal")
    args = parser.parse_args()
    try:
        active, actions = uninstall(args.target, mode=args.mode, apply=args.apply)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Uninstaller stopped: {error}\n")
    print("Removal applied." if active else "PREVIEW: no files were changed; re-run with --apply to remove listed artifacts.")
    print("\n".join(actions))


if __name__ == "__main__":
    main()
