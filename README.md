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

**This toolkit repository has one exception:** Staff Engineer is the sole PR reviewer for both authoring and shipped changes. Do not add Principal, Code Reviewer, AppSec, or QA approvals to this repository. This exception lives in the toolkit-specific `AGENTS.md` lanes, which the installer replaces for `--repo-role application` (the default). Installed application projects retain the environment/risk review rules above and their own `approvals_required`; updates preserve their project settings.

For this toolkit, internal docs, tests, scripts, and tooling use the light **authoring lane**. Templates, adopter policy, distributed bootstrap, and CI configuration use the **shipped lane**: versioned changes reviewed at the PR. A bootstrap script shipped to others stays shipped even if it lives in `scripts/`.

## Quick install

Use Python 3.9 or newer on macOS/Linux. No Python packages or Node dependencies are needed. Git is needed only when downloading skills. Run these commands from your copy of `agent-canvas`; the target can be anywhere on your machine.

For a new project:

```sh
python3 scripts/install.py /path/to/my-new-project --workspace my-new-project
```

The installer creates the folder if needed, adds the rules and settings, adds ignore entries, and writes `INSTALL-FOLLOWUP.md`. It also saves a small installation record in `.agent-canvas/state.json`, so later upgrades can tell package defaults apart from your changes. It does not initialize Git, commit changes, or create a GitHub repository. Use your usual project generator or Git setup separately.

For an existing project, run the same command:

```sh
python3 scripts/install.py /path/to/existing-project
```

**Existing projects get a preview first.** The only file written during preview is `INSTALL-FOLLOWUP.md`, which contains the inventory, proposed actions, and a ready-to-run prompt. A folder with only `.git` is treated as a new project.

Read the preview, then apply safe additions when ready:

```sh
python3 scripts/install.py /path/to/existing-project --apply
```

The output labels work as **ADD**, **WOULD ADD**, **REUSE**, **SKIP**, or **DECIDE**. Normal installation with `--apply` adds missing files and ignore entries; it never replaces an existing AGENTS.md, config, version reference, or skill. Missing fields in an existing config are left for the follow-up merge, so project-specific settings remain intact. Differing files are recorded as pending conflicts. CODEOWNERS, other agents, hooks, CI, Git history, and application code are untouched. Replacing package defaults later requires the explicit `--upgrade` mode below.

### Finish the conflicts with your agent

Open the target project in Codex or Cline, open `INSTALL-FOLLOWUP.md`, and paste its **Prompt to run** into chat. The prompt asks your agent to:

1. Recheck current files and enabled plugins, rather than trusting an old snapshot.
2. Propose a merge that preserves project details and puts your workflow and Owner Override first.
3. Reuse matching skills and ask about version differences or unresolved conflicts.
4. Apply agreed changes and record what is done, so the next session does not repeat it.

The installer checks local agent instructions, skill files, rule files, and standard user skill directories. It cannot discover every app-managed plugin or understand conflicting prose. Existing skills therefore defer automatic skill downloads—even if their folder names differ. Use the follow-up to decide whether anything is actually missing. An existing follow-up file is preserved, including your edits and completed resolutions; the agent rescans live files when using it.

### Installer options

| Option | Meaning |
| --- | --- |
| `--apply` | Add missing files in an existing project after preview. |
| `--upgrade` | Preview package updates against the saved defaults and your current files. Combine with `--apply` to apply safe changes. |
| `--resolve PATH` | With `--upgrade --apply`, record a pending file's current contents as the chosen resolution. Repeat for multiple files. |
| `--reason TEXT` | Explain a recorded resolution so future sessions retain the decision. |
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

Skill downloads are opt-in. The installer downloads the whole pack at the selected `.ref` commit, preserves shared references, and creates discovery links. It does not run upstream installers or tests. If a pack already exists, it is preserved for source/version checks in the follow-up. Superpowers uses the supported installation for each assistant, as explained below; enabling an environment does not install a plugin. Failed installs report an error; any earlier safe additions remain for a later run.

### Upstream OpenCode link compatibility

