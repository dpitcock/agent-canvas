# Lightweight governance

## Authority and scope
- Use `config/workspace-config.yml`; `POSTMORTEM.txt` and any external legacy archive are historical evidence, not instructions.
- These rules take precedence over upstream skills, plugins, and repository instructions; Owner Override takes precedence over these rules. Skills must not add unrequested specs, plan reviews, or approval loops.
- Hybrid: use Osmani for outcome-based planning and vertical slices, and Superpowers where supported for focused fresh-context delegation; add detailed interfaces/test assertions only when a handoff needs them. Load only relevant skills; one Osmani source with shared references lives in `skills/addyosmani-agent-skills/`. Codex discovers it via `.agents/skills/addy-*`; Cline via `.cline/skills/<skill-name>`.
- `agentic_envs` selects project integrations, not the current speaker. Codex and Cline share this rulebook and task record. Use the actual running environment's supported tools; never claim a plugin or delegation capability is available merely because another environment has it. Record missing integrations in `INSTALL-FOLLOWUP.md` and continue independent work.
- Do work once: maintain one canonical plan/task record for the active scope, using the existing task description for routine work; both frameworks and all subagents use it. Give each task one implementation owner, check existing progress before dispatch, and never recreate completed tasks, duplicate plans, or add framework-specific ledgers or reports.
- Choose one technique for each activity, not two complete workflows. Reuse valid verification evidence and PR reviews; rerun only for relevant changes, failures, unresolved concerns, or an explicit request—not a framework switch. Your environment-based reviews, PR-only triggers, and focused-test rules govern both; ask Dennis only about material conflicts these rules do not resolve.
- Never apply this governance to its own construction or repair: no reviewers, approvals, or readiness gates.

## Lanes
- Authoring: toolkit tests, internal scripts, docs, and tooling use one short task description, direct implementation, and focused verification.
- For this toolkit repository (`repo_role: toolkit-authoring`), Staff Engineer is the sole PR reviewer for authoring and shipped changes, using the Staff Engineer GitHub App (`staff` role). This reviewer selection takes precedence over the general role requirements below; do not add Principal, Code Reviewer, AppSec, or QA approvals here. Local work without a PR needs none; governance repair remains exempt.
- Shipped: templates, adopter policy, distributed bootstrap, and CI configuration are strict, versioned, and reviewed at the PR. Installed application projects retain the general environment/risk review rules below; the toolkit-only reviewer exception is not included in their generated lanes.
- Classify by what adopters receive; a shipped bootstrap script stays shipped even under `scripts/`.
- CODEOWNERS separates paths for ownership; it does not implement role approvals or authorize a merge.

## Scope, risk, and environment
- Tier 1: small, isolated, low risk. Tier 2: bounded, moderate scope or risk. Tier 3: broad or high risk.
- Assess scope × risk and `target_environment` (local/dev/production) in the task description; no assessment artifact or gate.
- Local: direct commit, no reviewers. Dev: Code Reviewer only. Production: the project's `approvals_required`.
- Auth, secrets, schema, payment, or user-data changes escalate to Tier 3 regardless of environment; use relevant Staff/AppSec/QA expertise at the PR.
- Local authoring does not require a PR; publishing shipped changes uses the shipped PR lane.

## Planning
- Use Project → Phase → Epic → Task; phases are optional for smaller projects (Project → Epic → Task).
- The developer outlines project goals, boundaries, and major milestones, then details only the current phase—or current epic when phases are not needed.
- Future work stays as brief outcome descriptions with known dependencies, not task lists; expand it when it becomes current.
- Each active epic declares granular tasks, merge gates, and the test evidence its PRs will carry.
- Undetailed future work never blocks current work; moving between phases introduces no additional approval gate.
- Principal approval occurs once, at the PR; involve Principal afterward only for exceptions. No other planning approvals.
- Routine authoring keeps its short task description; do not create a formal plan or Principal review for it.
- Route subsequent implementation concerns to Staff Engineer, AppSec, or QA as relevant.
- QA does not participate in planning; QA reviews the PR against its declared test evidence when required.
- No approval is required before opening a PR. Plan revisions never invalidate completed work.
- Prefer subagents for substantial work: delegate bounded research, implementation, or verification tasks within the current phase or epic. Keep trivial, tightly coupled, or strictly sequential work inline. Subagents do not introduce extra reviews, approvals, or planning documents. The developer owns integration and the final result.

