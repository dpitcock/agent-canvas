# Supervised mode proof of concept

`--supervised` registers a project with a host-state directory outside the project. The host state—not `AGENTS.md`, task files, prompts, or installer state—contains the immutable authorization snapshot, action graph, validation receipts, leases, retry/reconciliation decisions, blockers, and JSONL audit log.

One host state directory can serve multiple projects. Every task operation accepts a project identity and resolves state beneath that project's host registration. Task IDs may therefore repeat across projects without sharing actions, evidence, visible messages, or audit events; an unqualified lookup is rejected when it would be ambiguous.

State writes sync the file and then its directory after replacement. If only the post-replacement directory sync fails during final release, the host verifies the exact saved terminal decision under the task lock and returns it with `durability_confirmed=False`. The renderer displays the saved final message with `durability_warning: state_directory_sync_failed`; it does not retry that already-visible transition. This preserves delivery in the running process without claiming crash durability. Failures before replacement remain retryable; loss of the client after a returned release still requires recovery from `visible_messages()`.

Accepted local-development limitation: creation of state directories does not sync every parent directory. An abrupt crash or power loss can therefore lose a newly created registration or task directory despite syncing its contained files. `durability_confirmed` describes the handled state-write sync result, not an end-to-end power-loss guarantee. Parent-directory creation syncing is deferred for this personal local-Mac installation.

Project audit reads and appends share a file lock across tasks. Appending scans backward in bounded blocks only as far as the last newline and truncates an unfinished tail; it does not reparse historical records. Audit reads stream complete records and retain only the requested task's events. Complete malformed records remain explicit read errors and are never discarded, but do not prevent later appends. Terminal release evidence remains in the task state, separate from this progress log.

If the same post-replacement sync failure occurs while claiming validation, the host verifies that its attempt owns the visible claim and proceeds with validation and normal exception recovery. It does not strand the task in `validating`. Uncommitted temporary state files are removed when writing, flushing, or file syncing fails.

The custom renderer consumes Codex App Server item events. It forwards progress but buffers `item/agentMessage/delta` content. Only after `turn/completed` does it ask the host supervisor to release content. Remaining host actions queue a continuation instead; a blocker is delivered only after no independently authorized action remains.

The POC uses atomic replacement for task state and fsynced append-only progress audit events. Terminal release authorization, its message, and its audit event are committed together in the task file; a failed state write cannot leave a successful release event behind. `HostSupervisor.audit(task_id)` combines that task's progress log with its committed release event. Reading `audit.jsonl` alone is not a complete audit. Existing historical JSONL events remain readable.

`final_release_committed` and `blocker_release_committed` mean the host authorized release, not that a UI acknowledged rendering. A client crash after commitment does not cause an automatic second release; clients can recover the saved message using `visible_messages`. Exactly-once display across client crashes is not guaranteed by this POC. It recovers a leased side-effect action as `reconcile`, never a second dispatch. Pause and cancellation are persisted and disable automatic continuation.

## Boundary and limitations

Each renderer retains at most 1 MiB of UTF-8 message text across pending and completed items, 4,096 nonempty delta chunks, and 1,024 distinct item IDs plus 1,024 completed turn IDs over its lifetime. IDs are limited to 512 characters; phases are normalized to fixed values. Completed item text replaces its delta buffer and counts against the same text budget. Empty deltas consume no retained metadata. Turn completion clears message text but preserves bounded replay history. Exceeding a retention limit (or receiving text that cannot encode as UTF-8) clears retained state and permanently returns `renderer_limit_exceeded`; later events cannot release text or reach the host gate. Start a new renderer for a new independently bound turn instead of evicting replay history.

These limits bound renderer-retained state, not the size of an event already allocated by the caller. A live transport must separately limit incoming event size before parsing.

The locally generated Codex App Server protocol schema exposes `turn/start`, `item/agentMessage/delta`, and `turn/completed`, supporting this renderer design. This POC exercises the renderer with an event fixture; it does not launch a real App Server turn or modify the desktop app.

The guarantee holds only when users interact through this custom client and the agent lacks filesystem access to its host-state root. File permissions alone do not protect a state directory from another process running as the same operating-system identity. The existing Codex desktop app is outside this interception boundary.

Host validators execute a copied executable from the host-state directory. Copying is limited to 64 MiB and checked against a five-second deadline; a missing executable, failed copy, or missing digest prevents launch. The receipt identifies the copied executable, not its interpreter, imported modules, or other runtime dependencies. Validators must support execution from that location: virtual-environment discovery, paths relative to the executable, and native-library lookup can depend on the original installation path.

Output is drained incrementally with an 8 KiB retained byte prefix. Timeout sends TERM and KILL to the validator process group, then closes output pipes after a final drain deadline and bounds the direct-child wait. Detached descendants may survive independently; this host execution path is not a sandbox. Use the Docker executor's documented local-development boundary when process isolation is required.

Upstream desktop support needs: a host-owned final-message interceptor, durable host task state, callbacks for completion and recovery/compaction, a way to suppress default final rendering, cancellation propagation, and child-turn evidence/join events.

## Test results

Executed: `python3 -m unittest discover -s tests -v` (30 tests) and `python3 -m py_compile scripts/install.py scripts/supervisor.py scripts/supervised_client.py`.

The supervised tests cover the ten required scenarios through the host API and an App Server-shaped renderer event stream. Unrun: a live Codex App Server model turn, desktop-app interception, and an operating-system-separated service-account deployment. Those require a deliberately deployed custom client and host boundary; they are not represented as passing checks.