At Agent Canvas commit `51fb8201055d9590d7a865bfef447bde973f0e79`, the fingerprint check rejected the tracked `.opencode/skills -> ../skills` symlink in pinned Osmani pack `2686b620fc1fed2e8f60c704839c766b8594c6b6`, reporting `ValueError: Skill pack contains symlinks; reconcile manually`. This affected `--skills` installation and subsequent upgrade validation of a pack containing that link.

The installer now accepts only that relative link, with literal target `../skills/` (the spelling stored upstream) or `../skills`, and only when its target is the pack's real, existing `skills` directory. It preserves the upstream link but never traverses or hashes it; the skill and reference files are hashed at their original paths. Other symlinks, including alternate targets, escaping links, broken links, cycles, and symlinked target directories, remain rejected. Git metadata directories remain excluded from content hashing; symlinks encountered at their boundaries are rejected too. This exception does not enable an OpenCode adapter.

Existing installations that omitted only this unused link and recorded the resulting fingerprint (the Capitol Deal workaround) need no migration or reinstall. Their fingerprints and resolution notes remain valid; leaving the link absent is supported. Upgrade preview and apply neither restore the link nor replace the pack, and changes to regular skill/reference content still require reconciliation.

If an older attempt failed before recording pack provenance, it may have left the downloaded pack on disk. Updating the toolkit and rerunning installation will preserve that pack, not silently trust it. Use `INSTALL-FOLLOWUP.md` to verify its source, pinned revision, and contents and explicitly reconcile the missing provenance. Preserve existing fingerprints and resolution history; do not replace them merely to suppress an error. Prefer the updated installer for new installations. The older archive workaround was to omit only `.opencode/skills`, keep every regular skill/reference file unchanged, and record the verified revision, resulting regular-file fingerprint, and exception in the project's installation record and follow-up notes.

## Upgrade without losing your decisions

Think of an upgrade as comparing three instruction cards: the old package defaults, your project's edited card, and the new package defaults. A change you already resolved belongs to your project; it should not become the same question every time you update.

First update your separate toolkit clone to the version you want. For example, from a clean `agent-canvas` clone on its main branch:

```sh
git pull --ff-only
python3 scripts/install.py /path/to/existing-project --upgrade
```

This previews the upgrade. Read the proposed changes and the refreshed upgrade section in the project's `INSTALL-FOLLOWUP.md`, then apply safe changes:

```sh
python3 scripts/install.py /path/to/existing-project --upgrade --apply
```

| What changed? | What the upgrade does |
| --- | --- |
| Only the package changed a part | Apply the package change. |
| Only your project changed a part | Keep your change. |
| Each changed different lines | Combine the changes. |
| Both changed the same part | Leave the file for you and your agent to reconcile. |
| You deleted a managed file | Preserve the deletion; flag a conflicting package change if needed. |
| Nothing new happened | Leave the files and recorded decisions alone. |

Four text files use this merge process: `AGENTS.md`, `config/workspace-config.yml`, `.owner-override.example`, and `skills/addyosmani-agent-skills.ref`. Environment adapters separately track the discovery links they create, adding specific Cline ignore entries when needed. Your active `.owner-override`, unrelated agents, and application files stay untouched.

### Resolve once, remember next time

Use the prompt in `INSTALL-FOLLOWUP.md` to work through new or pending conflicts with your coding agent. Existing prose and earlier decisions are retained; the installer refreshes only its marked upgrade section. After you edit a pending file into the form you want, record that decision:

```sh
python3 scripts/install.py /path/to/existing-project --upgrade --apply \
  --resolve AGENTS.md --reason 'Keep project convention'
```

Use project-relative paths. Repeat `--resolve` to record several pending files with the same reason. This records your resolution; it is not a review or approval gate. The next upgrade preserves that choice unless a new package change overlaps it. Unresolved files remain pending.

The installer can combine text, but it cannot prove that two instructions mean compatible things. Read merged instructions and use the follow-up prompt for conflicts in meaning.

### Keep the small installation record

