#!/usr/bin/env python3
"""Host-owned local-development container execution primitive.

This module is deliberately independent of ``supervisor.py``.  It stages an
explicit snapshot from a pinned workspace descriptor and mounts *only* that
snapshot into a Docker container; it never executes a project command on the
host.  This is a practical Docker Desktop sandbox, not a hostile-code
boundary: an actor that controls the host Docker daemon can escape it.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
import pwd
from pathlib import Path
import re
import selectors
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import uuid


_DIGEST_PREFIX = "@sha256:"
# Host-owned policy, never populated from PATH, project files, or requests.
_TRUSTED_DOCKER_PATHS = (
    "/Applications/Docker.app/Contents/Resources/bin/docker",
    "/usr/bin/docker",
    "/usr/local/bin/docker",
)


class ConfigurationError(ValueError):
    """The caller or configured runtime cannot provide the required boundary."""


class InputRejected(ValueError):
    """A project-controlled input cannot safely become staged input."""


@dataclass(frozen=True)
class _DockerCLI:
    runtime: str
    endpoint: str
    config: str

    def run(self, args, **kwargs):
        return subprocess.run(self.command(args), env=self.environment(), **kwargs)

    def popen(self, args, **kwargs):
        return subprocess.Popen(self.command(args), env=self.environment(), **kwargs)

    def command(self, args):
        return [self.runtime, "--host", self.endpoint, "--config", self.config, *args]

    @staticmethod
    def environment():
        # Do not inherit Docker contexts, proxies, HOME, or loader settings.
        return {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}


def _sha256(value):
    return hashlib.sha256(value).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _safe_relative(path):
    if not isinstance(path, str) or not path or path.startswith("/"):
        raise InputRejected("input paths must be nonempty relative paths")
    parts = path.split("/")
    if len(parts) > 64:
        raise InputRejected("input path depth exceeds 64 components")
    if any(part in {"", ".", ".."} for part in parts):
        raise InputRejected("input paths may not contain empty, dot, or parent components")
    return tuple(parts)


def _remove_tree(path):
    try:
        shutil.rmtree(path)
    except OSError:
        return False
    return not Path(path).exists()


def _remove_tree_fd(directory_fd):
    """Remove a tree only through a descriptor retained by the caller."""
    try:
        for name in os.listdir(directory_fd):
            entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(entry.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=directory_fd)
                try:
                    _remove_tree_fd(child)
                finally:
                    os.close(child)
                os.rmdir(name, dir_fd=directory_fd)
            else:
                os.unlink(name, dir_fd=directory_fd)
    except OSError:
        return False
    return True


def _open_or_create_parent(root_fd, parts):
    """Open the parent of safe relative parts without using a pathname."""
    current = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            try:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=current)
            except FileNotFoundError:
                os.mkdir(part, mode=0o711, dir_fd=current)
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=current)
            os.close(current)
            current = next_fd
            os.fchmod(current, 0o711)
        return current
    except Exception:
        os.close(current)
        raise


def _open_staging_parent(path):
    """Return a descriptor for a private, host-owned staging directory."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        fd = os.open(os.fspath(path), flags)
    except OSError as error:
        raise ConfigurationError("staging parent must be a real host-owned directory") from error
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise ConfigurationError("staging parent must be a private host-owned directory")
        return fd
    except Exception:
        os.close(fd)
        raise


def _create_stage(parent_fd, parent_path):
    """Create the snapshot through the retained parent descriptor."""
    parent_path = Path(parent_path)
    for _ in range(16):
        name = f"agent-canvas-input-{uuid.uuid4().hex}"
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            continue
        root = parent_path / name
        try:
            created = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            visible = os.lstat(root)
        except OSError as error:
            try:
                os.rmdir(name, dir_fd=parent_fd)
            except OSError:
                pass
            raise ConfigurationError("staging parent changed while creating the snapshot") from error
        if (created.st_dev, created.st_ino) != (visible.st_dev, visible.st_ino):
            try:
                os.rmdir(name, dir_fd=parent_fd)
            except OSError:
                pass
            raise ConfigurationError("staging parent changed while creating the snapshot")
        return root
    raise ConfigurationError("could not allocate a unique staging directory")


@dataclass(frozen=True)
class ProjectIdentity:
    device: int
    inode: int

    @classmethod
    def capture(cls, root):
        root = os.fspath(root)
        try:
            info = os.lstat(root)
        except OSError as error:
            raise ConfigurationError("project root is unreadable") from error
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ConfigurationError("project root must be a real directory, not a link")
        return cls(info.st_dev, info.st_ino)


