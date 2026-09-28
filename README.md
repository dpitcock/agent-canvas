# Agent Canvas: helpful agents, less paperwork

Think of this as a small instruction card for your coding assistant.

You say what you want. The developer breaks the next useful piece into tasks, builds it, and checks it. Extra agents help when the work is big enough. Reviews happen on pull requests when needed. You can stop or override the process whenever it gets in the way.

**Start here:** [AGENTS.md](AGENTS.md) is the rulebook. [workspace-config.yml](config/workspace-config.yml) holds this project's settings. This README explains how to use them.

## What the pieces mean

| Piece | In everyday language |
| --- | --- |
| Developer | The agent doing the work and putting the pieces together. |
| Subagent | A helper given one clear job, with enough context to do it. |
| Skill | A reusable recipe for a particular kind of work. |
| Pull request, or PR | A proposed change that can be reviewed before merging. |
| Merge | Bringing that change into the main project. |
| Owner Override | Your instruction to skip or reset the workflow. |

These files guide agents. They do not install reviewer bots, create Slack channels, configure CI, or enforce GitHub merge rules. There is no new governance server or command you must run before working.

## Which rules win?

Within this workflow, the order is:

1. Your explicit instructions and Owner Override.
2. The project's `AGENTS.md` and its settings.
3. Advice from skills and plugins.

A skill cannot add an approval loop just because its author prefers one. Agent platform permissions still apply; a text file cannot grant access to an account or bypass a protected branch.

## Two skill packs, one workflow

