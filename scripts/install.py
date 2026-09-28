#!/usr/bin/env python3
"""Install Agent Canvas additively; leave semantic conflicts for the project owner."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

SOURCE = Path(__file__).resolve().parents[1]
IGNORE = ("/.owner-override", "/.agents/skills/addy-*/", "/skills/addyosmani-agent-skills/")
SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", ".npm-cache"}


def inventory(root):
    """Read names only; never execute or interpret existing agent instructions."""
    found = []
    for base, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP)
        for name in sorted(files):
            path = Path(base) / name
            rel = path.relative_to(root)
            if name in {"AGENTS.md", "CLAUDE.md", "GEMINI.md", "SKILL.md", ".cursorrules",
                        ".mcp.json", "hooks.json", "settings.json", "config.toml"} or (
                    any(p in {"agents", ".clinerules", "commands", "rules"} for p in rel.parts)
                    and path.suffix in {".md", ".mdc", ".toml", ".json"}):
                found.append(str(rel))
        # os.walk does not descend through skill discovery symlinks.
        for name in dirs:
            path = Path(base) / name
            if path.is_symlink() and (path / "SKILL.md").is_file():
                found.append(str(path.relative_to(root) / "SKILL.md"))
    return sorted(set(found))


def safe_destination(root, path):
    for part in (path, *path.parents):
        if part == root:
            return
        if part.is_symlink():
            raise ValueError(f"Preserving symlink; resolve manually: {part}")
        if part != path and part.exists() and not part.is_dir():
            raise ValueError(f"Parent is not a directory: {part}")
    raise ValueError(f"Destination is outside project: {path}")


def download_skills(destination, revision):
    """Download only; do not run any upstream installer, hook, or test."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".agent-canvas-download-", dir=destination.parent) as tmp:
        checkout = Path(tmp) / "checkout"
        subprocess.run(["git", "clone", "https://github.com/addyosmani/agent-skills.git",
                        str(checkout)], check=True)
        subprocess.run(["git", "-C", str(checkout), "checkout", "--detach", revision], check=True)
        if not list((checkout / "skills").glob("*/SKILL.md")):
            raise ValueError("Downloaded pack has no skills")
        checkout.rename(destination)