class PinnedProject:
    """A retained root descriptor.  Never reopen the workspace by pathname."""

    def __init__(self, fd, identity, path):
        self.fd, self.identity, self.path = fd, identity, Path(path)

    @classmethod
    def open(cls, root, expected):
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            fd = os.open(os.fspath(root), flags)
        except OSError as error:
            raise ConfigurationError("cannot open the project root without following links") from error
        observed = os.fstat(fd)
        identity = ProjectIdentity(observed.st_dev, observed.st_ino)
        if identity != expected:
            os.close(fd)
            raise ConfigurationError("project root identity changed before staging")
        try:
            path = Path(root).resolve(strict=True)
            resolved = os.stat(path, follow_symlinks=False)
        except OSError as error:
            os.close(fd)
            raise ConfigurationError("project root changed before staging") from error
        if (resolved.st_dev, resolved.st_ino) != (identity.device, identity.inode):
            os.close(fd)
            raise ConfigurationError("project root changed before staging")
        return cls(fd, identity, path)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


@dataclass
class StagedInputs:
    root: Path
    files: tuple[str, ...]
    digest: str
    byte_count: int
    _parent_fd: int
    _stage_fd: int

    def visible_root(self):
        """Return ``root`` only while the retained parent still names this stage."""
        if self._parent_fd is None or self._stage_fd is None:
            raise ConfigurationError("staging snapshot is no longer pinned")
        expected = os.fstat(self._stage_fd)
        try:
            through_parent = os.stat(self.root.name, dir_fd=self._parent_fd, follow_symlinks=False)
            visible = os.lstat(self.root)
        except OSError as error:
            raise ConfigurationError("staging parent changed while preparing the snapshot") from error
        identity = (expected.st_dev, expected.st_ino)
        if identity != (through_parent.st_dev, through_parent.st_ino) or identity != (visible.st_dev, visible.st_ino):
            raise ConfigurationError("staging parent changed while preparing the snapshot")
        return self.root

    def remove(self):
        """Remove this stage only if its retained parent still names its FD."""
        if self._parent_fd is None or self._stage_fd is None:
            return False
        try:
            expected = os.fstat(self._stage_fd)
            named = os.stat(self.root.name, dir_fd=self._parent_fd, follow_symlinks=False)
            if (expected.st_dev, expected.st_ino) != (named.st_dev, named.st_ino):
                return False
            if not _remove_tree_fd(self._stage_fd):
                return False
            os.rmdir(self.root.name, dir_fd=self._parent_fd)
            return True
        except OSError:
            return False

    def close(self):
        for attribute in ("_stage_fd", "_parent_fd"):
            fd = getattr(self, attribute)
            if fd is not None:
                os.close(fd)
                setattr(self, attribute, None)


def _open_relative(root_fd, parts):
    current = os.dup(root_fd)
    try:
        for index, part in enumerate(parts):
            last = index == len(parts) - 1
            flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
            if not last:
                flags |= os.O_DIRECTORY
            next_fd = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = next_fd
        return current
    except OSError as error:
        os.close(current)
        raise InputRejected("declared input is missing, a link, or not a regular file") from error


