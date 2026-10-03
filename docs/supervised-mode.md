# Supervised mode proof of concept

`--supervised` registers a project with a host-state directory outside the project. The host state—not `AGENTS.md`, task files, prompts, or installer state—contains the immutable authorization snapshot, action graph, validation receipts, leases, retry/reconciliation decisions, blockers, and JSONL audit log.

One host state directory can serve multiple projects. Every task operation accepts a project identity and resolves state beneath that project's host registration. Task IDs may therefore repeat across projects without sharing actions, evidence, visible messages, or audit events; an unqualified lookup is rejected when it would be ambiguous.

The custom renderer consumes Codex App Server item events. It forwards progress but buffers `item/agentMessage/delta` content. Only after `turn/completed` does it ask the host supervisor to release content. Remaining host actions queue a continuation instead; a blocker is delivered only after no independently authorized action remains.

The POC uses atomic replacement for task state, fsynced append-only audit events, and an interprocess task lease lock. It recovers a leased side-effect action as `reconcile`, never a second dispatch. Pause and cancellation are persisted and disable automatic continuation.

## Boundary and limitations

The locally generated Codex App Server protocol schema exposes `turn/start`, `item/agentMessage/delta`, and `turn/completed`, supporting this renderer design. This POC exercises the renderer with an event fixture; it does not launch a real App Server turn or modify the desktop app.

The guarantee holds only when users interact through this custom client and the agent lacks filesystem access to its host-state root. File permissions alone do not protect a state directory from another process running as the same operating-system identity. The existing Codex desktop app is outside this interception boundary.

Upstream desktop support needs: a host-owned final-message interceptor, durable host task state, callbacks for completion and recovery/compaction, a way to suppress default final rendering, cancellation propagation, and child-turn evidence/join events.

## Test results

Executed: `python3 -m unittest discover -s tests -v` (35 tests) and `python3 -m py_compile scripts/install.py scripts/supervisor.py scripts/supervised_client.py`.

The supervised tests cover the ten required scenarios through the host API and an App Server-shaped renderer event stream. Unrun: a live Codex App Server model turn, desktop-app interception, and an operating-system-separated service-account deployment. Those require a deliberately deployed custom client and host boundary; they are not represented as passing checks.