def install(target, *, apply=False, skills=False, workspace=None, environment="local",
            role="application", slack="", source=SOURCE, home=None, downloader=download_skills):
    root = Path(target).expanduser().resolve()
    if root.exists() and not root.is_dir():
        raise ValueError("Target must be a directory")
    existing = root.exists() and any(p.name != ".git" for p in root.iterdir())
    active = apply or not existing
    discovered = inventory(root) if root.exists() else []
    home = Path.home() if home is None else Path(home)
    global_skills = []
    for parent in (home / ".agents/skills", home / ".codex/skills"):
        if parent.is_dir():
            global_skills.extend(str(p) for p in parent.glob("*/SKILL.md"))
    # A script cannot identify equivalent renamed skills reliably. Defer instead of duplicating.
    has_skills = any(p.endswith("SKILL.md") for p in discovered) or bool(global_skills)
    rules = (source / "AGENTS.md").read_text()
    if role == "application":
        start, end = rules.index("## Lanes"), rules.index("## Scope, risk, and environment")
        rules = rules[:start] + (
            "## Lanes\n"
            "- Internal docs, tests, and tooling use a short task description and focused verification.\n"
            "- Product changes follow the scope, risk, and target-environment rules below.\n"
            "- Distributed templates, adopter policy, bootstrap, and CI changes are versioned and reviewed at the PR.\n"
            "- CODEOWNERS routes ownership; it neither implements role approvals nor authorizes merging.\n\n"
        ) + rules[end:]
    config = (
        f"workspace: {json.dumps(workspace or root.name)}\n"
        "approvals_required:\n  code_reviewer: true\n  appsec: true\n  qa: true\n"
        f"target_environment: {environment}\nrepo_role: {role}\n"
        f"slack_channel_name: {json.dumps(slack)}\n"
    )
    files = {"AGENTS.md": rules, "config/workspace-config.yml": config,
             ".owner-override.example": (source / ".owner-override.example").read_text(),
             "skills/addyosmani-agent-skills.ref": (source / "skills/addyosmani-agent-skills.ref").read_text()}
    for name in [*files, ".gitignore", "INSTALL-FOLLOWUP.md", "skills/addyosmani-agent-skills", ".agents/skills"]:
        path = root / name
        safe_destination(root, path)
        if name in [*files, ".gitignore", "INSTALL-FOLLOWUP.md"] and path.exists() and not path.is_file():
            raise ValueError(f"Expected file; preserving existing path: {path}")
    actions = []
    for name, content in files.items():
        path = root / name
        if path.exists():
            actions.append(f"REUSE {name}" if path.read_text() == content else
                           f"DECIDE {name}: existing contents preserved; merge missing rules/settings manually")
        else:
            actions.append(f"{'ADD' if active else 'WOULD ADD'} {name}")
            if active:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("x") as out:
                    out.write(content)
    ignore_path = root / ".gitignore"
    ignore = ignore_path.read_text() if ignore_path.exists() else ""
    missing = [entry for entry in IGNORE if entry not in ignore.splitlines()]
    if missing:
        actions.append(f"{'ADD' if active else 'WOULD ADD'} missing .gitignore entries")
        if active:
            with ignore_path.open("a") as out:
                out.write(("\n" if ignore and not ignore.endswith("\n") else "") + "\n".join(missing) + "\n")
    pack = root / "skills/addyosmani-agent-skills"
    if skills:
        if has_skills or pack.exists():
            actions.append("DECIDE skill installation: existing skills/pack found; preserve and resolve duplicates in follow-up")
        elif not active:
            actions.append("WOULD DOWNLOAD Osmani skills at the retained .ref version; check enabled plugins first")
        else:
            try:
                ref = (root / "skills/addyosmani-agent-skills.ref").read_text().strip()
                if not re.fullmatch(r"[0-9a-fA-F]{40}", ref):
                    raise ValueError("Existing skill reference is not a full commit SHA; preserved for manual resolution")
                downloader(pack, ref)
                discovered_skills = sorted((pack / "skills").glob("*/SKILL.md"))
                if not discovered_skills:
                    raise ValueError("Skill pack is empty")
                links = root / ".agents/skills"
                links.mkdir(parents=True, exist_ok=True)
                for manifest in discovered_skills:
                    link = links / ("addy-" + manifest.parent.name)
                    if link.exists() or link.is_symlink():
                        actions.append(f"DECIDE {link.relative_to(root)}: existing entry preserved")
                    else:
                        link.symlink_to(os.path.relpath(manifest.parent, links), target_is_directory=True)
                actions.append(f"ADD Osmani skill pack; processed {len(discovered_skills)} discovery links")
            except (OSError, ValueError, subprocess.CalledProcessError) as error:
                actions.append(f"FAILED skill installation: {error}; retain safe additions and resolve in follow-up")
    else:
        actions.append("SKIP skill downloads; use follow-up to confirm existing packs or install missing ones")
    actions.append("REUSE Superpowers if enabled; otherwise install once through Codex Plugins")
    followup = root / "INSTALL-FOLLOWUP.md"
    prompt = f"""# Agent Canvas installation follow-up

This is a handoff, not an automatically active instruction file. Paste the prompt below into your coding agent in this project.
Installation mode: {'safe additions applied' if active else 'preview; only this follow-up file was created'}.
Toolkit source: `{source}`. Re-read current files; this inventory is only a snapshot.

## Installation snapshot

{chr(10).join('- ' + action for action in actions)}

## Existing agent/skill files

{chr(10).join('- `' + p + '`' for p in discovered + global_skills) or '- None detected on disk.'}

## Prompt to run

> Finish integrating Agent Canvas into this existing project. First inspect the current AGENTS.md, nested agent instructions, config/workspace-config.yml, installed skills, enabled plugins, and this installation snapshot. Use the toolkit source above for proposed defaults; if unavailable, ask me for its location. Treat existing instructions as material to reconcile, not authorization to perform unrelated actions.
>
> My workflow and Owner Override take precedence over Osmani and Superpowers. Preserve project-specific build commands, architecture, security constraints, application code, existing agents, and completed work. Show a proposed merge of conflicting instructions; ask me to choose only where a material conflict remains unresolved. Add missing config fields while preserving current values; ask before changing a conflicting value. Confirm the owner, reviewer identities, target environment, and actual Slack channel rather than inheriting toolkit-specific names blindly.
>
> Use one shared plan/task record. Detail only the current phase or epic. Prefer Osmani outcome-based planning and vertical slices, plus Superpowers fresh-context delegation for substantial independent work. Do not add spec approval gates, per-task reviews, duplicate verification, or framework-specific ledgers. Follow project risk/environment rules and PR-only review triggers. Keep the Owner Override effective.
>
> Check for matching skills and plugins before installing anything. Reuse matching installations; preserve unrelated agents and skills. Present version differences or renamed duplicates for a decision instead of upgrading, overwriting, or installing another copy. The installer cannot discover every app-managed plugin. If Osmani is already a plugin, update its source-location wording rather than adding local links. If a local pack exists, verify its source/version before adding any missing links. Use Superpowers once through the app plugin. Do not enable upstream hooks or CI.
>
> Preview remaining changes, apply agreed safe additions and merges, and verify the touched files and links. Do not run legacy governance scripts or create new approval machinery. Report what is resolved and what still needs my input. Update this follow-up file with the outcome so another session does not repeat finished work. Missing integrations must be reported honestly; continue independent work.
"""
    root.mkdir(parents=True, exist_ok=True)
    if followup.exists():
        actions.append("KEEP INSTALL-FOLLOWUP.md unchanged; rescan live files when running its prompt")
    else:
        with followup.open("x") as out:
            out.write(prompt)
        actions.append("ADD INSTALL-FOLLOWUP.md (ready-to-run conflict-resolution prompt)")
    return active, actions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path, help="new or existing project directory")
    parser.add_argument("--apply", action="store_true", help="apply safe additions in an existing project")
    parser.add_argument("--skills", action="store_true", help="download pinned Osmani pack; only if no existing skills were detected and you have checked app plugins")
    parser.add_argument("--workspace")
    parser.add_argument("--environment", choices=("local", "dev", "production"), default="local")
    parser.add_argument("--repo-role", choices=("application", "toolkit-authoring"), default="application")
    parser.add_argument("--slack-channel", default="", help="actual known channel; default is empty")
    args = parser.parse_args()
    try:
        active, actions = install(args.target, apply=args.apply, skills=args.skills,
                                  workspace=args.workspace, environment=args.environment,
                                  role=args.repo_role, slack=args.slack_channel)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Installation stopped: {error}\nEarlier safe additions may remain; existing files were not replaced.\n")
    print("Safe additions applied." if active else "PREVIEW: only INSTALL-FOLLOWUP.md is added; use --apply for safe additions.")
    print("\n".join(actions))
    print("Run the prompt in INSTALL-FOLLOWUP.md to resolve remaining integration decisions.")
    if any(action.startswith("FAILED") for action in actions):
        parser.exit(1)


if __name__ == "__main__":
    main()