Commit `.agent-canvas/state.json` alongside your workflow files. It contains the record format version, a package version derived from file contents, original installation options, saved package defaults, pending conflicts, and append-only resolution notes. It also tracks installer-owned discovery links and the downloaded pack's version and content fingerprint. These let upgrades preserve customizations while moving the saved defaults forward. Keep secrets out of managed configuration and resolution notes; this record is project data, not a credential store.

An older or manually installed project may have no record. In that case, the installer does not guess which edits were yours. Run `--upgrade --apply` to establish the current package proposals, then reconcile differing files and record their resolutions. Future upgrades can use that baseline.

Run upgrades from your updated toolkit clone; the installer does not update a copy of itself inside the target project. Skill-pack updates are separate, explicit follow-up decisions. An upgrade may update the `.ref` proposal, but never downloads or replaces a skill pack. `--skills` cannot be combined with `--upgrade`.

## Manual setup in a new repository

Prefer doing it yourself? These steps are equivalent to the basic file setup above. Have Git and your coding assistant available.

1. Create your project folder and initialize Git, or use your usual project generator. Open that folder in Codex or Cline.
2. From this toolkit, copy `AGENTS.md`, `.owner-override.example`, `config/workspace-config.yml`, and `skills/addyosmani-agent-skills.ref` into the same relative locations in your project. Create `config/` and `skills/` if needed.
3. Customize the settings and owner-specific wording using the next section.
4. Add the ignore entries below to your project's `.gitignore`.
5. Install the skills using the instructions below, then start a new chat in the project.

Copy only the fresh-install files below. The old gate scripts, tests, epic templates, policy validators, and workflow documents have been removed. Do not restore the archived `init-project.sh` or `install-skills.sh`; they install the previous workflow.

The fresh-install files are exactly `AGENTS.md`, `.owner-override.example`, `config/workspace-config.yml`, and `skills/addyosmani-agent-skills.ref`, plus the ignore entries below. CODEOWNERS is optional ownership configuration. Install the skill packs separately; do not copy old `.agents/` entries or `skills/upstream/`. This repository's downloaded Osmani checkout is a local dependency, not part of the files to distribute.

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
agentic_envs:
  codex: true
  cline: true
  claude_code: false # Reserved; adapter not yet implemented.
# Applies to production PRs; local authoring requires no reviewers.
approvals_required:
  code_reviewer: true
  appsec: true
  qa: true