def stage_inputs(project, declared, staging_parent, *, max_bytes, max_files):
    """Copy declared regular files through ``project.fd`` into a 0700 snapshot.

    The returned directory is not inside the project and may be mounted after
    the source pathname is renamed or replaced.  The caller owns deletion.
    """
    if not isinstance(project, PinnedProject) or project.fd is None:
        raise ConfigurationError("a live pinned project descriptor is required")
    if not isinstance(max_bytes, int) or max_bytes < 0 or not isinstance(max_files, int) or max_files < 0:
        raise ConfigurationError("staging bounds must be non-negative integers")
    paths = tuple(declared)
    if len(paths) > max_files:
        raise InputRejected("declared input count exceeds the staging limit")
    try:
        Path(staging_parent).resolve(strict=True).relative_to(project.path)
    except ValueError:
        pass
    except OSError as error:
        raise ConfigurationError("staging parent must be a real host-owned directory") from error
    else:
        raise ConfigurationError("staging parent must be outside the project")
    parent_fd = _open_staging_parent(staging_parent)
    stage_fd = None
    root = None
    snapshot = None
    try:
        root = _create_stage(parent_fd, staging_parent)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        stage_fd = os.open(root.name, flags, dir_fd=parent_fd)
        snapshot = StagedInputs(root, (), "", 0, parent_fd, stage_fd)
        # The forced non-root container user must traverse the bind mount.  Direct
        # host enumeration stays disabled, while the mount itself is read-only.
        os.fchmod(stage_fd, 0o711)
        manifest, copied, total = [], [], 0
        for declared_path in paths:
            parts = _safe_relative(declared_path)
            fd = _open_relative(project.fd, parts)
            try:
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                    raise InputRejected("only singly-linked regular files may be staged")
                if before.st_size < 0 or before.st_size > max_bytes - total:
                    raise InputRejected("input bytes exceed the staging limit")
                target_parent = _open_or_create_parent(stage_fd, parts)
                # The path was derived solely from safe components under root.
                digest = hashlib.sha256()
                copied_bytes = 0
                try:
                    target_fd = os.open(
                        parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                        0o600, dir_fd=target_parent,
                    )
                finally:
                    os.close(target_parent)
                with os.fdopen(os.dup(fd), "rb", closefd=True) as source, os.fdopen(target_fd, "wb", closefd=True) as output:
                    while chunk := source.read(64 * 1024):
                        copied_bytes += len(chunk)
                        total += len(chunk)
                        if copied_bytes > before.st_size or total > max_bytes:
                            raise InputRejected("input changed or exceeded its staging limit")
                        digest.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                    os.fchmod(output.fileno(), 0o444)
                after = os.fstat(fd)
                if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
                ) or copied_bytes != before.st_size:
                    raise InputRejected("input changed while it was staged")
                copied.append(declared_path)
                manifest.append({"path": declared_path, "bytes": copied_bytes, "sha256": digest.hexdigest()})
            finally:
                os.close(fd)
        digest = _sha256(_canonical(manifest))
        snapshot.files, snapshot.digest, snapshot.byte_count = tuple(copied), digest, total
        return snapshot
    except Exception as error:
        cleanup_ok = root is None
        try:
            if snapshot is not None:
                cleanup_ok = snapshot.remove()
            elif root is not None:
                # No files have been copied before the stage descriptor opens.
                # Remove the empty directory without acquiring another fd.
                try:
                    os.rmdir(root.name, dir_fd=parent_fd)
                    cleanup_ok = True
                except FileNotFoundError:
                    cleanup_ok = True
                except OSError:
                    cleanup_ok = False
        finally:
            if snapshot is not None:
                snapshot.close()
            else:
                if stage_fd is not None:
                    os.close(stage_fd)
                os.close(parent_fd)
        if not cleanup_ok:
            raise ConfigurationError("rejected input stage cleanup could not be confirmed") from error
        raise


@dataclass(frozen=True)
class ExecutionRequest:
    action_id: str
    attempt_id: str
    project: Path
    image: str
    command: tuple[str, ...] | list[str]
    inputs: tuple[str, ...] | list[str]
    timeout_s: int = 30
    max_output_bytes: int = 8192
    memory: str = "256m"
    cpus: str = "1.0"
    pids: int = 64

    def __post_init__(self):
        for identifier in (self.action_id, self.attempt_id):
            if not isinstance(identifier, str) or not identifier.strip():
                raise ConfigurationError("action and attempt identifiers must be nonempty strings")
            try:
                identifier.encode("utf-8")
            except UnicodeEncodeError as error:
                raise ConfigurationError("action and attempt identifiers must be valid UTF-8") from error
        if not isinstance(self.image, str) or self.image.count(_DIGEST_PREFIX) != 1:
            raise ConfigurationError("image must be digest-pinned (name@sha256:<digest>)")
        if "\0" in self.image:
            raise ConfigurationError("image cannot contain NUL")
        try:
            self.image.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ConfigurationError("image must be valid UTF-8") from error
        _, digest = self.image.rsplit(_DIGEST_PREFIX, 1)
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ConfigurationError("image digest must be a lower-case sha256")
        if not isinstance(self.command, (list, tuple)) or not self.command or not all(isinstance(value, str) and value for value in self.command):
            raise ConfigurationError("command must be a nonempty argument vector")
        for value in self.command:
            if "\0" in value:
                raise ConfigurationError("command arguments cannot contain NUL")
            try:
                value.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ConfigurationError("command arguments must be valid UTF-8") from exc
        if isinstance(self.timeout_s, bool) or not isinstance(self.timeout_s, int) or not 0 < self.timeout_s <= 300:
            raise ConfigurationError("timeout must be between one and 300 seconds")
        if isinstance(self.max_output_bytes, bool) or not isinstance(self.max_output_bytes, int) or not 0 <= self.max_output_bytes <= 1_048_576:
            raise ConfigurationError("output limit must be between zero and one MiB")
        if isinstance(self.pids, bool) or not isinstance(self.pids, int) or not 1 <= self.pids <= 256:
            raise ConfigurationError("process limit must be between one and 256")
        memory = re.fullmatch(r"([1-9][0-9]*)([mMgG])", self.memory) if isinstance(self.memory, str) else None
        if not memory or not 16 <= int(memory.group(1)) * (1024 if memory.group(2).lower() == "g" else 1) <= 1024:
            raise ConfigurationError("memory limit must be between 16m and 1g")
        if isinstance(self.cpus, bool):
            raise ConfigurationError("CPU limit must be numeric, not boolean")
        try:
            cpus = float(self.cpus)
        except (TypeError, ValueError) as error:
            raise ConfigurationError("CPU limit must be numeric") from error
        if not 0 < cpus <= 4:
            raise ConfigurationError("CPU limit must be greater than zero and at most four")
        object.__setattr__(self, "cpus", format(cpus, ".15g"))
        object.__setattr__(self, "command", tuple(self.command))
        if not isinstance(self.inputs, (list, tuple)):
            raise ConfigurationError("inputs must be an ordered list or tuple of paths")
        object.__setattr__(self, "inputs", tuple(self.inputs))