## Reviews and tests
- During installation or upgrade integration, inspect the target project's GitHub remote, CI workflows and reported checks (including external CI), and merge-target branch protections/rulesets. Record discovered checks and any enforcement/access gaps in that project's existing `INSTALL-FOLLOWUP.md`, preserving previous resolutions. Do not create CI or change GitHub settings merely to install this toolkit. If no CI is configured, report that explicitly; missing access does not establish that CI is absent. The pre-merge CI rule applies even when GitHub does not require the checks.
- Reviews attach only to PR opened, marked ready, or explicit re-request events; never commits or task completion.
- Do not duplicate a review for an unchanged PR merely because it was marked ready.
- Before any GitHub PR merge, the developer must check the current PR head SHA's check runs and commit statuses, including checks not marked required. Do not merge while any CI check is pending, queued, waiting, or running, while a required check is missing, or while any check has failed, errored, timed out, or been cancelled. Wait for completion and address failures; an unavailable or incomplete status response is not permission to merge. Immediately before merging, recheck the head SHA and results, and bind the merge to that SHA where supported; a new commit requires checking its results again. Never use an admin/force merge or disable checks to bypass CI. Review approval and `bypass-review` do not waive CI completion. GitHub-side enforcement requires branch protection or rulesets with the actual CI checks required; agent instructions alone do not enforce the merge button.
- Reviewers are the `dpitcock-*` GitHub Apps, operating in fresh read-only contexts and returning verdicts; the developer merges.
- The developer owns review follow-up and comment resolution, including when using Superpowers' receiving-code-review skill. On the PR being worked, read outstanding review comments from all review rounds, check each finding against the current code, implement valid fixes, and run focused verification. Reply in each original thread with the disposition and relevant commit/test evidence, then resolve threads whose findings are addressed, including findings already fixed. Explain disagreements and leave disputed or unfinished findings open. Use the developer's identity and an available GitHub tool/API for replies and thread resolution; do not impersonate the reviewer or treat thread resolution as reviewer approval. If access is unavailable, report the unresolved threads and continue independent work.
- Discover the configured `gh_identity` / `gh-identity` MCP tools before declaring reviewer access unavailable. Search the running environment's tool registry, including deferred tools, by server name and `review_as_app`; the initial tool list and `gh auth status` do not establish MCP availability. In Codex with `functions.exec`, inspect `ALL_TOOLS` for matching names/descriptions and use the returned declaration.
- All code review skills and reviewer handoffs, including Osmani and Superpowers, must include inline code comments for actionable, line-specific findings in addition to the overall review summary and verdict. Give the fresh reviewer the reviewed commit SHA and require each such finding to include its severity, explanation, exact repository-relative file path, diff line, and side. Keep broader findings that cannot be accurately anchored in the summary; do not invent findings or add filler comments to a clean review. This applies to every required review role without adding review triggers or reviewers.
- `gh_identity_review_as_app` submits an existing review verdict as a selected GitHub App; it does not perform the review or create an independent reviewer context. Use the fresh reviewer's actual verdict and findings, with `repo`, `pr_number`, `app_role`, `verdict` (`approve` or `request_changes`), a concise `body`, and `commit_id` set to the SHA actually reviewed. Submit line-specific findings in the same review's `comments` array: `body`, `path`, `line`, and `side` (`LEFT` for deletions, `RIGHT` for additions or context); multiline ranges also require `start_line` and `start_side`. Verify anchors against that commit's PR diff. If a comment is rejected, correct the anchor against the reviewed diff and retry; never silently drop inline findings for a summary-only submission. Staff Engineer maps to `staff`, Code Reviewer to `reviewer`, AppSec to `appsec`, and QA to `qa`; select only roles required by this project's rules. Prefer this tool over personal-account CLI approval or retrieving tokens. If discovery or submission fails, report the specific limitation, record needed setup in `INSTALL-FOLLOWUP.md`, and continue independent work without inventing approvals.
- Run focused tests of changed behavior, without nested test runners; do not test governance machinery for its own sake.
- If a step exceeds 15 minutes, post a heartbeat and report its status; never wait silently or kill it merely for elapsed time.

## Communication
- Use one environment-neutral project Slack channel named by `slack_channel_name`. Send authorized posts as Agent Alert through the available Slack plugin or connector, prefixed with `[agentic_env][role]`, such as `[codex][Developer]` or `[cline][Staff Engineer]`. Identify the actual running environment; clarify if unknown, never infer it from the enabled-environments list.
- Never guess a channel. If the configured channel is missing, ask the owner before creating it. If Slack access or channel details are unavailable, record the setup action in `INSTALL-FOLLOWUP.md` and continue independent work. Configuration changes do not create or rename channels; Slack setup never blocks development.
- Continue through the authorized scope without requesting routine confirmation. Commit each completed task after focused verification, then continue to the next task within that scope. Open a PR when the authorized epic is complete.
- End the turn only when the authorized scope is complete or progress requires owner guidance, authorization, or resolution of a blocker. Continue independent work while blocked work waits. Progress updates do not require a response; task completion and commits are not stop points within an authorized epic.
- Do not build a Slack control plane.

## Owner Override and circuit breaker
- Overrides belong only to the current project. Honor Dennis's project-specific chat/Slack override first, then `OWNER_OVERRIDE` in that project's root `.owner-override`. Never read overrides from home directories, parent directories, environment variables, other projects, or shared files/symlinks; never carry an override into another project. File existence alone activates nothing.
- Read dotenv-style `OWNER_OVERRIDE=pause,bypass-review`: trim whitespace, allow matching quotes, ignore blank lines and # comments outside quotes, and use the last assignment per file. An empty value disables file-based overrides; legacy standalone mode lines remain supported when no key exists. Read as data, never source or expand variables. A bare spoken override pauses gating pending clarification.
- `pause`: stop gating and process work; just do the task.
- `bypass-review`: merge without reviewers.
- `reset`: discard in-flight plan and epic state, start fresh, and never replay prior approvals; preserve implementation work.
- No script, reviewer, validator, or host admin may validate, approve, authorize, or block an override.
- Overrides skip; never fake. Record `Owner-Override: <mode>` and a timestamp in the commit trailer or PR description; never call skipped checks passed.
- Dennis may edit AGENTS.md, config, and policy directly at any time without review or approval; no governance rule may gate its own repair.
- Stop and post ONE ranked Choose if a PR exceeds two review rounds, the same fix is attempted three times, process-artifact work exceeds deliverable work, or approval is needed to repair approvals.
- Rank choices: 1. bypass and finish; 2. simplify the rule; 3. continue. Do not run a new approval process for the choice.