target_environment: local
repo_role: toolkit-authoring
slack_channel_name: ws-agent-canvas
```

- **workspace:** your project name.
- **agentic_envs:** assistants to configure for this project. This package supports Codex and Cline; `claude_code: false` reserves a future adapter. The list does not identify the assistant currently speaking.
- **approvals_required:** roles required for production PRs. AppSec checks security; QA checks that the declared behavior and evidence hold up. This list does not require them for ordinary local work.
- **target_environment:** `local`, `dev`, or `production`, based on the intended use of the change—not merely where the agent runs.
- **repo_role:** `toolkit-authoring` describes this repository. For an application, use a descriptive value such as `application` and adapt the toolkit-specific lane wording in `AGENTS.md` to your product. This is agent-readable configuration, not a new validated schema.
- **slack_channel_name:** one project channel shared by all assistants, such as `ws-my-project`. Leave it empty if unknown. Each authorized post identifies its actual environment and role. Existing channel values are preserved; editing this setting does not create or rename a Slack channel.

The copied rulebook names Dennis as owner and `dpitcock-*` Apps as reviewers. Keep those for Dennis's projects, or replace them with your actual owner and review arrangement. Required roles must have real reviewers when you reach that PR; do not invent approvals. Local work does not wait for that setup.

## Install the skills

### Choose your coding assistants

Think of `agentic_envs` as a list of doors into the same workshop. Codex and Cline read the same `AGENTS.md`, use the same project settings, and keep the same task and resolution history. They do not need separate workflows.

| Config key | Shared rules | Local Osmani discovery | Superpowers |
| --- | --- | --- | --- |
| `codex` | `AGENTS.md` | `.agents/skills/addy-<skill-name>` | Existing Codex plugin; setup separately if missing. |
| `cline` | `AGENTS.md` | `.cline/skills/<skill-name>` | No automatic installation; report unsupported/missing integration in the follow-up. |
| `claude_code` | Adapter not implemented | Keep `false` for now | Not configured by this installer. |

[Cline supports AGENTS.md](https://docs.cline.bot/customization/cline-rules) and documents [project skill discovery](https://docs.cline.bot/customization/skills). Its skill folder names must match the manifest names, so its links do not use the Codex `addy-` prefix. Both sets of links point to one local Osmani pack, including its shared references. Cline also discovers `.claude/skills` and `.clinerules/skills`; existing installations need reconciliation to avoid exposing a second copy.

For a new project, the copied config enables Codex and Cline. For an existing project, edit that project's `config/workspace-config.yml` to enable the assistants you use, then run:

```sh
python3 scripts/install.py /path/to/project --upgrade
python3 scripts/install.py /path/to/project --upgrade --apply
```

The first command previews integration changes; the second applies safe changes. Existing installations without an `agentic_envs` section keep Codex-only behavior. Use the simple indented mapping shown above, with literal `true` or `false` values. Unknown names, invalid values, and unsupported enabled adapters are rejected before changes are made.

Upgrades do not download skills. They can expose a previously installer-downloaded, verified local pack to a newly enabled assistant. Manually installed or changed packs are preserved for reconciliation in the follow-up. If no pack exists, install missing skills separately with the opt-in `--skills` installation flow after checking for existing plugins.

Disabling an environment only permits removal of its unchanged installer-owned discovery links. User-created or modified entries remain for reconciliation. It does not uninstall the assistant, disable its plugins, delete the shared pack, or remove `AGENTS.md`. All generated integration files remain within the project; no home-directory settings are written.

These checks validate filesystem setup. After installation, start a fresh session in each enabled assistant and confirm that its skill list includes the expected skills. App versions and settings can affect discovery.

### Adding another assistant later

An adapter is the small part of the installer that tells an assistant where to find our shared instructions and skills. Adding a name to YAML alone does not implement support.

Claude Code, Gemini, and Antigravity are future integration examples, not supported adapters in this package. `claude_code: false` is currently accepted as a reserved setting; `claude_code: true` is rejected. Do not add `gemini` or `antigravity` to project config yet: unknown keys are rejected even when set to `false`.

To implement another adapter:

1. **Check the assistant's official documentation.** Record the exact product/version, project rule and skill locations, naming requirements, symlink support, and discovery precedence. Check Osmani and Superpowers compatibility separately, including plugin and subagent capabilities. Do not assume that using the same model means using the same integration.
2. **Add the installer mapping.** In [scripts/install.py](scripts/install.py), update `ADAPTERS` and `agentic_envs()`, then adapt `adapter_plan()` for any naming, duplicate-discovery, or capability differences. Adding a directory to `ADAPTERS` is sufficient only when the existing behavior actually fits the new assistant. Keep unsupported combinations explicit.
3. **Share the workflow.** Reuse `AGENTS.md`, project config, the root `.owner-override`, and one task record. If the assistant needs its own instruction filename, create a minimal bridge that directs it to the shared rules. Preserve existing instructions and record conflicts; do not copy the whole workflow into another rulebook. Overrides remain project-local, read as data, and never sourced.
4. **Share skills safely.** Reuse the verified local skill source or a compatible existing installation. Check every location the assistant searches so it does not load the same skill twice. Preserve unrelated skills and plugins. Keep unsupported integrations in `INSTALL-FOLLOWUP.md` while independent work continues.
5. **Support upgrades and removal.** Extend ownership tracking, path validation, and preview/apply handling for any new generated files or links. Only remove unchanged installer-owned entries when an environment is disabled. Preserve modified files, earlier conflict resolutions, and user notes. New text bridges need explicit baseline/merge support; they are not automatically managed by adding an adapter mapping.
6. **Verify the behavior and document it.** Extend [tests/test_environments.py](tests/test_environments.py) with focused cases for installation, enabling/disabling, duplicate avoidance, safe paths, and preservation of user edits. Reuse existing test helpers. Then check discovery in the actual assistant; report an unavailable live check honestly. Update the support table, config example, and follow-up guidance with verified capabilities and limitations.

Slack needs no new channel per adapter. Use the existing project channel and prefix authorized posts with the actual environment and role, for example `[claude_code][Developer]`, `[gemini][Developer]`, or `[antigravity][Developer]` once those environment identifiers are implemented. Missing Slack access remains a setup note, not a development gate.

This is ordinary implementation work within the current task. It does not introduce an additional specification, planning approval, or review chain.

### Osmani: one local copy, with shared reference files

This is the layout used in this repository. If Osmani's pack is already enabled as a plugin, use that installation instead and update the source-location sentence in `AGENTS.md`; do not also run these steps.

From your project root, with the copied `.ref` file present:

```sh
mkdir -p skills .agents/skills
git clone https://github.com/addyosmani/agent-skills.git skills/addyosmani-agent-skills
git -C skills/addyosmani-agent-skills checkout --detach "$(cat skills/addyosmani-agent-skills.ref)"
```

The `.ref` file records the selected version. Keeping the whole upstream copy preserves its shared references. If the destination already exists, inspect and reuse it instead of overwriting it.

For Codex, create the discovery links below. For Cline, use `.cline/skills/<skill-name>` without the `addy-` prefix, only after checking its other skill locations for duplicates. Prefer the installer for a new download: it creates the selected links and records ownership for future upgrades. Manual links and downloads are preserved for follow-up reconciliation.

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

### Superpowers: use the installation supported by your assistant

In the Codex app, open **Plugins**, find **Superpowers**, and install it. If it is already installed, keep that single installation. This workspace already has it available through the app.

Follow [Superpowers' installation instructions](https://github.com/obra/superpowers#installation) for other supported tools. Our rulebook remains authoritative over its workflow suggestions. Do not copy its full instructions into `AGENTS.md` or install another local copy just for this project.

Cline can use the shared rules and Osmani skills without a Codex plugin. This installer does not claim Superpowers support in Cline or copy a Codex plugin into it. Record missing support in `INSTALL-FOLLOWUP.md` and continue with the capabilities actually available. For other assistants, add a documented adapter before enabling them; skill compatibility alone does not establish plugin or subagent support.

## Use it in a normal chat

You do not need a special slash command. Try:

> Read AGENTS.md and config/workspace-config.yml. Add CSV export to this app. Keep the work bounded and verify the changed behavior.

For a larger project:

> Outline the project and its phases. Detail only the first phase. Use subagents for substantial independent tasks, keep one shared task record, and reuse completed work.

For existing work:

> Continue the current epic. Check what is already done before assigning tasks. Reuse the existing plan and valid test evidence.

The developer should select relevant skills, implement, and report what changed and what was checked. Simple edits stay inline. Larger independent jobs can go to helpers. No duplicate reviews or framework paperwork.

## When the process gets in the way

Say `override pause`, `override bypass-review`, or `override reset` in the current project's chat or Slack context. For a file-based override, use dotenv-style `KEY=value` syntax in `.owner-override` at that project's root only.

**Overrides never cross project boundaries.** Do not read them from home or parent directories, environment variables, or another project. Do not share or symlink override files. When switching projects, do not carry over an earlier chat/Slack override. There is no global override or fallback location.

| Mode | Meaning |
| --- | --- |
| `pause` | Stop gating and process work; do the task. |
| `bypass-review` | Skip reviewer requirements for the merge. |
| `reset` | Discard the in-flight plan/epic state and start fresh; keep implementation work. |

The installer copies the commented [.owner-override.example](.owner-override.example), but never creates or changes an active `.owner-override`. To activate the example:

```sh
cp .owner-override.example .owner-override
```

Its active setting is `OWNER_OVERRIDE=pause`. Edit that value to select another mode, or combine modes:

```dotenv
# Skip process work and reviewer requirements.
OWNER_OVERRIDE="pause,bypass-review"
```

Blank lines, surrounding whitespace, and `#` comments outside quotes are ignored. Single or double quotes are optional; comma-separated values enable multiple modes. Use one assignment; if repeated, the last assignment in the file wins. Explicit owner instructions for this project in chat/Slack take priority, followed by this project's root file.

