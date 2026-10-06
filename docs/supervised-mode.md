# Supervised mode proof of concept

`--supervised` registers a project with a host-state directory outside the project. The host state—not `AGENTS.md`, task files, prompts, or installer state—contains the immutable authorization snapshot, action graph, validation receipts, leases, retry/reconciliation decisions, blockers, and JSONL audit log.

One host state directory can serve multiple projects. Every task operation accepts a project identity and resolves state beneath that project's host registration. Task IDs may therefore repeat across projects without sharing actions, evidence, visible messages, or audit events; an unqualified lookup is rejected when it would be ambiguous.

Registration keys use the stored absolute path without dynamically resolving aliases. The installer canonicalizes the initial target; callers must reuse that exact path. Root device/inode changes and old registrations without an identity fail closed pending owner migration. Provisioning and override import share a project lock. Override imports pin path components, verify the opened root identity, and reject shared hard links as well as symlinks.

The custom renderer consumes Codex App Server item events. It forwards progress but buffers both `item/agentMessage/delta` content and completed `agentMessage` items. Only a `turn/completed` event with status `completed` asks the host supervisor to release content; failed or interrupted turns remain recoverable. Remaining host actions queue a continuation instead; a blocker is delivered only after no independently authorized action remains.

The host must construct each renderer with the `thread_id` and `turn_id` from `turn/start`. Unbound or foreign notifications are ignored, including progress. Each renderer is single-use after any terminal event or gate exception; continuation starts a newly bound renderer.

The POC uses atomic replacement for task state, fsynced append-only audit events, and an interprocess task lease lock. It recovers a leased side-effect action as `reconcile`, never a second dispatch. Pause and cancellation are persisted and disable automatic continuation.

## Boundary and limitations

Validator execution is disabled in this PR. Once independent authorized work is finished, a task with required validators returns `validation_unavailable`, preserves the withheld candidate, records the owner action, and produces no success receipts. Retrying or restarting cannot bypass this condition. There is no local subprocess fallback, executable snapshot, or recursive workspace copy.

The owner-selected container runner is tracked in [issue #8](https://github.com/dpitcock/agent-canvas/issues/8) for a separate PR. It must isolate host state and credentials, stage only bounded declared inputs, enforce resource/network limits, and prove receipt binding and teardown through hostile-code integration tests before validation-backed completion is enabled.

Automatic decisions hold the task lock and interrupt lock in that order until their state and audit records are persisted. Interrupt publication uses the same interrupt lock: an interrupt published first wins; an interrupt arriving after a committed decision cannot retroactively retract it. No project code executes in this critical section.

The locally generated Codex App Server protocol schema exposes `turn/start`, `item/agentMessage/delta`, and `turn/completed`, supporting this renderer design. This POC exercises the renderer with an event fixture; it does not launch a real App Server turn or modify the desktop app.

The guarantee holds only when users interact through this custom client and the agent lacks filesystem access to its host-state root. File permissions alone do not protect a state directory from another process running as the same operating-system identity. The existing Codex desktop app is outside this interception boundary.

During an applied supervised install or upgrade, the host installer may import the target root's regular `.owner-override` as an immutable host snapshot. It never follows a symlink, searches elsewhere, or watches later workspace changes. `pause` pauses active tasks and starts future tasks paused; `reset` cancels nonterminal tasks while preserving their audit history; and `bypass-review` remains PR-governance-only. A later root-file edit takes effect only after another owner-run applied supervised upgrade imports a new snapshot.

Project-local prompts, task files, configuration, and workflow overrides are not completion authority by themselves. The host-only `release_withheld_final` operation is the emergency final-message escape hatch: it releases exactly one host-stored candidate and durably records the owner, reason, attempt ID, and content digest. It is unavailable from workspace configuration.

Upstream desktop support needs: a host-owned final-message interceptor, durable host task state, callbacks for completion and recovery/compaction, a way to suppress default final rendering, cancellation propagation, and child-turn evidence/join events.

## Test results

Executed: `python3 -m unittest discover -s tests -v` (67 tests) and `python3 -m py_compile scripts/install.py scripts/supervisor.py scripts/supervised_client.py`.

Tests cover host action gating, recovery, authorization, multi-project isolation, interrupt ordering, legacy/final message selection, and fail-closed validation. Tests of the removed local runner were replaced with no-execution and restart regressions; the smaller count does not represent successful sandbox validation. Unrun/deferred: isolated validator execution, validation-backed completion, a live Codex App Server model turn, desktop-app interception, and an operating-system-separated service-account deployment.
