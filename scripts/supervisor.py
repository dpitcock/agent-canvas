#!/usr/bin/env python3
"""Host-owned task state and final-delivery decisions for supervised Codex clients.

This module deliberately never reads workspace task files as completion evidence.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager

import fcntl


DEFAULT_PROHIBITED = ("push", "publish", "pr_create", "merge", "destructive", "credential_change",
                      "config_change", "external_message")
TERMINAL_STATUSES = frozenset(("complete", "blocked", "cancelled"))
OWNER_OVERRIDE_MODES = {"pause", "bypass-review", "reset"}
_VALIDATOR_LAUNCHER = (
    "import os, sys\n"
    "directory = int(sys.argv[1])\n"
    "os.fchdir(directory)\n"
    "os.close(directory)\n"
    "os.execvp(sys.argv[2], sys.argv[2:])\n"
)


class StateCommitUncertainError(OSError):
    """Replacement is visible, but its directory sync was not confirmed."""


class Decision:
    def __init__(self, kind, *, release=False, message="", next_action=None, durability_confirmed=True):
        self.kind, self.release, self.message, self.next_action = kind, release, message, next_action
        self.durability_confirmed = durability_confirmed


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _now():
    return time.time_ns()


def _text_output(value):
    return value.decode(errors="replace") if isinstance(value, bytes) else (value or "")


def _binary_digest(path, *, directory_descriptor=None):
    """Hash a regular executable, resolving explicit relative paths from a pinned directory."""
    descriptor = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
        if directory_descriptor is not None and not os.path.isabs(path) and "/" in path:
            descriptor = os.open(path, flags, dir_fd=directory_descriptor)
        else:
            descriptor = os.open(path, flags)
        mode = os.fstat(descriptor).st_mode
        if not stat.S_ISREG(mode) or not mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
            return None
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 64 * 1024):
            digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _validator_binary_digest(command, *, directory_descriptor=None):
    """Hash the executable exactly as execvp will resolve it for a pinned project."""
    if directory_descriptor is None or os.path.isabs(command) or "/" in command:
        executable = shutil.which(command) or command
        return _binary_digest(executable, directory_descriptor=directory_descriptor)
    for directory in os.get_exec_path():
        candidate = os.path.join(directory, command) if directory else command
        digest = _binary_digest(
            candidate,
            directory_descriptor=None if os.path.isabs(candidate) else directory_descriptor,
        )
        if digest is not None:
            return digest
    return None


class HostSupervisor:
    """Durable state store whose root must be outside the supervised workspace."""

    def __init__(self, state_dir):
        self.root = Path(state_dir).expanduser().resolve()

    def _ensure_root(self):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        status = self.root.stat()
        if status.st_uid != os.geteuid() or status.st_mode & 0o777 != 0o700:
            raise ValueError("Supervisor state root must be caller-owned with mode 0700; choose a dedicated private directory")
        if any(entry.name not in {"projects", "validator-snapshots"} for entry in self.root.iterdir()):
            raise ValueError("Supervisor state root must be dedicated to supervisor state")

    def _projects_dir(self):
        """Return the host-owned project state directory without following a link."""
        self._ensure_root()
        projects = self.root / "projects"
        try:
            projects.lstat()
        except FileNotFoundError:
            projects.mkdir(mode=0o700)
        else:
            if projects.is_symlink():
                raise ValueError("Supervisor projects directory must not be a symlink")
            if not projects.is_dir():
                raise ValueError("Supervisor projects path must be a directory")
            try:
                projects.chmod(0o700)
            except OSError:
                pass
        return projects

    def _project_key(self, project):
        # A symlink replacement must not redirect lookup to another registration.
        return hashlib.sha256(str(self._project_path(project)).encode()).hexdigest()

    @staticmethod
    def _project_path(project):
        return Path(os.path.abspath(Path(project).expanduser()))

    @staticmethod
    def _validate_task_id(task_id):
        if not isinstance(task_id, str) or not task_id or any(
                character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
                for character in task_id):
            raise ValueError("task_id must contain only letters, digits, hyphen, and underscore")

    @staticmethod
    def _project_identity(project):
        status = Path(project).lstat()
        if not stat.S_ISDIR(status.st_mode):
            raise ValueError("Project root identity must be a real directory")
        return {"device": status.st_dev, "inode": status.st_ino}

    @staticmethod
    def _state_directory(path, *, create=False, description):
        """Return a state directory only when it is a real directory, never a link."""
        try:
            path.lstat()
        except FileNotFoundError:
            if not create:
                raise ValueError(f"Supervisor {description} does not exist")
            path.mkdir(mode=0o700)
        else:
            if path.is_symlink():
                raise ValueError(f"Supervisor {description} must not be a symlink")
            if not path.is_dir():
                raise ValueError(f"Supervisor {description} must be a directory")
        try:
            path.chmod(0o700)
        except OSError:
            pass
        return path

    @staticmethod
    def _state_file(path, *, required=False, description="state file"):
        """Reject links and directories before a host-state file is read or replaced."""
        try:
            path.lstat()
        except FileNotFoundError:
            if required:
                raise ValueError(f"Supervisor {description} does not exist")
            return path
        if path.is_symlink():
            raise ValueError(f"Supervisor {description} must not be a symlink")
        if not path.is_file():
            raise ValueError(f"Supervisor {description} must be a regular file")
        return path

    def _project_dir(self, project, *, create=False):
        project_dir = self._projects_dir() / self._project_key(project)
        return self._state_directory(project_dir, create=create, description="project state directory")

    def _registration(self, project, *, create=False):
        registration = self._project_dir(project, create=create) / "registration.json"
        return self._state_file(registration, description="project registration")

    def _tasks_dir(self, project, *, create=False):
        project_dir = self._project_dir(project, create=create)
        return self._state_directory(project_dir / "tasks", create=create, description="task state directory")

    def _registered_project(self, project):
        """Return a registered project only while its original directory still exists."""
        project = self._project_path(project)
        identity = self._project_identity(project)
        if not project.is_dir():
            raise ValueError("Supervised project must be an existing directory")
        registration = self._registration(project)
        if not registration.is_file():
            raise ValueError("Supervised project is not registered")
        current = self._read(registration)
        if current.get("project") != str(project):
            raise ValueError("Host registration project mismatch")
        if current.get("identity") != identity:
            raise ValueError("Host registration project directory identity mismatch")
        return project

    @contextmanager
    def _pinned_registered_project(self, project):
        """Keep the registered project directory open while a validator runs."""
        project = self._registered_project(project)
        registration = self._read(self._registration(project))
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(project, flags)
        except OSError as error:
            raise ValueError("Supervised project directory cannot be opened safely") from error
        try:
            status = os.fstat(descriptor)
            if {"device": status.st_dev, "inode": status.st_ino} != registration.get("identity"):
                raise ValueError("Host registration project directory identity mismatch")
            yield descriptor
        finally:
            os.close(descriptor)

    def _task_path(self, task_id, project=None):
        self._validate_task_id(task_id)
        if project is not None:
            project = self._registered_project(project)
            path = self._state_file(self._tasks_dir(project) / f"{task_id}.json", description="task state")
            if not path.exists():
                raise ValueError(f"Unknown task: {task_id}")
            return path
        projects = self._projects_dir()
        matches = []
        for project_dir in projects.iterdir():
            self._state_directory(project_dir, description="project state directory")
            tasks_path = project_dir / "tasks"
            try:
                tasks_path.lstat()
            except FileNotFoundError:
                continue
            tasks = self._state_directory(tasks_path, description="task state directory")
            candidate = self._state_file(tasks / f"{task_id}.json", description="task state")
            if candidate.is_file():
                matches.append(candidate)
        if len(matches) != 1:
            raise ValueError(f"Unknown or ambiguous task: {task_id}")
        registration = self._read(matches[0].parents[1] / "registration.json")
        self._registered_project(registration.get("project"))
        return matches[0]

    def _task_lock_path(self, task_id, project=None, *, create=False):
        self._validate_task_id(task_id)
        if project is not None:
            project = self._registered_project(project)
            return self._state_file(self._tasks_dir(project, create=create) / f"{task_id}.lock", description="task lock")
        return self._state_file(self._task_path(task_id).with_suffix(".lock"), description="task lock")

    @contextmanager
    def _locked_task(self, task_id, *, project=None, create=False):
        """Serialize one task's durable read-modify-write transitions."""
        lock_path = self._task_lock_path(task_id, project, create=create)
        with lock_path.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                path = self._task_path(task_id, project) if project is None else self._tasks_dir(project) / f"{task_id}.json"
                yield self._read(path) if path.exists() else None
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @staticmethod
    def _write(path, value):
        HostSupervisor._state_file(path, description="state file")
        data = json.dumps(value, indent=2, sort_keys=True) + "\n"
        temp = None
        try:
            with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as out:
                temp = Path(out.name)
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temp, path)
            try:
                directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            except OSError as error:
                raise StateCommitUncertainError("State replaced, but directory sync failed") from error
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)

    @staticmethod
    def _read(path):
        try:
            HostSupervisor._state_file(path, required=True, description="state file")
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"Host state is unreadable: {path}") from error

    def _check_project_location(self, project, *, expected_identity=None):
        project = self._project_path(project)
        identity = self._project_identity(project)
        if expected_identity is not None and (identity["device"], identity["inode"]) != expected_identity:
            raise ValueError("Project directory changed during installation")
        if not project.is_dir():
            raise ValueError("Supervised project must be an existing directory")
        try:
            self.root.relative_to(project.resolve())
        except ValueError:
            pass
        else:
            raise ValueError("Supervisor state directory must be outside the project workspace")
        try:
            project.resolve().relative_to(self.root)
        except ValueError:
            pass
        else:
            raise ValueError("Supervisor state directory must be outside the project workspace")
        return project

    @contextmanager
    def _locked_project(self, project, *, expected_identity=None):
        project = self._check_project_location(project, expected_identity=expected_identity)
        path = self._state_file(self._project_dir(project, create=True) / "project.lock", description="project lock")
        with path.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def provision(self, project, *, expected_identity=None):
        with self._locked_project(project, expected_identity=expected_identity):
            return self._provision_locked(project, expected_identity=expected_identity)

    def _provision_locked(self, project, *, expected_identity=None):
        project = self._check_project_location(project)
        identity = self._project_identity(project)
        if expected_identity is not None and (identity["device"], identity["inode"]) != expected_identity:
            raise ValueError("Project directory changed during installation")
        self._ensure_root()
        registration = self._registration(project, create=True)
        if registration.exists():
            current = self._read(registration)
            if current["project"] != str(project):
                raise ValueError("Host registration project mismatch")
            if current.get("identity") != identity:
                raise ValueError("Host registration project directory identity mismatch")
            return current
        value = {"schema_version": 1, "project": str(project), "identity": identity, "created_at": _now()}
        self._write(registration, value)
        return value

    def import_owner_override(self, project, modes, *, source_digest):
        """Import an owner-provided snapshot; never reread workspace overrides here."""
        if not isinstance(modes, (list, tuple, set)) or any(mode not in OWNER_OVERRIDE_MODES for mode in modes):
            raise ValueError("Unknown owner override mode")
        if not isinstance(source_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", source_digest):
            raise ValueError("Owner override snapshot requires a SHA-256 source digest")
        project = self._project_path(project)
        with self._locked_project(project):
            current = self._provision_locked(project)
            normalized = sorted(set(modes))
            current["owner_override"] = {"modes": normalized, "source_digest": source_digest,
                                         "imported_at": _now(), "import_complete": False}
            self._write(self._registration(project), current)
            self._event(None, "owner_override_imported", project=project, modes=normalized, source_digest=source_digest)
            tasks = self._tasks_dir(project, create=True)
            for path in sorted(tasks.glob("*.json")):
                if path.name.endswith(".interrupt.json"):
                    continue
                task = self._read(path)
                if task["status"] in TERMINAL_STATUSES:
                    continue
                if "reset" in normalized:
                    self.cancel(path.stem, project=project)
                    self._event(path.stem, "owner_override_reset", project=project)
                elif "pause" in normalized and task["status"] != "paused":
                    self.pause(path.stem, project=project)
                    self._event(path.stem, "owner_override_paused", project=project)
            current["owner_override"]["import_complete"] = True
            self._write(self._registration(project), current)
            return current

    def _project_for_task(self, task_id, project=None):
        return self._task_path(task_id, project).parents[1]

    def _event_path(self, task_id, project=None):
        directory = self._project_dir(project) if task_id is None else self._project_for_task(task_id, project)
        return self._state_file(directory / "audit.jsonl", description="audit log")

    @contextmanager
    def _locked_audit(self, task_id, project=None):
        """Serialize shared project audit recovery, reads and appends.

        Callers holding a task lock acquire this lock second. The audit file
        stays on the same inode; recovery only truncates an unfinished tail.
        """
        path = self._event_path(task_id, project)
        # Buffer reads for audit iteration; writes use os.write exclusively, so
        # closing this stream after unlocking cannot flush a pending record.
        with path.open("a+b") as out:
            fcntl.flock(out, fcntl.LOCK_EX)
            try:
                size = out.seek(0, os.SEEK_END)
                cursor = size
                boundary = 0
                # Appends inspect only the unfinished tail, in bounded blocks.
                # Complete records (including corruption) are left untouched;
                # audit() validates them while streaming the history.
                while cursor:
                    start = max(0, cursor - 65536)
                    out.seek(start)
                    chunk = out.read(cursor - start)
                    newline = chunk.rfind(b"\n")
                    if newline >= 0:
                        boundary = start + newline + 1
                        break
                    cursor = start
                if boundary != size:
                    out.truncate(boundary)
                    out.flush()
                    os.fsync(out.fileno())
                out.seek(0, os.SEEK_END)
                yield out
            finally:
                fcntl.flock(out, fcntl.LOCK_UN)

    def _event(self, task_id, event_type, *, project=None, **details):
        event = {"at": _now(), "type": event_type, "task_id": task_id, **details}
        with self._locked_audit(task_id, project) as out:
            remaining = memoryview((json.dumps(event, sort_keys=True) + "\n").encode("utf-8"))
            while remaining:
                written = os.write(out.fileno(), remaining)
                if written <= 0:
                    raise OSError("Audit append made no progress")
                remaining = remaining[written:]
            out.flush()
            os.fsync(out.fileno())
        return event

    def audit(self, task_id, *, project=None):
        # The terminal decision and its audit event share one atomic task write.
        # Progress events remain in JSONL; it is not the complete delivery ledger.
        with self._locked_task(task_id, project=project) as task:
            events = []
            with self._locked_audit(task_id, project) as source:
                source.seek(0)
                try:
                    for line in source:
                        event = json.loads(line.decode("utf-8"))
                        if not isinstance(event, dict) or not {"task_id", "at", "type"} <= event.keys():
                            raise ValueError("Invalid audit event")
                        if event["task_id"] == task_id:
                            events.append(event)
                except (ValueError, UnicodeError) as error:
                    raise ValueError(f"Host audit is unreadable: {source.name}") from error
            if task.get("release_event"):
                events.append(task["release_event"])
            events.extend(task.get("prior_release_events", []))
            if task.get("creation_event"):
                events.append(task["creation_event"])
            return sorted(events, key=lambda event: event["at"])

    @staticmethod
    def _task_definition(action_map, validators, blockers, permitted_operations):
        return {
            "actions": {
                action_id: {
                    key: value for key, value in action.items()
                    if key not in {"status", "attempts", "evidence"}
                }
                for action_id, action in action_map.items()
            },
            "validators": list(validators),
            "blockers": list(blockers),
            "permitted_operations": sorted(set(permitted_operations)),
        }

    @staticmethod
    def _validate_validator_ids(validators):
        seen = set()
        for validator in validators:
            identifier = validator.get("id") if isinstance(validator, dict) else None
            if not isinstance(identifier, str) or not identifier.strip() or identifier in seen:
                raise ValueError("Host validators require unique nonempty string ids")
            seen.add(identifier)

    def create_task(self, task_id, project, actions, *, validators=(), blockers=(), permitted_operations=("read", "write", "delegate")):
        self._validate_task_id(task_id)
        with self._locked_project(project):
            registration = self._provision_locked(project)
            return self._create_task_locked(task_id, project, registration, actions, validators, blockers, permitted_operations)

    def _create_task_locked(self, task_id, project, registration, actions, validators, blockers, permitted_operations):
        actions = list(actions)
        validators = list(validators)
        self._validate_validator_ids(validators)
        for validator in validators:
            self._validate_validator_definition(validator)
        blockers = list(blockers)
        for blocker in blockers:
            if not isinstance(blocker, dict):
                raise ValueError("blockers must be records")
            if "owner_action" in blocker:
                message = blocker["owner_action"]
                if not isinstance(message, str) or not message.strip():
                    raise ValueError("blocker owner_action must be a nonempty string")
                try:
                    message.encode("utf-8")
                except UnicodeEncodeError as exc:
                    raise ValueError("blocker owner_action must be valid UTF-8") from exc
        permitted_operations = tuple(permitted_operations)
        prohibited = sorted(set(permitted_operations).intersection(DEFAULT_PROHIBITED))
        if prohibited:
            raise ValueError("task authorization includes prohibited operations: " + ", ".join(prohibited))
        path = self._state_file(self._tasks_dir(project, create=True) / f"{task_id}.json", description="task state")
        action_map = {}
        for action in actions:
            action = dict(action)
            action_id = action.pop("id", None)
            if not isinstance(action_id, str) or not action_id.strip() or action_id in action_map:
                raise ValueError("actions require unique nonempty string ids")
            operation = action.get("operation")
            if operation not in permitted_operations or operation in DEFAULT_PROHIBITED:
                raise ValueError("action operation is not permitted by task authorization")
            action_map[action_id] = {**action, "status": "pending", "attempts": [], "evidence": None}
        definition = self._task_definition(action_map, validators, blockers, permitted_operations)
        with self._locked_task(task_id, project=project, create=True):
            if path.exists():
                task = self._read(path)
                if definition != self._task_definition(
                    task["actions"], task["validators"], task["blockers"],
                    task["authorization"]["permitted_operations"],
                ):
                    raise ValueError("task_id already has a different durable definition")
                return task
            authorization = {"revision": 1, "permitted_operations": sorted(set(permitted_operations)),
                             "prohibited_operations": list(DEFAULT_PROHIBITED)}
            authorization["digest"] = _digest(authorization)
            task = {"schema_version": 1, "task_id": task_id, "project": registration["project"],
                    "status": "paused" if "pause" in registration.get("owner_override", {}).get("modes", []) else "active",
                    "authorization": authorization, "actions": action_map, "validators": list(validators),
                    "blockers": list(blockers), "evidence": {"validators": {}}, "visible_messages": []}
            task["creation_event"] = {"at": _now(), "type": "task_created", "task_id": task_id,
                                      "authorization_digest": authorization["digest"]}
            if task["status"] == "paused":
                task["creation_event"]["owner_override"] = "pause"
            self._write(path, task)
            return task

    def task(self, task_id, *, project=None):
        return self._read(self._task_path(task_id, project))

    def _save_task(self, task):
        self._write(self._task_path(task["task_id"], task["project"]), task)

    def _commit_release(self, task, attempt_id, message, *, blocker=False, blocker_id=None,
                        event_type=None, release_details=None):
        """Commit release authorization, message and audit evidence together.

        This records a host decision, not an acknowledgement that a UI rendered
        it. A client crash after this commit can recover the visible message.
        """
        if task.get("release_event"):
            task.setdefault("prior_release_events", []).append(task["release_event"])
        task["release_event"] = {
            "at": _now(), "task_id": task["task_id"],
            "type": "blocker_release_committed" if blocker else "final_release_committed",
            "attempt_id": attempt_id,
            "content_digest": hashlib.sha256(message.encode()).hexdigest(),
        }
        if blocker:
            task["release_event"]["blocker_id"] = blocker_id if blocker_id is not None else task["blockers"][0].get("id")
        else:
            task.pop("withheld_final", None)
        if event_type is not None:
            task["release_event"]["type"] = event_type
        if release_details:
            task["release_event"].update(release_details)
        task["visible_messages"].append(message)
        task["status"] = "blocked" if blocker else "complete"
        durability_confirmed = True
        try:
            self._save_task(task)
        except StateCommitUncertainError:
            # The task lock is still held. Recover only this exact transition,
            # never a stale or unrelated terminal message.
            committed = self.task(task["task_id"], project=task["project"])
            if (committed.get("release_event") != task["release_event"]
                    or committed.get("prior_release_events") != task.get("prior_release_events")
                    or committed.get("status") != task["status"]
                    or committed.get("visible_messages") != task["visible_messages"]):
                raise
            durability_confirmed = False
        return Decision("blocker" if blocker else "complete", release=True, message=message,
                        durability_confirmed=durability_confirmed)

    def _interrupt_path(self, task_id, project=None):
        path = self._task_path(task_id, project)
        return self._state_file(path.with_name(path.stem + ".interrupt.json"), description="task interrupt")

    @contextmanager
    def _locked_interrupt(self, task_id, project=None):
        path = self._interrupt_path(task_id, project)
        lock_path = self._state_file(path.with_name(path.name + ".lock"), description="interrupt lock")
        with lock_path.open("a", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield path
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _request_interrupt(self, task_id, status, *, project=None):
        with self._locked_interrupt(task_id, project) as path:
            if path.exists() and self._read(path).get("status") == "cancelled" and status == "paused":
                return
            self._write(path, {"status": status, "requested_at": _now()})

    def _clear_interrupt(self, task_id, *, project=None):
        with self._locked_interrupt(task_id, project) as path:
            path.unlink(missing_ok=True)

    def _interrupt_status(self, task_id, *, project=None):
        with self._locked_interrupt(task_id, project) as path:
            if not path.exists():
                return None
            value = self._read(path)
            status = value.get("status")
            return status if status in {"paused", "cancelled"} else None

    def _remaining(self, task):
        return [(action_id, action) for action_id, action in task["actions"].items() if action["status"] != "complete"]

    @staticmethod
    def _operation_allowed(task, operation):
        return operation not in DEFAULT_PROHIBITED and operation in task["authorization"]["permitted_operations"]

    @staticmethod
    def _authorization_message(operation):
        return f"{operation} requires explicit owner authorization in a new authorization revision."

    @contextmanager
    def _locked_decision(self, task_id, project=None):
        # Project imports must finish applying to every task before automatic
        # decisions resume. All callers acquire project -> task -> interrupt.
        task_path = self._task_path(task_id, project)
        registered = self._read(task_path.parents[1] / "registration.json")["project"]
        with self._locked_project(registered):
            registration = self._read(self._registration(registered))
            if registration.get("owner_override", {}).get("import_complete") is False:
                raise ValueError("Owner override import is incomplete; retry the host import before automatic decisions")
            with self._locked_task(task_id, project=registered) as task, self._locked_interrupt(task_id, registered) as path:
                yield task, path

    def claim_action(self, task_id, action_id, attempt_id, *, project=None):
        with self._locked_decision(task_id, project) as (task, path):
            self._apply_pending_interrupt(task, path)
            if task["status"] != "active":
                return Decision(task["status"])
            action = task["actions"].get(action_id)
            if not action:
                raise ValueError(f"Unknown action: {action_id}")
            operation = action.get("operation")
            if not self._operation_allowed(task, operation):
                self._event(task_id, "authorization_blocked", project=task["project"], operation=operation,
                            action_id=action_id)
                return Decision("blocker", message=self._authorization_message(operation))
            if action["status"] == "complete":
                return Decision("complete")
            if action["status"] == "leased":
                return Decision("reconcile", message="A prior side-effect dispatch requires reconciliation.")
            action["status"] = "leased"
            action["attempts"].append({"attempt_id": attempt_id, "at": _now(), "decision": "dispatch"})
            self._save_task(task)
            self._event(task_id, "action_dispatched", project=task["project"], action_id=action_id, attempt_id=attempt_id)
            return Decision("dispatch")

    def reconcile_action(self, task_id, action_id, *, succeeded, receipt, project=None):
        with self._locked_task(task_id, project=project) as task:
            if task["status"] in {"complete", "blocked", "cancelled"}:
                raise ValueError("Terminal task actions cannot be reconciled")
            action = task["actions"].get(action_id)
            if not action or action["status"] != "leased":
                raise ValueError("Only a leased action can be reconciled")
            if succeeded:
                action.update(status="complete", evidence=receipt)
                event = "action_reconciled"
            else:
                action["status"] = "pending"
                event = "retry_queued"
            self._save_task(task)
            self._event(task_id, event, project=task["project"], action_id=action_id, receipt=receipt)

    @staticmethod
    def _completion_replay(task, action, evidence):
        if action["status"] == "complete":
            if action["evidence"] == evidence:
                return True
            raise ValueError("Completed action evidence cannot be changed")
        if task["status"] in {"complete", "blocked", "cancelled"}:
            raise ValueError("Terminal task actions cannot be completed")
        return False

    def complete_action(self, task_id, action_id, *, evidence, project=None):
        with self._locked_task(task_id, project=project) as task:
            action = task["actions"].get(action_id)
            if not action:
                raise ValueError(f"Unknown action: {action_id}")
            if self._completion_replay(task, action, evidence):
                return
            action.update(status="complete", evidence=evidence)
            self._save_task(task)
            self._event(task_id, "action_completed", project=task["project"], action_id=action_id, evidence=evidence)

    def join_child(self, task_id, action_id, child_task_id, *, evidence, project=None):
        with self._locked_task(task_id, project=project) as task:
            action = task["actions"].get(action_id)
            if not action or action.get("child_task_id") != child_task_id:
                raise ValueError("Child result is not bound to this parent action")
            joined = {"child_task_id": child_task_id, "evidence": evidence}
            if self._completion_replay(task, action, joined):
                return
            action.update(status="complete", evidence=joined)
            self._save_task(task)
            self._event(task_id, "action_completed", project=task["project"], action_id=action_id, evidence=action["evidence"])
            self._event(task_id, "child_evidence_joined", project=task["project"], action_id=action_id, child_task_id=child_task_id)

    def _validator_snapshot(self, command, *, project_descriptor=None):
        """Copy the opened executable into host state and return its immutable launch path."""
        descriptor = None
        snapshot_descriptor = None
        snapshot_path = None
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            if os.path.isabs(command):
                descriptor = os.open(command, flags)
            elif "/" in command:
                if project_descriptor is None:
                    return None, None
                descriptor = os.open(command, flags, dir_fd=project_descriptor)
            else:
                for directory in os.get_exec_path():
                    candidate = os.path.join(directory, command) if directory else command
                    try:
                        descriptor = os.open(
                            candidate, flags,
                            **({"dir_fd": project_descriptor}
                               if project_descriptor is not None and not os.path.isabs(candidate) else {}),
                        )
                        mode = os.fstat(descriptor).st_mode
                        if not stat.S_ISREG(mode) or not mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
                            os.close(descriptor)
                            descriptor = None
                            continue
                        break
                    except OSError:
                        continue
                if descriptor is None:
                    return None, None
            mode = os.fstat(descriptor).st_mode
            if not stat.S_ISREG(mode) or not mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
                return None, None
            snapshots = self._state_directory(self.root / "validator-snapshots", create=True,
                                              description="validator snapshot directory")
            snapshot_descriptor, snapshot_path = tempfile.mkstemp(prefix="validator-", dir=snapshots)
            os.fchmod(snapshot_descriptor, 0o700)
            digest = hashlib.sha256()
            copy_deadline = time.monotonic() + 5
            copied = 0
            while chunk := os.read(descriptor, 64 * 1024):
                copied += len(chunk)
                if copied > 64 * 1024 * 1024 or time.monotonic() >= copy_deadline:
                    raise ValueError("Host validator snapshot exceeds the 64 MiB or 5 second copy limit")
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    view = view[os.write(snapshot_descriptor, view):]
            os.fsync(snapshot_descriptor)
            os.fchmod(snapshot_descriptor, 0o500)
            os.close(snapshot_descriptor)
            snapshot_descriptor = None
            return snapshot_path, digest.hexdigest()
        except (OSError, ValueError):
            if snapshot_path is not None:
                os.unlink(snapshot_path)
            raise ValueError("Host validator executable snapshot could not be created")
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if snapshot_descriptor is not None:
                os.close(snapshot_descriptor)

    @staticmethod
    def _signal_validator(process, sig):
        try:
            os.killpg(process.pid, sig)
        except (ProcessLookupError, PermissionError):
            # Some platforms deny access to an empty group after its leader
            # exits. A surviving leader still needs a direct termination.
            if process.poll() is None:
                process.send_signal(sig)

    @staticmethod
    def _stream_validator_output(process, timeout):
        """Drain both pipes without retaining more than the receipt cap in host memory."""
        retained = bytearray()
        timed_out = False
        deadline = time.monotonic() + timeout
        drain_deadline = deadline + 2
        kill_deadline = None
        with selectors.DefaultSelector() as selector:
            for stream in (process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
            while selector.get_map() or process.poll() is None:
                now = time.monotonic()
                if not timed_out and now >= deadline:
                    timed_out = True
                    HostSupervisor._signal_validator(process, signal.SIGTERM)
                    kill_deadline = now + 1
                elif timed_out and kill_deadline is not None and now >= kill_deadline:
                    HostSupervisor._signal_validator(process, signal.SIGKILL)
                    kill_deadline = None
                if timed_out and now >= drain_deadline:
                    break
                wait_for = 0.05 if timed_out else max(0, min(0.05, deadline - now))
                for key, _ in selector.select(wait_for):
                    # One bounded read per event ensures noisy writers cannot
                    # starve timeout checks or the other output stream.
                    try:
                        chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    remaining = 8192 - len(retained)
                    if remaining > 0:
                        retained.extend(chunk[:remaining])
        process.stdout.close()
        process.stderr.close()
        process.wait(timeout=1)
        return _text_output(bytes(retained)), "timeout" if timed_out else process.returncode

    @staticmethod
    def _validate_validator_definition(validator):
        command = validator.get("command")
        if (not isinstance(command, list) or not command
                or not all(isinstance(part, str) for part in command) or not command[0]):
            raise ValueError("Host validator command must be a nonempty argument list")
        for part in command:
            if "\x00" in part:
                raise ValueError("Host validator arguments cannot contain NUL")
            try:
                part.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError("Host validator arguments must be valid UTF-8") from exc
        timeout = validator.get("timeout_s", 30)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0 or timeout > 300 or not math.isfinite(timeout):
            raise ValueError("Host validator timeout_s must be between 0 and 300")
        return command, timeout

    def _validator_receipt(self, validator, *, project=None, project_descriptor=None):
        command, timeout = self._validate_validator_definition(validator)
        declared_command = command
        snapshot_path, binary_digest = self._validator_snapshot(command[0], project_descriptor=project_descriptor)
        if snapshot_path is None or binary_digest is None:
            if snapshot_path is not None:
                os.unlink(snapshot_path)
            raise ValueError("Host validator requires an executable snapshot and digest before launch")
        launch_command = [snapshot_path, *command[1:]]
        run_options = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "cwd": project,
                       "start_new_session": True}
        if project_descriptor is not None:
            # A freshly started interpreter may safely fchdir before it execs the
            # validator.  Avoid preexec_fn: it executes in the forked child of
            # this potentially multithreaded supervisor and can deadlock there.
            launch_command = [sys.executable, "-c", _VALIDATOR_LAUNCHER, str(project_descriptor),
                              snapshot_path, *command[1:]]
            run_options.update(cwd=None, pass_fds=(project_descriptor,))
        try:
            process = subprocess.Popen(launch_command, **run_options)
            try:
                output, status = self._stream_validator_output(process, timeout)
            finally:
                try:
                    self._signal_validator(process, signal.SIGKILL)
                finally:
                    process.stdout.close()
                    process.stderr.close()
                    process.wait(timeout=1)
        finally:
            os.unlink(snapshot_path)
        return {"command": declared_command, "command_digest": _digest(declared_command), "binary_digest": binary_digest,
                "exit_status": status, "output": output, "digest": hashlib.sha256(output.encode()).hexdigest(),
                "timeout_s": timeout, "version": validator.get("version", "host-configured")}

    def _validate(self, task):
        # Recheck older saved definitions before running any validator, not
        # halfway through execution after a receipt has already been replaced.
        self._validate_validator_ids(task["validators"])
        for validator in task["validators"]:
            self._validate_validator_definition(validator)
        receipts = {}
        with self._pinned_registered_project(task["project"]) as project_descriptor:
            for validator in task["validators"]:
                validator_id = validator.get("id")
                receipt = self._validator_receipt(validator, project_descriptor=project_descriptor)
                receipts[validator_id] = receipt
                if receipt["exit_status"] != 0:
                    return False, receipts
        return True, receipts

    def gate_final(self, task_id, attempt_id, content, *, project=None):
        with self._locked_decision(task_id, project) as (task, interrupt_path):
            # All automatic decisions serialize with interrupt publication. No untrusted
            # code or long-running validators execute while these locks are held.
            interruption = self._read(interrupt_path)["status"] if interrupt_path.exists() else None
            self._event(task_id, "final_attempt", project=task["project"], attempt_id=attempt_id, content_digest=hashlib.sha256(content.encode()).hexdigest())
            if task["status"] in TERMINAL_STATUSES | {"paused"}:
                if task["status"] == "paused" and interruption == "cancelled":
                    task["status"] = "cancelled"
                    self._save_task(task)
                    self._event(task_id, "cancelled", project=task["project"], source="final_interrupt")
                    return Decision("cancelled", message="Automatic continuation is disabled until an explicit resume.")
                return Decision(task["status"], message="Automatic continuation is disabled until an explicit resume.")
            task["withheld_final"] = {"attempt_id": attempt_id, "content": content,
                                      "content_digest": hashlib.sha256(content.encode()).hexdigest(),
                                      "withheld_at": _now()}
            if interruption:
                task["status"] = interruption
                self._save_task(task)
                self._event(task_id, interruption, project=task["project"], source="final_interrupt")
                return Decision(interruption, message="Automatic continuation is disabled until an explicit resume.")
            remaining = self._remaining(task)
            authorized = [(action_id, action) for action_id, action in remaining
                          if self._operation_allowed(task, action.get("operation"))]
            if authorized:
                action_id, action = authorized[0]
                self._save_task(task)
                self._event(task_id, "continuation_queued", project=task["project"], action_id=action_id)
                return Decision("continue", next_action={"id": action_id, **{k: v for k, v in action.items() if k not in {"attempts", "evidence", "status"}}})
            if remaining:
                action_id, action = remaining[0]
                operation = action.get("operation")
                message = self._authorization_message(operation)
                self._event(task_id, "authorization_blocked", project=task["project"], operation=operation,
                            action_id=action_id)
                return self._commit_release(task, attempt_id, message, blocker=True, blocker_id=action_id)
            if task["blockers"]:
                blocker = task["blockers"][0]
                message = blocker.get("owner_action", "Owner authorization is required.")
                return self._commit_release(task, attempt_id, message, blocker=True)
            if task["validators"]:
                self._validate_validator_ids(task["validators"])
                for validator in task["validators"]:
                    self._validate_validator_definition(validator)
                message = ("Owner action required: connect an isolated container validator runner, "
                           "then retry validation. Local validator execution is disabled.")
                task["validation_blocker"] = {"reason": "isolated_runner_unavailable",
                                              "owner_action": message}
                self._save_task(task)
                self._event(task_id, "validation_unavailable", project=task["project"])
                return Decision("validation_unavailable", message=message)
            return self._commit_release(task, attempt_id, content)

    def visible_messages(self, task_id, *, project=None):
        return self.task(task_id, project=project)["visible_messages"]

    def recover(self, task_id, *, project=None):
        with self._locked_decision(task_id, project) as (task, path):
            self._apply_pending_interrupt(task, path)
            if task["status"] == "validating":
                attempt_id = task.pop("validation_attempt", None)
                task["status"] = "active"
                self._save_task(task)
                self._event(task_id, "validation_recovered", project=task["project"], attempt_id=attempt_id)
                return Decision("active", message="Abandoned validation was recovered; final delivery may be retried.")
            remaining = self._remaining(task)
            if task["status"] != "active" or not remaining:
                return Decision(task["status"])
            authorized = [(action_id, action) for action_id, action in remaining
                          if self._operation_allowed(task, action.get("operation"))]
            if not authorized:
                operation = remaining[0][1].get("operation")
                self._event(task_id, "authorization_blocked", project=task["project"], operation=operation)
                return Decision("blocker", message=self._authorization_message(operation))
            action_id, action = authorized[0]
            self._event(task_id, "recovered", project=task["project"], action_id=action_id)
            return Decision("continue", next_action={"id": action_id, **{k: v for k, v in action.items() if k not in {"attempts", "evidence", "status"}}})

    def pause(self, task_id, *, project=None):
        self._request_interrupt(task_id, "paused", project=project)
        with self._locked_task(task_id, project=project) as task:
            if task["status"] in TERMINAL_STATUSES:
                self._clear_interrupt(task_id, project=task["project"])
                self._event(task_id, "terminal_transition_ignored", project=task["project"], requested="pause")
                return
            if task["status"] == "paused":
                return
            task["status"] = "paused"
            self._save_task(task)
            self._event(task_id, "paused", project=task["project"])

    def resume(self, task_id, *, project=None):
        # Match gate_final's task -> interrupt order to avoid an ABBA deadlock.
        # The task lock also makes clearing the interrupt part of the same valid
        # paused-to-active transition.
        with self._locked_task(task_id, project=project) as task:
            with self._locked_interrupt(task_id, task["project"]) as interrupt_path:
                pending = self._read(interrupt_path).get("status") if interrupt_path.exists() else None
                if task["status"] != "paused":
                    if task["status"] in TERMINAL_STATUSES:
                        raise ValueError("A terminal task cannot resume")
                    if pending == "paused":
                        raise ValueError("A pause transition is pending")
                    raise ValueError("Only a paused task can resume")
                if pending == "cancelled":
                    raise ValueError("A cancellation is pending")
                interrupt_path.unlink(missing_ok=True)
                task["status"] = "active"
                self._save_task(task)
                self._event(task_id, "resumed", project=task["project"])

    def cancel(self, task_id, *, project=None):
        self._request_interrupt(task_id, "cancelled", project=project)
        with self._locked_task(task_id, project=project) as task:
            if task["status"] in TERMINAL_STATUSES:
                self._clear_interrupt(task_id, project=task["project"])
                self._event(task_id, "terminal_transition_ignored", project=task["project"], requested="cancel")
                return
            task["status"] = "cancelled"
            self._save_task(task)
            self._event(task_id, "cancelled", project=task["project"])

    def request_operation(self, task_id, operation, *, project=None):
        task = self.task(task_id, project=project)
        if operation not in DEFAULT_PROHIBITED and operation in task["authorization"]["permitted_operations"]:
            return Decision("permitted")
        message = f"{operation} requires explicit owner authorization in a new authorization revision."
        self._event(task_id, "authorization_blocked", project=task["project"], operation=operation)
        return Decision("blocker", message=message)

    def release_withheld_final(self, task_id, *, owner, reason, project=None):
        """Release exactly one stored candidate through a host-only owner action."""
        if not isinstance(owner, str) or not owner.strip() or not isinstance(reason, str) or not reason.strip():
            raise ValueError("Owner release requires a nonempty owner and reason")
        with self._locked_task(task_id, project=project) as task:
            withheld = task.get("withheld_final")
            if not isinstance(withheld, dict) or not isinstance(withheld.get("content"), str):
                raise ValueError("No withheld final is available for owner release")
            content = withheld["content"]
            return self._commit_release(
                task, withheld.get("attempt_id"), content, event_type="owner_override_final_released",
                release_details={"owner": owner.strip(), "reason": reason.strip()},
            )

    def _apply_pending_interrupt(self, task, path):
        """Caller holds task and interrupt locks, in that order."""
        if task["status"] not in TERMINAL_STATUSES and path.exists():
            status = self._read(path)["status"]
            if status not in {"paused", "cancelled"}:
                raise ValueError("Invalid host interrupt")
            task["status"] = status
            self._save_task(task)
            self._event(task["task_id"], status, project=task["project"], source="pending_interrupt")