Set `OWNER_OVERRIDE=` or remove the root `.owner-override` to disable file-based overrides for this project. A blank or comment-only file activates nothing by itself. There is no fallback to another location. Older standalone mode lines still work in this project's root file when there is no `OWNER_OVERRIDE` key.

This is a convention the agent reads under `AGENTS.md`, not an operating-system environment loader or an enforcement program. The file is never sourced as shell code; commands and variable references are not expanded. A bare “override” in chat pauses gating while you clarify the mode.

Overrides need no governance approval. Record the mode and timestamp in the commit trailer or PR description; skipped checks must never be reported as passed. Existing platform access controls still apply.

If a PR exceeds two review rounds, the same fix is attempted three times, process work outweighs the deliverable, or approvals block repairing approvals, the agent stops and offers one ranked choice: **bypass and finish**, **simplify the rule**, or **continue**.

## Slack and reviewer connections

Authorized Slack posts use **Agent Alert**, go to the shared project channel, and start with the actual environment and role: `[codex][Developer]` or `[cline][Staff Engineer]`. If the running environment is unknown, clarify it; do not guess from `agentic_envs`. Non-final turns end with a Proceed or ranked-Choose action item. A step taking over 15 minutes gets a heartbeat and status report; it is not silently abandoned or killed merely for taking time.

