# Local container executor

`scripts/container_executor.py` is a small, standalone Python interface for
running a declared validator in Docker Desktop.  It is intentionally not wired
to the supervisor; PR #7 can consume a verified receipt later.

The supported target for this proof of concept is a personal local Mac running
Docker Desktop. Linux Desktop compatibility is outside this project's current
scope; the fixed-path fallbacks below do not promise support for every platform.

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

Declared input paths are limited to 64 components, including the filename.
Deeper paths are rejected before their directory chain is staged, keeping
recursive cleanup bounded; any earlier staged inputs are cleaned up on rejection.

The returned receipt binds the action and attempt identifiers, staged-input
digest, exact command, pinned image, canonical absolute runtime path, selected
local daemon endpoint (`runtime_endpoint`), observed runtime version, status,
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

The host-owned executor selects Docker only from these fixed installation
paths, in order: `/Applications/Docker.app/Contents/Resources/bin/docker`
(Docker Desktop on macOS), `/usr/bin/docker`, and `/usr/local/bin/docker`.
There is no request, project configuration, environment-variable, or `PATH`
override. Installations elsewhere are unsupported and fail closed when none
of these paths is usable. Symlink aliases are resolved before execution; the
canonical target must be outside the project, a singly linked regular
executable owned by root or the current host account, and not writable by
group or other users. That one absolute path is used for every lifecycle
command, including cleanup, and is recorded in `receipt.runtime`. Changing
`PATH` or an installation alias during a run cannot select another executable.

The daemon endpoint is selected once from `/var/run/docker.sock`, then
`<OS-account-home>/.docker/run/docker.sock` for Docker Desktop. The account
home comes from the operating system's account database, never `HOME`.
The resolved endpoint must be a Unix socket outside the project, owned by
root or the current account. Socket aliases are resolved before the first
command; every lifecycle command uses that same explicit `--host` endpoint.
Remote daemons, Docker contexts, and installations exposing only other socket
locations are unsupported and fail closed.

Each run also creates an empty private directory under the resolved `/tmp`
host directory, outside the project, and supplies it as `--config` to every
Docker command. It is removed on success and failure, including preflight or
staging failures. `TMPDIR`, `TEMP`, and `TMP` cannot select its parent. Docker
processes receive only a fixed `PATH`, `LANG`, and `LC_ALL`; inherited Docker
variables, home settings, proxies, credential helpers, and loader variables
do not configure them. Normal Docker configuration and registry credentials
are not loaded, so the exact image must already be available locally.

Socket ownership and path selection establish the supported local install,
not daemon authentication or attestation. Replacing the selected socket,
binary, or their ancestors through host-account or administrator control
remains outside this boundary, as does changing the daemon itself.

Installation directories and their administrators are trusted, including
admin-writable macOS application directories. This pins an executable path,
not its inode: replacing the binary or its ancestors through host-account or
administrator control remains outside the local-development boundary above.

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