@dataclass(frozen=True)
class ExecutionReceipt:
    action_id: str
    attempt_id: str
    input_digest: str
    command: tuple[str, ...]
    image: str
    runtime: str
    runtime_version: str
    runtime_endpoint: str
    exit_status: int | str
    timed_out: bool
    cancelled: bool
    output: str
    output_truncated: bool
    digest: str

    @classmethod
    def build(cls, **values):
        output = values.pop("output")
        payload = {**values, "command": list(values["command"]), "output": output,
                   "output_digest": _sha256(output.encode())}
        return cls(**values, output=output, digest=_sha256(_canonical(payload)))

    def verify(self):
        payload = {**asdict(self), "command": list(self.command)}
        digest = payload.pop("digest")
        payload["output_digest"] = _sha256(self.output.encode())
        return digest == _sha256(_canonical(payload))

    def matches(self, *, action_id, attempt_id, input_digest, command, image):
        return self.verify() and (self.action_id, self.attempt_id, self.input_digest, self.command, self.image) == (
            action_id, attempt_id, input_digest, tuple(command), image
        )


class ContainerExecutor:
    """Docker-only executor.  It has no host-process fallback by design."""

    @staticmethod
    def _endpoint_candidates():
        candidates = [Path("/var/run/docker.sock")]
        try:
            home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
        except KeyError:
            return candidates
        if home.is_absolute():
            candidates.append(home / ".docker" / "run" / "docker.sock")
        return candidates

    @classmethod
    def _select_endpoint(cls, project):
        project = Path(project).resolve(strict=True)
        for candidate in cls._endpoint_candidates():
            try:
                endpoint = Path(candidate).resolve(strict=True)
                info = endpoint.stat()
            except (OSError, RuntimeError):
                continue
            if (not endpoint.is_relative_to(project) and stat.S_ISSOCK(info.st_mode)
                    and info.st_uid in {0, os.geteuid()}):
                return "unix://" + str(endpoint)
        raise ConfigurationError("trusted local Docker socket is unavailable")

    @staticmethod
    def _select_runtime(project):
        """Resolve an installed host CLI once; PATH is not a trust source.

        Installation directories and the host account are trusted in this
        local-development model (including admin-writable macOS app folders).
        Symlink aliases are allowed, but their final target must be outside
        the project and owned by root or the current host account.
        """
        try:
            project = Path(project).resolve(strict=True)
            for candidate in _TRUSTED_DOCKER_PATHS:
                if not Path(candidate).is_absolute():
                    continue
                try:
                    runtime = Path(candidate).resolve(strict=True)
                    info = runtime.stat()
                except (OSError, RuntimeError):
                    continue
                if runtime.is_relative_to(project):
                    continue
                if (not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.geteuid()}
                        or info.st_mode & 0o022 or info.st_nlink != 1 or not os.access(runtime, os.X_OK)):
                    continue
                return str(runtime)
        except (OSError, RuntimeError) as error:
            raise ConfigurationError("cannot resolve the project for trusted Docker selection") from error
        raise ConfigurationError("trusted Docker runtime is unavailable; refusing host execution")

    @classmethod
    def plan(cls, request, staged_inputs, name, *, runtime):
        """Return the exact ``docker create`` argv without invoking a shell."""
        staged = Path(staged_inputs).resolve()
        if not staged.is_dir():
            raise ConfigurationError("a host-owned staging directory is required")
        return [runtime, "create", "--pull=never", "--log-driver", "none", "--name", name, "--network", "none", "--user", "65532:65532",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only", "--pids-limit", str(request.pids),
                "--memory", request.memory, "--cpus", request.cpus, "--ulimit", "nofile=64:64",
                "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=16m", "--tmpfs", "/outputs:rw,noexec,nosuid,nodev,size=16m",
                "--workdir", "/inputs", "--env", "HOME=/nonexistent", "--env", "LANG=C.UTF-8",
                "--env", "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "--mount", f"type=bind,src={staged},dst=/inputs,readonly", request.image, *request.command]

    @classmethod
    def _check_runtime(cls, request, runtime):
        try:
            version = runtime.run(["version", "--format", "{{.Server.Version}}"],
                                     capture_output=True, text=True, timeout=5, check=True).stdout.strip()
            # Inspect is intentionally before create: Docker create otherwise pulls a
            # missing image, which makes an unapproved image available implicitly.
            # Return fixed-size metadata, not labels/history or volume names.
            # A nonempty volume map needs only a constant rejection marker.
            image_format = ('{"Id":{{json .Id}},"Config":{"Volumes":'
                            '{{if .Config.Volumes}}{"declared":{}}{{else}}null{{end}}}}')
            image_metadata = runtime.run(["image", "inspect", request.image, "--format", image_format],
                                            capture_output=True, text=True, timeout=5, check=True).stdout.strip()
        except (OSError, subprocess.SubprocessError) as error:
            raise ConfigurationError("Docker daemon or the pinned image is unavailable") from error
        try:
            image = json.loads(image_metadata)
        except (ValueError, TypeError) as error:
            raise ConfigurationError("Docker returned invalid image metadata") from error
        if not isinstance(image, dict) or not isinstance(image.get("Config"), dict):
            raise ConfigurationError("Docker returned invalid image metadata")
        image_id = image.get("Id")
        if not version or not isinstance(image_id, str) or not image_id.startswith("sha256:"):
            raise ConfigurationError("Docker did not report an immutable server and image identity")
        volumes = image["Config"].get("Volumes")
        if volumes is not None and not isinstance(volumes, dict):
            raise ConfigurationError("Docker returned invalid image volumes metadata")
        # Image-declared anonymous volumes remain writable with --read-only
        # and are not constrained by the explicit tmpfs size limits.
        if volumes:
            raise ConfigurationError("image-declared volumes are not permitted")
        return version, image_id

    @classmethod
    def _remove_container(cls, name, runtime):
        """Best-effort Docker cleanup whose failure remains a failed execution."""
        try:
            removed = runtime.run(["rm", "-f", "-v", name], capture_output=True, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            return False
        if removed.returncode == 0:
            return True
        try:
            absent = runtime.run(["container", "inspect", name], capture_output=True,
                                    timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            return False
        if absent.returncode == 0:
            return False
        error = absent.stderr.decode("utf-8", "replace") if isinstance(absent.stderr, bytes) else (absent.stderr or "")
        return any(message in error for message in (
            f"No such container: {name}",
            f"No such object: {name}",
        ))

    @classmethod
    def _container_exited(cls, name, runtime):
        """Confirm the container already exited before accepting a failed kill."""
        try:
            inspected = runtime.run(
                ["container", "inspect", "--format", "{{.State.Status}}", name],
                capture_output=True, text=True, timeout=5, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return inspected.returncode == 0 and inspected.stdout.strip() == "exited"

    @staticmethod
    def _remove_stage(path):
        """Do not return a validation receipt while a stage may still exist."""
        return _remove_tree(path)

    @classmethod
    def run(cls, request, identity, staging_parent, *, cancellation=None):
        """Stage and execute, returning an integrity-bound receipt on every outcome.

        ``cancellation`` may be a ``threading.Event``.  A launch or cleanup
        ambiguity raises ``ConfigurationError`` rather than implying success.
        """
        if cancellation is not None and not isinstance(cancellation, threading.Event):
            raise ConfigurationError("cancellation must be a threading.Event")
        runtime = cls._select_runtime(request.project)
        endpoint = cls._select_endpoint(request.project)
        # Fixed host temporary parent: tempfile's default trusts TMPDIR/TEMP/TMP.
        parent = Path("/tmp").resolve(strict=True)
        if parent.is_relative_to(Path(request.project).resolve(strict=True)):
            raise ConfigurationError("Docker configuration must be outside the project")
        with tempfile.TemporaryDirectory(prefix="agent-canvas-docker-", dir=parent) as config:
            return cls._run(request, identity, staging_parent, _DockerCLI(runtime, endpoint, config), cancellation)

    @classmethod
    def _run(cls, request, identity, staging_parent, runtime, cancellation):
        version, _image_id = cls._check_runtime(request, runtime)
        name = "agent-canvas-" + _sha256(f"{request.action_id}:{request.attempt_id}:{uuid.uuid4()}".encode())[:24]
        output, truncated, timed_out, cancelled, status = b"", False, False, False, "launch_failed"
        create_attempted = False
        with PinnedProject.open(request.project, identity) as pinned:
            snapshot = stage_inputs(pinned, request.inputs, staging_parent, max_bytes=16 * 1024 * 1024, max_files=128)
        try:
            # A cancellation before either Docker lifecycle transition must not
            # start a new container.  Staging remains necessary to bind the
            # cancelled receipt to the exact declared input snapshot.
            if cancellation is not None and cancellation.is_set():
                cancelled, status = True, "cancelled"
            else:
                create_attempted = True
                created = runtime.run(
                    cls.plan(request, snapshot.visible_root(), name, runtime=runtime.runtime)[1:], capture_output=True, timeout=10, check=False
                )
                if created.returncode:
                    raise ConfigurationError("Docker rejected the required isolation configuration")
                # Recheck the staging pathname before start.  Docker resolves
                # the bind mount during start, so this is not atomic binding;
                # host-account staging mutation is outside this boundary.
                snapshot.visible_root()
                if cancellation is not None and cancellation.is_set():
                    cancelled, status = True, "cancelled"
                else:
                    process = runtime.popen(["start", "--attach", name], stdout=subprocess.PIPE,
                                               stderr=subprocess.STDOUT, start_new_session=True)
                    deadline = time.monotonic() + request.timeout_s
                    selector = selectors.DefaultSelector()
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while True:
                        if cancellation is not None and cancellation.is_set():
                            cancelled = True
                            break
                        if time.monotonic() >= deadline:
                            timed_out = True
                            break
                        events = selector.select(timeout=min(0.05, max(0, deadline - time.monotonic())))
                        chunk = process.stdout.read1(4096) if events and process.stdout else b""
                        if chunk:
                            available = request.max_output_bytes - len(output)
                            output += chunk[:max(0, available)]
                            truncated |= len(chunk) > available
                            if truncated:
                                break
                            continue
                        if events and process.poll() is not None:
                            # Only an actual empty read is EOF. A selector
                            # timeout followed by exit may still leave bytes
                            # to drain on the next iteration under these caps.
                            break
                        if not events:
                            continue
                    selector.close()
                    if timed_out or cancelled or truncated:
                        stopped = runtime.run(["kill", name], capture_output=True, timeout=10, check=False)
                        if stopped.returncode and not cls._container_exited(name, runtime):
                            raise ConfigurationError("Docker could not stop the output-limited container")
                        # ``communicate`` would buffer every byte emitted after
                        # the cap.  Closing the pipe first bounds host memory;
                        # Docker kill has already stopped the producer.
                        if process.stdout is not None:
                            process.stdout.close()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired as error:
                        raise ConfigurationError("Docker attach did not exit after output handling") from error
                    if process.stdout is not None:
                        process.stdout.close()
                    status = process.returncode if not (timed_out or cancelled) else ("timeout" if timed_out else "cancelled")
        finally:
            cleanup_ok = True
            if create_attempted:
                cleanup_ok = cls._remove_container(name, runtime)
            try:
                stage_removed = snapshot.remove()
            finally:
                snapshot.close()
            if not cleanup_ok or not stage_removed:
                raise ConfigurationError("Docker or host staging cleanup could not be confirmed")
        return ExecutionReceipt.build(action_id=request.action_id, attempt_id=request.attempt_id, input_digest=snapshot.digest,
                                      command=request.command, image=request.image, runtime=runtime.runtime,
                                      runtime_endpoint=runtime.endpoint,
                                      runtime_version=version, exit_status=status, timed_out=timed_out,
                                      cancelled=cancelled, output=output.decode("utf-8", "replace"),
                                      output_truncated=truncated)
