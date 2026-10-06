#!/usr/bin/env python3
"""Preview or remove agent, governance, and workflow setup while retaining plans."""

import argparse
import importlib.util
from pathlib import Path
import re

_uninstall_spec = importlib.util.spec_from_file_location("agent_canvas_uninstall", Path(__file__).with_name("uninstall.py"))
_uninstall = importlib.util.module_from_spec(_uninstall_spec)
_uninstall_spec.loader.exec_module(_uninstall)
remove_empty_parents = _uninstall.remove_empty_parents
remove_path = _uninstall.remove_path
root_path = _uninstall.root_path
safe_path = _uninstall.safe_path


TARGETS = (
    "AGENTS.md", "CLAUDE.md", "GEMINI.md", ".cursorrules", ".mcp.json",
    "INSTALL-FOLLOWUP.md", ".owner-override",
    ".owner-override.example", ".agent-canvas", ".agents", ".cline", ".clinerules",
    ".claude", ".codex", ".cursor", ".github", "agents", "skills", "rules", "commands",
    "config/workspace-config.yml", "CODEOWNERS", ".github/CODEOWNERS", "docs/superpowers",
)
PLAN_PARTS = {"plan", "plans", "epic", "epics", "task", "tasks", "spec", "specs"}
PLAN_NAME = re.compile(r"(?:^|[-_.])(plan|plans|epic|epics|task|tasks|spec|specs)(?:[-_.]|$)", re.I)


def is_plan(path, root):
    relative = path.relative_to(root)
    return any(part.lower() in PLAN_PARTS for part in relative.parts) or bool(PLAN_NAME.search(path.name))


def nuke_path(root, path, actions, apply):
    safe_path(root, path)
    if not path.exists() and not path.is_symlink():
        return
    if is_plan(path, root):
        actions.append(f"PRESERVE PLAN {path.relative_to(root)}")
        return
    if path.is_symlink() or path.is_file():
        actions.append(f"{'REMOVE' if apply else 'WOULD REMOVE'} {path.relative_to(root)}")
        if apply:
            remove_path(path)
            remove_empty_parents(root, path)
        return
    for child in sorted(path.iterdir(), key=lambda item: item.name):
        nuke_path(root, child, actions, apply)
    if path.exists() and path.is_dir() and not any(path.iterdir()):
        actions.append(f"{'REMOVE' if apply else 'WOULD REMOVE'} {path.relative_to(root)}/")
        if apply:
            path.rmdir()
            remove_empty_parents(root, path)


def nuke(target, *, apply=False):
    root = root_path(target)
    actions = []
    for relative in TARGETS:
        path = root / relative
        if path.exists() or path.is_symlink():
            nuke_path(root, path, actions, apply)
    if not actions:
        actions.append("NO AGENT, GOVERNANCE, OR WORKFLOW ARTIFACTS FOUND")
    return apply, actions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path, help="existing project directory")
    parser.add_argument("--apply", action="store_true", help="perform the previewed reset")
    args = parser.parse_args()
    try:
        active, actions = nuke(args.target, apply=args.apply)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Agent nuke stopped: {error}\n")
    print("Agent reset applied." if active else "PREVIEW: no files were changed; re-run with --apply to remove listed artifacts.")
    print("\n".join(actions))


if __name__ == "__main__":
    main()