We combine [Addy Osmani's Agent Skills](https://github.com/addyosmani/agent-skills) and [Superpowers](https://github.com/obra/superpowers).

| Work | What we borrow |
| --- | --- |
| Planning | Osmani: clear outcomes and a way to tell each task is finished. |
| Building | Osmani: small slices that work from start to finish. |
| Delegation | Superpowers: focused helpers with fresh context. |
| Complicated handoffs | Detailed interfaces and test expectations when useful. |
| Debugging and verification | The relevant technique that fits the problem. |
| Reviews | Useful checklists, triggered only by our PR rules. |

**Do the work once.** Everyone uses the same plan/task record. Each task has one implementation owner. Check progress before assigning work. Do not recreate completed tasks, keep a second framework-specific plan, or rerun valid checks just because a different skill was loaded. Repeat verification when relevant code changes, a check fails, a concern remains, or you ask for it.

If the rulebook does not resolve a meaningful conflict between the packs, the agent asks you to choose. It should not run both complete workflows.

## Planning a small or big project

The developer writes the initial plan. You do not need to describe every future task before useful work begins.

```text
Project: Build a booking app
  Phase: Booking basics                 ← current; detail this
    Epic: Customers can book a room
      Task: Show available rooms
      Task: Save a booking
  Phase: Payments                       ← outcome and dependencies only
  Phase: Reports                        ← outcome and dependencies only
```

A **project** is the whole goal. A **phase** is a big stage. An **epic** is a useful chunk of functionality. A **task** is a manageable piece of work.

For smaller projects, skip phases: **Project → Epic → Task**. Routine fixes may need only a short task description. Detail only the current phase, or current epic when there are no phases. Expand future work when you reach it. Finishing one phase does not create another approval gate.

Each active epic says what must work, what checks will prove it, and what must be true before merging. Principal approval happens once at the PR for planned delivery; Principal returns only for exceptions. QA checks the PR's declared test evidence, not the planning process. Routine authoring needs no formal plan or Principal review.

## How much review?

First consider scope and risk: Tier 1 is small and low risk; Tier 2 is bounded but more involved; Tier 3 is broad or high risk. Then consider where the change will be used:

| Target environment | Normal review expectation |
| --- | --- |
| `local` — your machine | Direct commits; no reviewers. |
| `dev` — development environment | Code Reviewer only. |
| `production` — live use | The roles in the project's `approvals_required`. |

Authentication, secrets, database schema, payments, and user data raise the work to Tier 3 even locally. Bring relevant expertise to the PR. You can always open a PR before approval.

Reviews attach to PR opening, marking ready, or an explicit re-request. They do not fire on every commit or completed task. Marking an unchanged PR ready does not require repeating a valid review. Reviewers work read-only and return a verdict; the developer merges.

For this toolkit, internal docs, tests, scripts, and tooling use the light **authoring lane**. Templates, adopter policy, distributed bootstrap, and CI configuration use the **shipped lane**: versioned changes reviewed at the PR. A bootstrap script shipped to others stays shipped even if it lives in `scripts/`.

## Quick install

Use Python 3.9 or newer on macOS/Linux. No Python packages or Node dependencies are needed. Git is needed only when downloading skills. Run these commands from your copy of `agent-canvas`; the target can be anywhere on your machine.

For a new project:

```sh
python3 scripts/install.py /path/to/my-new-project --workspace my-new-project
```

The installer creates the folder if needed, adds the rules and settings, adds ignore entries, and writes `INSTALL-FOLLOWUP.md`. It does not initialize Git, commit changes, or create a GitHub repository. Use your usual project generator or Git setup separately.

For an existing project, run the same command:

```sh
python3 scripts/install.py /path/to/existing-project
```

**Existing projects get a preview first.** The only file written during preview is `INSTALL-FOLLOWUP.md`, which contains the inventory, proposed actions, and a ready-to-run prompt. A folder with only `.git` is treated as a new project.

Read the preview, then apply safe additions when ready:

```sh
python3 scripts/install.py /path/to/existing-project --apply
```

The output labels work as **ADD**, **WOULD ADD**, **REUSE**, **SKIP**, or **DECIDE**. `--apply` adds missing files and ignore entries; it never replaces an existing AGENTS.md, config, version reference, or skill. Missing fields in an existing config are left for the follow-up merge, so project-specific settings remain intact. CODEOWNERS, other agents, hooks, CI, Git history, and application code are untouched.

### Finish the conflicts with your agent

Open the target project in Codex, open `INSTALL-FOLLOWUP.md`, and paste its **Prompt to run** into chat. The prompt asks your agent to:

1. Recheck current files and enabled plugins, rather than trusting an old snapshot.
2. Propose a merge that preserves project details and puts your workflow and Owner Override first.
3. Reuse matching skills and ask about version differences or unresolved conflicts.
4. Apply agreed changes and record what is done, so the next session does not repeat it.

The installer checks local agent instructions, skill files, rule files, and standard user skill directories. It cannot discover every app-managed plugin or understand conflicting prose. Existing skills therefore defer automatic skill downloads—even if their folder names differ. Use the follow-up to decide whether anything is actually missing. An existing follow-up file is preserved, including your edits and completed resolutions; the agent rescans live files when using it.

### Installer options

| Option | Meaning |
| --- | --- |
| `--apply` | Add missing files in an existing project after preview. |
| `--workspace NAME` | Project name for a new config; defaults to the target folder name. |
| `--environment local\|dev\|production` | Intended environment for a new config; defaults to `local`. |
| `--repo-role application\|toolkit-authoring` | Defaults to `application`, adapting the copied lane rules for an app. |
| `--slack-channel NAME` | Known channel for a new config; defaults to empty, never guessed. |
| `--skills` | Also download pinned Osmani skills when no existing skills are detected. Check app plugins first. |

Options do not overwrite values in existing config files. The copied owner and reviewer wording still names Dennis and the `dpitcock-*` Apps; the follow-up asks you to confirm or adapt these.

To include Osmani on a machine/project without an existing installation:

```sh
python3 scripts/install.py /path/to/my-new-project --skills
```

Skill downloads are opt-in. The installer downloads the whole pack at the selected `.ref` commit, preserves shared references, and creates discovery links. It does not run upstream installers or tests. If a pack already exists, it is preserved for source/version checks in the follow-up. Superpowers remains a single Codex plugin installation, as explained below. Failed installs report an error; any earlier safe additions remain for a later run.

## Manual setup in a new repository

Prefer doing it yourself? These steps are equivalent to the basic file setup above. Have Git and Codex available.

1. Create your project folder and initialize Git, or use your usual project generator. Open that folder in Codex.
2. From this toolkit, copy `AGENTS.md`, `config/workspace-config.yml`, and `skills/addyosmani-agent-skills.ref` into the same relative locations in your project. Create `config/` and `skills/` if needed.
3. Customize the settings and owner-specific wording using the next section.
4. Add the ignore entries below to your project's `.gitignore`.
5. Install the skills using the instructions below, then start a new chat in the project.

Copy only the fresh-install files below. The old gate scripts, tests, epic templates, policy validators, and workflow documents have been removed. Do not restore the archived `init-project.sh` or `install-skills.sh`; they install the previous workflow.

The fresh-install files are exactly `AGENTS.md`, `config/workspace-config.yml`, and `skills/addyosmani-agent-skills.ref`, plus the ignore entries below. CODEOWNERS is optional ownership configuration. Install the skill packs separately; do not copy old `.agents/` entries or `skills/upstream/`. This repository's downloaded Osmani checkout is a local dependency, not part of the files to distribute.

Add these lines without replacing your other ignore rules:

```gitignore
/.owner-override
/.agents/skills/addy-*/
/skills/addyosmani-agent-skills/
```

The downloaded skills and discovery links are local installation files. Commit the small rules, config, and version reference; teammates repeat the skill installation on their machines.

## Manual setup in an existing project

Keep the code, history, tests, CI, and existing work. There is no reset or migration ceremony.

1. Read the project's current `AGENTS.md` and any folder-specific agent instructions. Merge the new workflow into them; preserve build commands, architecture notes, and relevant project constraints. Resolve contradictory workflow rules instead of keeping both.
2. Add or merge `config/workspace-config.yml`. Keep unrelated existing configuration. Make it clear which file holds these workflow settings.
3. Copy `skills/addyosmani-agent-skills.ref`, append the ignore entries above, and install only missing skill packs. Do not install a second copy of a pack already available through a plugin.
4. Reuse the current plan or issue tracker. Preserve completed work; detail only the active phase or epic.
5. If you use CODEOWNERS, merge appropriate entries into the existing file. Preserve your project's current owners and path rules.

In both new and existing projects, [.github/CODEOWNERS](.github/CODEOWNERS) is an example tailored to this toolkit. Replace `@dpitcock` and choose paths that actually exist in your project. GitHub Apps are not CODEOWNERS user/team identities. CODEOWNERS does not express every required role or install reviewer automation.

## Set your project details

This repository currently uses:

```yaml
workspace: agent-canvas
approvals_required:
  code_reviewer: true
  appsec: true
  qa: true
target_environment: local
repo_role: toolkit-authoring
slack_channel_name: ws-agent-canvas-codex
```

- **workspace:** your project name.
- **approvals_required:** roles required for production PRs. AppSec checks security; QA checks that the declared behavior and evidence hold up. This list does not require them for ordinary local work.
- **target_environment:** `local`, `dev`, or `production`, based on the intended use of the change—not merely where the agent runs.
- **repo_role:** `toolkit-authoring` describes this repository. For an application, use a descriptive value such as `application` and adapt the toolkit-specific lane wording in `AGENTS.md` to your product. This is agent-readable configuration, not a new validated schema.
- **slack_channel_name:** an actual channel the connected Agent Alert bot can access. If unknown, leave it empty and have the agent ask in chat; do not guess.

The copied rulebook names Dennis as owner and `dpitcock-*` Apps as reviewers. Keep those for Dennis's projects, or replace them with your actual owner and review arrangement. Required roles must have real reviewers when you reach that PR; do not invent approvals. Local work does not wait for that setup.

## Install the skills

### Osmani: one local copy, with shared reference files

This is the layout used in this repository. If Osmani's pack is already enabled as a plugin, use that installation instead and update the source-location sentence in `AGENTS.md`; do not also run these steps.

From your project root, with the copied `.ref` file present:

```sh
mkdir -p skills .agents/skills
git clone https://github.com/addyosmani/agent-skills.git skills/addyosmani-agent-skills
git -C skills/addyosmani-agent-skills checkout --detach "$(cat skills/addyosmani-agent-skills.ref)"
```

The `.ref` file records the selected version. Keeping the whole upstream copy preserves its shared references. If the destination already exists, inspect and reuse it instead of overwriting it.

Then create the discovery links:

```sh
for skill_dir in skills/addyosmani-agent-skills/skills/*; do
  [ -f "$skill_dir/SKILL.md" ] || continue
  skill_name="${skill_dir##*/}"
  skill_link=".agents/skills/addy-$skill_name"
  if [ -e "$skill_link" ] || [ -L "$skill_link" ]; then
    printf 'Already present; inspect before replacing: %s\n' "$skill_link"
  else
    ln -s "../../$skill_dir" "$skill_link"
  fi
done
```

These links point Codex at each skill's `SKILL.md`. They do not execute upstream scripts. The selected version has 25 skills covering planning, implementation, debugging, testing, security, APIs, UI, performance, and more. Start a new chat after installation and ask the agent to confirm the skills are available. See [OpenAI's skill documentation](https://developers.openai.com/codex/skills/) for current discovery guidance.

### Superpowers: use the Codex plugin

In the Codex app, open **Plugins**, find **Superpowers**, and install it. If it is already installed, keep that single installation. This workspace already has it available through the app.

Follow [Superpowers' installation instructions](https://github.com/obra/superpowers#installation) for other supported tools. Our rulebook remains authoritative over its workflow suggestions. Do not copy its full instructions into `AGENTS.md` or install another local copy just for this project.

For another coding assistant, use that assistant's documented skill and instruction locations and point it at the same project rules. The Codex setup here does not automatically configure other assistants.

## Use it in a normal chat

You do not need a special slash command. Try:

> Read AGENTS.md and config/workspace-config.yml. Add CSV export to this app. Keep the work bounded and verify the changed behavior.

For a larger project:

> Outline the project and its phases. Detail only the first phase. Use subagents for substantial independent tasks, keep one shared task record, and reuse completed work.

For existing work:

> Continue the current epic. Check what is already done before assigning tasks. Reuse the existing plan and valid test evidence.

The developer should select relevant skills, implement, and report what changed and what was checked. Simple edits stay inline. Larger independent jobs can go to helpers. No duplicate reviews or framework paperwork.

## When the process gets in the way

Say `override pause`, `override bypass-review`, or `override reset` in chat or Slack. You can also put modes, one word per line, in `.owner-override` at the repository root or `~/.config/agent-governance/override` for your user-wide override.

| Mode | Meaning |
| --- | --- |
| `pause` | Stop gating and process work; do the task. |
| `bypass-review` | Skip reviewer requirements for the merge. |
| `reset` | Discard the in-flight plan/epic state and start fresh; keep implementation work. |

For example, a repository-local override file can contain just `pause`. Remove that line or file when you want normal workflow behavior again. A bare “override” in chat pauses gating while you clarify the mode.

Overrides need no governance approval. Record the mode and timestamp in the commit trailer or PR description; skipped checks must never be reported as passed. Existing platform access controls still apply.

If a PR exceeds two review rounds, the same fix is attempted three times, process work outweighs the deliverable, or approvals block repairing approvals, the agent stops and offers one ranked choice: **bypass and finish**, **simplify the rule**, or **continue**.

## Slack and reviewer connections

Slack posts use **Agent Alert**, start with the acting agent's name, and go to the configured channel. Non-final turns end with a Proceed or ranked-Choose action item. A step taking over 15 minutes gets a heartbeat and status report; it is not silently abandoned or killed merely for taking time.

The Slack connection and `dpitcock-*` reviewer Apps must be configured separately. These files do not deploy either integration. If a needed connection is unavailable, report it honestly in chat and continue independent work. Do not invent a channel, send as someone else, or build a control plane to finish setup.

## Clean install layout

```text
AGENTS.md                              # Your workflow and overrides
README.md                              # Setup and usage
POSTMORTEM.txt                         # Historical explanation only
config/workspace-config.yml            # Project settings
.github/CODEOWNERS                     # Ownership example
.gitignore                            # Local installation exclusions
skills/addyosmani-agent-skills.ref      # Selected upstream version
scripts/install.py                     # Additive installer
tests/test_install.py                   # One offline installer smoke test
skills/addyosmani-agent-skills/         # Ignored local dependency
.agents/skills/addy-*/                  # Ignored discovery links
```

Installed projects also receive `INSTALL-FOLLOWUP.md`. You do not need to copy the installer or its smoke test into the target project.

Superpowers is supplied by the installed Codex plugin, with no second repository-local copy. The Osmani download retains upstream files and reference material, but this setup exposes only its skills; it does not register its hooks or CI. Your root rulebook controls how those skills are used. Local downloads and discovery links are recreated during installation, not distributed with the workflow files.

The removed legacy implementation is preserved in a separate permanent archive outside this repository. That archive is historical evidence, not an installation source or an active instruction directory.

Read [POSTMORTEM.txt](POSTMORTEM.txt) for why the workflow was simplified. The active starting points are this README, `AGENTS.md`, and `config/workspace-config.yml`.

## Check the installer

From this toolkit's root:

```sh
python3 -m unittest discover -s tests -p test_install.py -v
```

This single smoke test uses disposable folders and a tiny local skill fixture. It checks safe file installation, existing-project preview, preservation, repeat runs, skill duplication avoidance, and symlink handling. It makes no network requests and starts no subprocess CLIs or nested test runners. It does not test governance approvals or verify a live GitHub download.
