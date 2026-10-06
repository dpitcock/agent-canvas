#!/usr/bin/env python3
"""Remove Agent Canvas from a project without touching application code."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil


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


def remove_path(path):
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def remove_empty_parents(root, path):
    parent = path.parent
    while parent != root:
        if parent.is_symlink() or not parent.is_dir() or any(parent.iterdir()):
            return
        parent.rmdir()
        parent = parent.parent


def read_state(root):
    state_path = root / STATE
    safe_path(root, state_path)
    if not state_path.exists():
        return None
    if state_path.is_symlink():
        raise ValueError("Cannot safely read a symlinked .agent-canvas/state.json")
    try:
        state = json.loads(state_path.read_text())
        if state.get("schema_version") != 1 or not isinstance(state.get("baselines"), dict):
            raise ValueError
        return state
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("Cannot safely read .agent-canvas/state.json; use --mode remove-all after inspecting it") from error


def pack_fingerprint(pack):
    entries = {}
    for base, dirs, files in os.walk(pack, followlinks=False):
        dirs[:] = sorted(name for name in dirs if name != ".git" and not (Path(base) / name).is_symlink())
        for name in sorted(files):
            path = Path(base) / name
            if path.is_symlink() or not path.is_file():
                return None
            entries[str(path.relative_to(pack))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()


def followup_without_agent_canvas_block(text):
    if BEGIN not in text and END not in text:
        return text, False
    if text.count(BEGIN) != 1 or text.count(END) != 1 or text.index(BEGIN) >= text.index(END):
        return text, None
    updated = (text[:text.index(BEGIN)] + text[text.index(END) + len(END):]).strip()
    return (updated + "\n") if updated else "", True


def planned_removal(root, path, actions, apply):
    safe_path(root, path)
    if not path.exists() and not path.is_symlink():
        return
    actions.append(f"{'REMOVE' if apply else 'WOULD REMOVE'} {path.relative_to(root)}")
    if apply:
        remove_path(path)
        remove_empty_parents(root, path)


def remove_ignore_entries(root, state, actions, apply):
    path = root / ".gitignore"
    safe_path(root, path)
    if not path.exists():
        return
    if path.is_symlink():
        actions.append("PRESERVE .gitignore: it is a symlink")
        return
    provenance = state.get("provenance")
    if provenance is not None:
        entries = set(provenance.get("gitignore_entries", []))
    else:
        # State written before provenance tracking used the historical fixed set.
        entries = set(IGNORE)
        for relative in state.get("adapters", {}).get("links", {}):
            if relative.startswith(".cline/skills/"):
                entries.add("/.cline/skills/" + Path(relative).name)
    old = path.read_text()
    kept = [line for line in old.splitlines(keepends=True) if line.rstrip("\r\n") not in entries]
    new = "".join(kept)
    if old != new:
        actions.append(f"{'UPDATE' if apply else 'WOULD UPDATE'} .gitignore: remove Agent Canvas ignore entries")
        if apply:
            path.write_text(new)


def remove_followup_block(root, actions, apply):
    path = root / FOLLOWUP
    safe_path(root, path)
    if not path.exists():
        return
    if path.is_symlink():
        actions.append("PRESERVE INSTALL-FOLLOWUP.md: it is a symlink")
        return
    updated, found = followup_without_agent_canvas_block(path.read_text())
    if found is None:
        actions.append("PRESERVE INSTALL-FOLLOWUP.md: Agent Canvas markers are malformed")
    elif found:
        actions.append(f"{'UPDATE' if apply else 'WOULD UPDATE'} INSTALL-FOLLOWUP.md: remove Agent Canvas section")
        if apply:
            if updated:
                path.write_text(updated)
            else:
                path.unlink()


def uninstall(target, *, mode="preserve", apply=False):
    """Return a preview/apply action list for an Agent Canvas removal."""
    if mode not in {"preserve", "remove-all"}:
        raise ValueError("mode must be preserve or remove-all")
    root = root_path(target)
    state = read_state(root) if (root / STATE).exists() else None
    actions = []
    baselines = state.get("baselines", {}) if state else {}
    provenance = state.get("provenance") if state else None
    managed_files = set(provenance.get("managed_files", [])) if provenance is not None else None

    for name in MANAGED:
        path = root / name
        safe_path(root, path)
        if not path.exists():
            continue
        if path.is_symlink():
            if mode == "remove-all":
                planned_removal(root, path, actions, apply)
            else:
                actions.append(f"PRESERVE {name}: it is a symlink")
            continue
        owned_unchanged = (name in baselines and baselines[name] is not None
                           and path.read_text() == baselines[name]
                           and (managed_files is None or name in managed_files))
        if mode == "remove-all" or owned_unchanged:
            planned_removal(root, path, actions, apply)
        else:
            actions.append(f"PRESERVE {name}: not proven to be an unchanged Agent Canvas file")

    adapters = state.get("adapters", {}).get("links", {}) if state else {}
    for relative, expected in adapters.items():
        path = root / relative
        safe_path(root, path)
        exact = path.is_symlink() and os.readlink(path) == expected
        if mode == "remove-all" or exact:
            planned_removal(root, path, actions, apply)
        elif path.exists() or path.is_symlink():
            actions.append(f"PRESERVE {relative}: owned link was changed")

    pack = root / "skills/addyosmani-agent-skills"
    pack_record = state.get("adapters", {}).get("pack") if state else None
    pack_matches = bool(pack_record and pack.is_dir() and pack_fingerprint(pack) == pack_record.get("fingerprint"))
    if pack.exists() or pack.is_symlink():
        if pack.is_symlink():
            if mode == "remove-all" and state:
                planned_removal(root, pack, actions, apply)
            else:
                actions.append("PRESERVE skills/addyosmani-agent-skills: it is a symlink")
            pack = None
    if pack and pack.exists():
        if mode == "remove-all" and state:
            planned_removal(root, pack, actions, apply)
        elif pack_matches:
            planned_removal(root, pack, actions, apply)
        else:
            actions.append("PRESERVE skills/addyosmani-agent-skills: not proven to be an unchanged Agent Canvas pack")

    if state:
        remove_ignore_entries(root, state, actions, apply)
    if mode == "remove-all":
        planned_removal(root, root / FOLLOWUP, actions, apply)
    elif state:
        remove_followup_block(root, actions, apply)
    if state:
        planned_removal(root, root / STATE, actions, apply)
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
