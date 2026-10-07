#!/usr/bin/env python3
"""Host-owned task state and final-delivery decisions for supervised Codex clients.

This module deliberately never reads workspace task files as completion evidence.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager

import fcntl


DEFAULT_PROHIBITED = ("push", "publish", "pr_create", "merge", "destructive", "credential_change",
                      "config_change", "external_message")
TERMINAL_STATUSES = frozenset(("complete", "blocked", "cancelled"))


class Decision:
    def __init__(self, kind, *, release=False, message="", next_action=None):
        self.kind, self.release, self.message, self.next_action = kind, release, message, next_action


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _now():
    return time.time_ns()


def _text_output(value):
    return value.decode(errors="replace") if isinstance(value, bytes) else (value or "")


class HostSupervisor:
    """Durable state store whose root must be outside the supervised workspace."""

    def __init__(self, state_dir):
        self.root = Path(state_dir).expanduser().resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self.root.chmod(0o700)
        except OSError:
            pass

    def _project_key(self, project):
        return hashlib.sha256(str(Path(project).resolve()).encode()).hexdigest()

    @staticmethod
    def _validate_task_id(task_id):
        if not isinstance(task_id, str) or not task_id or any(
                character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
                for character in task_id):
            raise ValueError("task_id must contain only letters, digits, hyphen, and underscore")

    @staticmethod
    def _project_identity(project):
        status = Path(project).stat()
        return {"device": status.st_dev, "inode": status.st_ino}

    def _project_dir(self, project):
        return self.root / "projects" / self._project_key(project)

    def _registration(self, project):
        return self._project_dir(project) / "registration.json"

    def _registered_project(self, project):
        """Return a registered project only while its original directory still exists."""
        project = Path(project).resolve()
        if not project.is_dir():
            raise ValueError("Supervised project must be an existing directory")
        registration = self._registration(project)
        if not registration.is_file():
            raise ValueError("Supervised project is not registered")
        current = self._read(registration)
        if current.get("project") != str(project):
            raise ValueError("Host registration project mismatch")
        if current.get("identity") != self._project_identity(project):
            raise ValueError("Host registration project directory identity mismatch")
        return project

    def _task_path(self, task_id, project=None):
        self._validate_task_id(task_id)
        if project is not None:
            project = self._registered_project(project)
            path = self._project_dir(project) / "tasks" / f"{task_id}.json"
            if not path.is_file():
                raise ValueError(f"Unknown task: {task_id}")
            return path
        matches = list((self.root / "projects").glob(f"*/tasks/{task_id}.json")) if (self.root / "projects").exists() else []
        if len(matches) != 1:
            raise ValueError(f"Unknown or ambiguous task: {task_id}")
        registration = self._read(matches[0].parents[1] / "registration.json")
        self._registered_project(registration.get("project"))
        return matches[0]

    def _task_lock_path(self, task_id, project=None):
        self._validate_task_id(task_id)
        if project is not None:
            project = self._registered_project(project)
            return self._project_dir(project) / "tasks" / f"{task_id}.lock"
        return self._task_path(task_id).with_suffix(".lock")

    @contextmanager
    def _locked_task(self, task_id, *, project=None):
        """Serialize one task's durable read-modify-write transitions."""
        lock_path = self._task_lock_path(task_id, project)
        lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with lock_path.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                path = self._task_path(task_id, project) if project is None else self._project_dir(project) / "tasks" / f"{task_id}.json"
                yield self._read(path) if path.exists() else None
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @staticmethod
    def _write(path, value):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        data = json.dumps(value, indent=2, sort_keys=True) + "\n"
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
            temp = Path(out.name)
        try:
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)

    @staticmethod
    def _read(path):
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"Host state is unreadable: {path}") from error

    def provision(self, project):
        project = Path(project).resolve()
        if not project.is_dir():
            raise ValueError("Supervised project must be an existing directory")
        try:
            self.root.relative_to(project)
        except ValueError:
            pass
        else:
            raise ValueError("Supervisor state directory must be outside the project workspace")
        try:
            project.relative_to(self.root)
        except ValueError:
            pass
        else:
            raise ValueError("Supervisor state directory must be outside the project workspace")
        identity = self._project_identity(project)
        registration = self._registration(project)
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

    def _project_for_task(self, task_id, project=None):
        return self._task_path(task_id, project).parents[1]

    def _event_path(self, task_id, project=None):
        return self._project_for_task(task_id, project) / "audit.jsonl"

    def _event(self, task_id, event_type, *, project=None, **details):
        event = {"at": _now(), "type": event_type, "task_id": task_id, **details}
        path = self._event_path(task_id, project)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as out:
            out.write(json.dumps(event, sort_keys=True) + "\n")
            out.flush()
            os.fsync(out.fileno())
        return event

    def audit(self, task_id, *, project=None):
        path = self._event_path(task_id, project)
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def create_task(self, task_id, project, actions, *, validators=(), blockers=(), permitted_operations=("read", "write", "delegate")):
        self._validate_task_id(task_id)
        registration = self.provision(project)
        path = self._project_dir(project) / "tasks" / f"{task_id}.json"
        with self._locked_task(task_id, project=project):
            if path.exists():
                return self._read(path)
            action_map = {}
            for action in actions:
                action = dict(action)
                action_id = action.pop("id", None)
                if not action_id or action_id in action_map:
                    raise ValueError("actions require unique ids")
                action_map[action_id] = {**action, "status": "pending", "attempts": [], "evidence": None}
            authorization = {"revision": 1, "permitted_operations": sorted(set(permitted_operations)),
                             "prohibited_operations": list(DEFAULT_PROHIBITED)}
            authorization["digest"] = _digest(authorization)
            task = {"schema_version": 1, "task_id": task_id, "project": registration["project"], "status": "active",
                    "authorization": authorization, "actions": action_map, "validators": list(validators),
                    "blockers": list(blockers), "evidence": {"validators": {}}, "visible_messages": []}
            self._write(path, task)
            self._event(task_id, "task_created", project=project, authorization_digest=authorization["digest"])
            return task

    def task(self, task_id, *, project=None):
        return self._read(self._task_path(task_id, project))

    def _save_task(self, task):
        self._write(self._task_path(task["task_id"], task["project"]), task)

    def _remaining(self, task):
        return [(action_id, action) for action_id, action in task["actions"].items() if action["status"] != "complete"]

    def claim_action(self, task_id, action_id, attempt_id, *, project=None):
        with self._locked_task(task_id, project=project) as task:
            if task["status"] != "active":
                return Decision(task["status"])
            action = task["actions"].get(action_id)
            if not action:
                raise ValueError(f"Unknown action: {action_id}")
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

    def complete_action(self, task_id, action_id, *, evidence, project=None):
        with self._locked_task(task_id, project=project) as task:
            action = task["actions"].get(action_id)
            if not action:
                raise ValueError(f"Unknown action: {action_id}")
            action.update(status="complete", evidence=evidence)
            self._save_task(task)
            self._event(task_id, "action_completed", project=task["project"], action_id=action_id, evidence=evidence)

    def join_child(self, task_id, action_id, child_task_id, *, evidence, project=None):
        with self._locked_task(task_id, project=project) as task:
            action = task["actions"].get(action_id)
            if not action or action.get("child_task_id") != child_task_id:
                raise ValueError("Child result is not bound to this parent action")
            action.update(status="complete", evidence={"child_task_id": child_task_id, "evidence": evidence})
            self._save_task(task)
            self._event(task_id, "action_completed", project=task["project"], action_id=action_id, evidence=action["evidence"])
            self._event(task_id, "child_evidence_joined", project=task["project"], action_id=action_id, child_task_id=child_task_id)

    def _validator_receipt(self, validator):
        command = validator.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
            raise ValueError("Host validator command must be a nonempty argument list")
        timeout = validator.get("timeout_s", 30)
        if not isinstance(timeout, (int, float)) or timeout <= 0 or timeout > 300:
            raise ValueError("Host validator timeout_s must be between 0 and 300")
        try:
            run = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
            output = (_text_output(run.stdout) + _text_output(run.stderr))[:8192]
            status = run.returncode
        except subprocess.TimeoutExpired as error:
            output, status = _text_output(error.stdout)[:8192], "timeout"
        executable = shutil.which(command[0]) or command[0]
        binary = Path(executable)
        binary_digest = hashlib.sha256(binary.read_bytes()).hexdigest() if binary.is_file() else None
        return {"command": command, "command_digest": _digest(command), "binary_digest": binary_digest,
                "exit_status": status, "output": output, "digest": hashlib.sha256(output.encode()).hexdigest(),
                "timeout_s": timeout, "version": validator.get("version", "host-configured")}

    def _validate(self, task):
        receipts = {}
        for validator in task["validators"]:
            validator_id = validator.get("id")
            if not validator_id:
                raise ValueError("Host validators require an id")
            receipt = self._validator_receipt(validator)
            receipts[validator_id] = receipt
            if receipt["exit_status"] != 0:
                return False, receipts
        return True, receipts

    def gate_final(self, task_id, attempt_id, content, *, project=None):
        with self._locked_task(task_id, project=project) as task:
            self._event(task_id, "final_attempt", project=task["project"], attempt_id=attempt_id, content_digest=hashlib.sha256(content.encode()).hexdigest())
            if task["status"] in {"complete", "blocked"}:
                return Decision(task["status"])
            if task["status"] in {"paused", "cancelled"}:
                return Decision(task["status"], message="Automatic continuation is disabled until an explicit resume.")
            if task["status"] == "validating":
                return Decision("validating", message="Host validation is already in progress.")
            remaining = self._remaining(task)
            if remaining:
                action_id, action = remaining[0]
                self._event(task_id, "continuation_queued", project=task["project"], action_id=action_id)
                return Decision("continue", next_action={"id": action_id, **{k: v for k, v in action.items() if k not in {"attempts", "evidence", "status"}}})
            if task["blockers"]:
                blocker = task["blockers"][0]
                message = blocker.get("owner_action", "Owner authorization is required.")
                task["visible_messages"].append(message)
                task["status"] = "blocked"
                self._save_task(task)
                self._event(task_id, "blocker_delivered", project=task["project"], blocker_id=blocker.get("id"))
                return Decision("blocker", release=True, message=message)
            if not task["validators"]:
                task["visible_messages"].append(content)
                task["status"] = "complete"
                self._save_task(task)
                self._event(task_id, "final_released", project=task["project"], attempt_id=attempt_id)
                return Decision("complete", release=True, message=content)
            task["status"] = "validating"
            task["validation_attempt"] = attempt_id
            self._save_task(task)
            self._event(task_id, "validation_started", project=task["project"], attempt_id=attempt_id)

        try:
            valid, receipts = self._validate(task)
        except Exception:
            with self._locked_task(task_id, project=project) as current:
                if current["status"] == "validating" and current.get("validation_attempt") == attempt_id:
                    current["status"] = "active"
                    current.pop("validation_attempt", None)
                    self._save_task(current)
                    self._event(task_id, "validation_aborted", project=current["project"], attempt_id=attempt_id)
            raise

        with self._locked_task(task_id, project=project) as current:
            if current["status"] != "validating" or current.get("validation_attempt") != attempt_id:
                return Decision(current["status"], message="Automatic continuation is disabled until an explicit resume.")
            current["evidence"]["validators"].update(receipts)
            for validator_id, receipt in receipts.items():
                self._event(task_id, "validator_received", project=current["project"], validator_id=validator_id, receipt=receipt)
            current.pop("validation_attempt", None)
            if not valid:
                current["status"] = "active"
                self._save_task(current)
                self._event(task_id, "continuation_queued", project=current["project"], reason="validator_failed")
                return Decision("continue", message="Host validator failed; repair the reported action.")
            current["visible_messages"].append(content)
            current["status"] = "complete"
            self._save_task(current)
            self._event(task_id, "final_released", project=current["project"], attempt_id=attempt_id)
            return Decision("complete", release=True, message=content)

    def visible_messages(self, task_id, *, project=None):
        return self.task(task_id, project=project)["visible_messages"]

    def recover(self, task_id, *, project=None):
        task = self.task(task_id, project=project)
        remaining = self._remaining(task)
        if task["status"] != "active" or not remaining:
            return Decision(task["status"])
        action_id, action = remaining[0]
        self._event(task_id, "recovered", project=task["project"], action_id=action_id)
        return Decision("continue", next_action={"id": action_id, **{k: v for k, v in action.items() if k not in {"attempts", "evidence", "status"}}})

    def pause(self, task_id, *, project=None):
        with self._locked_task(task_id, project=project) as task:
            if task["status"] in TERMINAL_STATUSES:
                return
            task["status"] = "paused"
            self._save_task(task)
            self._event(task_id, "paused", project=task["project"])

    def resume(self, task_id, *, project=None):
        with self._locked_task(task_id, project=project) as task:
            if task["status"] != "paused":
                raise ValueError("Only a paused task can resume")
            task["status"] = "active"
            self._save_task(task)
            self._event(task_id, "resumed", project=task["project"])

    def cancel(self, task_id, *, project=None):
        with self._locked_task(task_id, project=project) as task:
            if task["status"] in TERMINAL_STATUSES:
                return
            task["status"] = "cancelled"
            self._save_task(task)
            self._event(task_id, "cancelled", project=task["project"])

    def request_operation(self, task_id, operation, *, project=None):
        task = self.task(task_id, project=project)
        if operation in task["authorization"]["permitted_operations"]:
            return Decision("permitted")
        message = f"{operation} requires explicit owner authorization in a new authorization revision."
        self._event(task_id, "authorization_blocked", project=task["project"], operation=operation)
        return Decision("blocker", message=message)