Use the available Slack plugin or connector, including the ChatGPT Slack connector when working there. If the configured channel is missing, ask the owner before creating it. If Slack access or channel details are missing, put the setup action in `INSTALL-FOLLOWUP.md` and continue working. Slack setup never blocks development.

The Slack connection and `dpitcock-*` reviewer Apps must be configured separately. These files do not deploy either integration. If a needed connection is unavailable, report it honestly in chat and continue independent work. Do not invent a channel, send as someone else, or build a control plane to finish setup.

## Clean install layout

```text
AGENTS.md                              # Your workflow and overrides
.owner-override.example                 # Commented dotenv-style example; inactive
README.md                              # Setup and usage
POSTMORTEM.txt                         # Historical explanation only
config/workspace-config.yml            # Project settings
.github/CODEOWNERS                     # Ownership example
.gitignore                            # Local installation exclusions
skills/addyosmani-agent-skills.ref      # Selected upstream version
scripts/install.py                     # Installer and opt-in upgrade mode
tests/test_install.py                   # One offline installer smoke test
tests/test_upgrade.py                   # Focused offline upgrade checks
tests/test_environments.py              # Codex/Cline adapter lifecycle checks
skills/addyosmani-agent-skills/         # Ignored local dependency
.agents/skills/addy-*/                  # Codex discovery links
.cline/skills/<skill-name>              # Cline links to the same local source
```

Installed projects also receive `INSTALL-FOLLOWUP.md` and, on an applied installation, `.agent-canvas/state.json`. You do not need to copy the installer or its tests into the target project.

In Codex, Superpowers is supplied by its installed plugin, with no second repository-local copy. The Osmani download retains upstream files and reference material, but this setup exposes only its skills; it does not register its hooks or CI. Your root rulebook controls how those skills are used. Local downloads and discovery links are recreated during installation, not distributed with the workflow files.

The removed legacy implementation is preserved in a separate permanent archive outside this repository. That archive is historical evidence, not an installation source or an active instruction directory.

Read [POSTMORTEM.txt](POSTMORTEM.txt) for why the workflow was simplified. The active starting points are this README, `AGENTS.md`, and `config/workspace-config.yml`.

## Check the installer

From this toolkit's root:

```sh
python3 -m unittest discover -s tests -v
```

These focused checks use disposable folders and local fixtures. They cover safe installation, existing-project preview, preservation, repeat runs, skill duplication avoidance, and symlink handling, plus upgrades that retain customizations and recorded resolutions while surfacing new conflicts. Adapter checks cover shared sources, enabling/disabling environments, modified links, legacy config, and existing Cline skills. They make no network requests and start no subprocess CLIs or nested test runners. They do not test governance approvals, launch assistant apps, or verify a live GitHub download.
