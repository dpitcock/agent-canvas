# Supervised mode proof of concept

`--supervised` registers a project with a host-state directory outside the project. The host state—not `AGENTS.md`, task files, prompts, or installer state—contains the immutable authorization snapshot, action graph, validation receipts, leases, retry/reconciliation decisions, blockers, and JSONL audit log.

One host state directory can serve multiple projects. Every task operation accepts a project identity and resolves state beneath that project's host registration. Task IDs may therefore repeat across projects without sharing actions, evidence, visible messages, or audit events; an unqualified lookup is rejected when it would be ambiguous.

Choose a dedicated state root owned by the current user with mode `0700`. A missing root is created privately; an existing root with different ownership, permissions, or unrelated top-level contents is rejected without changing its permissions. Existing supervisor roots may contain `projects` and `validator-snapshots`.

State writes sync the file and then its directory after replacement. If only the post-replacement directory sync fails during final release, the host verifies the exact saved terminal decision under the task lock and returns it with `durability_confirmed=False`. The renderer displays the saved final message with `durability_warning: state_directory_sync_failed`; it does not retry that already-visible transition. This preserves delivery in the running process without claiming crash durability. Failures before replacement remain retryable; loss of the client after a returned release still requires recovery from `visible_messages()`.

Accepted local-development limitation: creation of state directories does not sync every parent directory. An abrupt crash or power loss can therefore lose a newly created registration or task directory despite syncing its contained files. `durability_confirmed` describes the handled state-write sync result, not an end-to-end power-loss guarantee. Parent-directory creation syncing is deferred for this personal local-Mac installation.

Project audit reads and appends share a file lock across tasks. Appending scans backward in bounded blocks only as far as the last newline and truncates an unfinished tail; it does not reparse historical records. Audit reads stream complete records and retain only the requested task's events. Complete malformed records remain explicit read errors and are never discarded, but do not prevent later appends. Terminal release evidence remains in the task state, separate from this progress log.

Uncommitted temporary state files are removed when writing, flushing, or file syncing fails. Legacy persisted `validating` tasks remain recoverable; the current gate does not start host validation.

The custom renderer consumes Codex App Server item events. It forwards matching progress but buffers agent-message content. The host must bind it to thread and turn IDs from `turn/start`; unrelated and unidentified events are ignored. Only successful `turn/completed` asks the supervisor to release content. Each bound renderer is single-use after a terminal event or gate exception. Recovery uses a new renderer; an explicit legacy test mode retains old fixture retry behavior. Remaining actions queue continuation; a blocker is delivered only after no independent authorized action remains.

Registrations bind an absolute project path and root device/inode. Reuse that exact path: lookups do not resolve a changed symlink into another registration. Provisioning and owner-override imports share a project lock. Applied supervised installation imports only the target root's regular, unshared override file through pinned directories, bounded to 64 KiB. Workspace edits alone cannot change the stored host snapshot.

Override import records an incomplete marker before audit and task transitions. If import fails or the process stops, automatic dispatch, recovery and final gating reject decisions until an owner-run import successfully completes; restarting alone does not bypass the marker. Retry the applied supervised import to finish reconciliation. This does not extend the documented power-loss durability guarantee.

If the root override file was removed before that retry, the applied installer imports an empty snapshot only if the import is still incomplete under the project lock. Already-paused or cancelled tasks are not automatically resumed. Missing files do not clear previously completed snapshots; use an explicit empty `OWNER_OVERRIDE=` file to import that change normally. Explicit resume also takes the project lock so it cannot race a project-wide pause import.

Imported `pause` affects active and future tasks; `reset` cancels nonterminal work without deleting evidence. `bypass-review` is PR-governance-only. Automatic decisions acquire project, task, then interrupt locks, so a project override import completes across all tasks before dispatch, recovery, or final gating resumes. An interrupt published first takes priority. The host-only `release_withheld_final` operation requires an owner and reason and commits its release evidence with task state.

The POC uses atomic replacement for task state and fsynced append-only progress audit events. New tasks commit their creation audit event in the same task file, so creation has no separate audit-append failure window and identical retries retain one event. Terminal release authorization, its message, and its audit event are also committed together in the task file; a failed state write cannot leave a successful release event behind. `HostSupervisor.audit(task_id)` combines that task's progress log with its committed creation and release events. Reading `audit.jsonl` alone is not a complete audit. Existing historical JSONL events remain readable; missing creation evidence from older versions is not fabricated retroactively.

`final_release_committed` and `blocker_release_committed` mean the host authorized release, not that a UI acknowledged rendering. A client crash after commitment does not cause an automatic second release; clients can recover the saved message using `visible_messages`. Exactly-once display across client crashes is not guaranteed by this POC. It recovers a leased side-effect action as `reconcile`, never a second dispatch. Pause and cancellation are persisted and disable automatic continuation.

Accepted local-development limitation: action completion is saved before its separate audit append. If that append fails, the completed action and its evidence remain in task state, but an identical retry does not restore the missing `action_completed` audit event. Audit history is therefore not guaranteed to contain every action transition; consult task state when reconciling such failures.

## Boundary and limitations

Each renderer retains at most 1 MiB of UTF-8 message text across pending and completed items, 4,096 nonempty delta chunks, and 1,024 distinct item IDs plus 1,024 completed turn IDs over its lifetime. IDs are limited to 512 characters; phases are normalized to fixed values. Completed item text replaces its delta buffer and counts against the same text budget. Empty deltas consume no retained metadata. Turn completion clears message text but preserves bounded replay history. Exceeding a retention limit (or receiving text that cannot encode as UTF-8) clears retained state and permanently returns `renderer_limit_exceeded`; later events cannot release text or reach the host gate. Start a new renderer for a new independently bound turn instead of evicting replay history.

These limits bound renderer-retained state, not the size of an event already allocated by the caller. A live transport must separately limit incoming event size before parsing.

The locally generated Codex App Server protocol schema exposes `turn/start`, `item/agentMessage/delta`, and `turn/completed`, supporting this renderer design. This POC exercises the renderer with an event fixture; it does not launch a real App Server turn or modify the desktop app.

The guarantee holds only when users interact through this custom client and the agent lacks filesystem access to its host-state root. File permissions alone do not protect a state directory from another process running as the same operating-system identity. The existing Codex desktop app is outside this interception boundary.

The supported deployment is a personal local Mac under the Docker Desktop development trust model, not protection against malicious host-account or Docker-control access. A dispatch decision authorizes work but does not execute it or carry a project descriptor into a caller's later filesystem operations. Callers must bind their execution to the intended project; an adversarial root replacement between dispatch and execution is outside this POC's guarantee. The standalone executor's pinned inputs do not automatically extend to external action executors.

Required validators fail closed with `validation_unavailable`; the gate retains the candidate and produces no success receipt. It does not invoke the legacy private host-validator helpers. The standalone Docker executor is available separately but is not wired into supervisor completion. Enabling validation-backed completion requires explicit integration work, not a local subprocess fallback.

Upstream desktop support needs: a host-owned final-message interceptor, durable host task state, callbacks for completion and recovery/compaction, a way to suppress default final rendering, cancellation propagation, and child-turn evidence/join events.

## Test results

The PR verification section records the current test command, count and results. Coverage includes host state/audit recovery, registration identity, owner imports, interruption, bounded renderer state, and fail-closed validation.

The supervised tests cover the ten required scenarios through the host API and an App Server-shaped renderer event stream. Unrun: a live Codex App Server model turn, desktop-app interception, and an operating-system-separated service-account deployment. Those require a deliberately deployed custom client and host boundary; they are not represented as passing checks.
