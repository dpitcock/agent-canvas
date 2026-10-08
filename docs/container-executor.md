# Local container executor

`scripts/container_executor.py` is a small, standalone Python interface for
running a declared validator in Docker Desktop.  It is intentionally not wired
to the supervisor; PR #7 can consume a verified receipt later.

## Contract

The host supplies an `ExecutionRequest`, a recorded `ProjectIdentity`, and a
host-owned staging parent.  The request requires a digest-pinned image and an
argument-vector command.  The executor opens the project root once with a
no-follow directory descriptor, verifies its device/inode, and copies only the
declared, bounded regular files through that descriptor.  It never mounts the
project pathname.  If the pathname is replaced after authorization, the copied
stage still comes from the originally opened directory; the replacement is not
used.

Inputs reject traversal, symlinks, hard-linked files, special files, mutation
detected during copying, excessive count, and excessive bytes.  The completed
snapshot is the sole bind mount and is read-only.  The container has no Docker
socket, no host state, no host home, no inherited environment, no project
mount, no network, no Linux capabilities, a non-root user, a read-only root,
bounded tmpfs, memory, CPUs, pids, files, runtime, and captured output.
Images declaring volumes are rejected before container creation: their
anonymous writable volumes would bypass the explicit tmpfs size limits even
with a read-only root filesystem. Unreadable or malformed image metadata is
also rejected.

The stage permits traversal and read access through declared paths while it
exists so the fixed container UID can consume it.  Declared input must not
contain data that an untrusted validator—or another host user able to discover
the temporary path—must not read.  It remains a host-owned, read-only container
mount; the executor deletes it after execution.

The returned receipt binds the action and attempt identifiers, staged-input
digest, exact command, pinned image, observed runtime version, status,
timeout/cancellation state, and bounded output digest.  `receipt.matches()`
must be checked against the host task's current values before using it.

## Local-development boundary

This is an OpenHands-style practical local-development sandbox, **not a
hostile-code security boundary**.  A project or agent that can control the
Docker daemon, the Docker Desktop configuration, or the host operating-system
account can bypass container restrictions.  Do not store credentials or
supervisor state in a location accessible to that actor, and do not use this
executor as proof of safe execution of hostile code.  A separately operated
Linux VM or remote sandbox remains necessary for that stronger claim.
The executor rechecks the staging pathname before starting the container, but
Docker resolves the bind mount during start; the check does not atomically pin
that mount. Concurrent host-side staging mutation by an actor controlling the
host account is outside this boundary.

Docker must be installed, its daemon running, and the exact pinned image must
already be present; otherwise the executor fails closed and never invokes a
host validator command.  It uses no image pull fallback.

## Verification

Unit tests: `python3 -m unittest discover -s tests -v`.

The Docker integration test is intentionally opt-in because it needs a local
digest-pinned image:

```sh
AGENT_CANVAS_CONTAINER_IMAGE='alpine@sha256:865b95f46d98cf867a156fe4a135ad3fe50d2056aa3f25ed31662dff6da4eb62' \
  python3 -m unittest tests.test_container_executor.DockerIntegrationTests -v
```

It checks non-root execution, read-only staged input, no Docker socket,
writable `/outputs`, network denial, and receipt verification.  It does not
prove protection from Docker-daemon or host-account control.
