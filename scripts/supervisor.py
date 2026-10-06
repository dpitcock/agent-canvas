#!/usr/bin/env python3
"""Host-owned task state and final-delivery decisions for supervised Codex clients.

This module deliberately never reads workspace task files as completion evidence.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
import fcntl


DEFAULT_PROHIBITED = ("push", "publish", "pr_create", "merge", "destructive", "credential_change",
                      "config_change", "external_message")
TERMINAL_STATUSES = {"complete", "blocked", "cancelled"}
OWNER_OVERRIDE_MODES = {"pause", "bypass-review", "reset"}


class Decision:
    def __init__(self, kind, *, release=False, message="", next_action=None):
        self.kind, self.release, self.message, self.next_action = kind, release, message, next_action


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _now():
    return time.time_ns()


def _validate_task_id(task_id):
    if not isinstance(task_id, str) or not task_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in task_id):
        raise ValueError("task_id must contain only letters, digits, hyphen, and underscore")


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

    def _project_dir(self, project):
        return self.root / "projects" / self._project_key(project)

    def _registration(self, project):
        return self._project_dir(project) / "registration.json"

    def _project_event(self, project, event_type, **details):
        event = {"at": _now(), "type": event_type, "project": str(Path(project).resolve()), **details}
        path = self._project_dir(project) / "audit.jsonl"
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as out:
            out.write(json.dumps(event, sort_keys=True) + "\n")
            out.flush()
            os.fsync(out.fileno())
        return event

    def _task_path(self, task_id, project=None):
        _validate_task_id(task_id)
        if project is not None:
            path = self._project_dir(project) / "tasks" / f"{task_id}.json"
            if not path.is_file():
                raise ValueError(f"Unknown task: {task_id}")
            return path
        matches = list((self.root / "projects").glob(f"*/tasks/{task_id}.json")) if (self.root / "projects").exists() else []
        if len(matches) != 1:
            raise ValueError(f"Unknown or ambiguous task: {task_id}")
        return matches[0]

    def _interrupt_path(self, task_id, project=None):
        """Return host-owned cancellation state without taking the task lock."""
        path = self._task_path(task_id, project)
        return path.with_name(path.stem + ".interrupt.json")

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
            raise ValueError("Host state directory must be outside the supervised project workspace")
        try:
            project.relative_to(self.root)
        except ValueError:
            pass
        else:
            raise ValueError("Host state directory must be outside the supervised project workspace")
        registration = self._registration(project)
        if registration.exists():
            current = self._read(registration)
            if current["project"] != str(project):
                raise ValueError("Host registration project mismatch")
            return current
        value = {"schema_version": 1, "project": str(project), "created_at": _now()}
        self._write(registration, value)
        return value

    def import_owner_override(self, project, modes, *, source_digest):
        """Import a one-time, owner-provided workspace snapshot into host state."""
        project = Path(project).resolve()
        current = self.provision(project)
        if not isinstance(modes, (list, tuple, set)) or any(mode not in OWNER_OVERRIDE_MODES for mode in modes):
            raise ValueError("Unknown owner override mode")
        if not isinstance(source_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", source_digest):
            raise ValueError("Owner override snapshot requires a SHA-256 source digest")
        normalized = sorted(set(modes))
        current["owner_override"] = {"modes": normalized, "source_digest": source_digest, "imported_at": _now()}
        self._write(self._registration(project), current)
        self._project_event(project, "owner_override_imported", modes=normalized, source_digest=source_digest)
        for path in sorted((self._project_dir(project) / "tasks").glob("*.json")):
            task_id = path.stem
            task = self._read(path)
            if task["status"] in TERMINAL_STATUSES:
                continue
            if "reset" in normalized:
                self.cancel(task_id, project=project)
                self._event(task_id, "owner_override_reset", project=task["project"])
            elif "pause" in normalized and task["status"] == "active":
                self.pause(task_id, project=project)
                self._event(task_id, "owner_override_paused", project=task["project"])
        return current

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
        registration = self.provision(project)
        _validate_task_id(task_id)
        path = self._project_dir(project) / "tasks" / f"{task_id}.json"
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
        task = {"schema_version": 1, "task_id": task_id, "project": registration["project"],
                "status": "paused" if "pause" in registration.get("owner_override", {}).get("modes", []) else "active",
                "authorization": authorization, "actions": action_map, "validators": list(validators),
                "blockers": list(blockers), "evidence": {"validators": {}}, "visible_messages": []}
        self._write(path, task)
        self._event(task_id, "task_created", project=project, authorization_digest=authorization["digest"])
        if task["status"] == "paused":
            self._event(task_id, "owner_override_paused", project=project)
        return task

    def task(self, task_id, *, project=None):
        return self._read(self._task_path(task_id, project))

    def _save_task(self, task):
        self._write(self._task_path(task["task_id"], task["project"]), task)

    @contextmanager
    def _locked_task(self, task_id, project=None):
        """Serialize read-modify-write task decisions across supervisor processes."""
        path = self._task_path(task_id, project)
        lock_path = path.with_name(path.name + ".lock")
        with lock_path.open("a", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield self._read(path)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def _locked_interrupt(self, task_id, project=None):
        path = self._interrupt_path(task_id, project)
        lock_path = path.with_name(path.name + ".lock")
        with lock_path.open("a", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield path
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _request_interrupt(self, task_id, status, *, project=None):
        with self._locked_interrupt(task_id, project) as path:
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
        return operation in task["authorization"]["permitted_operations"]

    @staticmethod
    def _authorization_message(operation):
        return f"{operation} requires explicit owner authorization in a new authorization revision."

    def claim_action(self, task_id, action_id, attempt_id, *, project=None):
        with self._locked_task(task_id, project) as task:
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
        with self._locked_task(task_id, project) as task:
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
        with self._locked_task(task_id, project) as task:
            action = task["actions"].get(action_id)
            if not action:
                raise ValueError(f"Unknown action: {action_id}")
            action.update(status="complete", evidence=evidence)
            self._save_task(task)
            self._event(task_id, "action_completed", project=task["project"], action_id=action_id, evidence=evidence)

    def join_child(self, task_id, action_id, child_task_id, *, evidence, project=None):
        with self._locked_task(task_id, project) as task:
            action = task["actions"].get(action_id)
            if not action or action.get("child_task_id") != child_task_id:
                raise ValueError("Child result is not bound to this parent action")
            action.update(status="complete", evidence={"child_task_id": child_task_id, "evidence": evidence})
            self._save_task(task)
            self._event(task_id, "action_completed", project=task["project"], action_id=action_id, evidence=action["evidence"])
            self._event(task_id, "child_evidence_joined", project=task["project"], action_id=action_id, child_task_id=child_task_id)

    @staticmethod
    def _validator_binary(command, project):
        candidate = Path(command[0])
        if candidate.is_absolute():
            return candidate.resolve()
        if "/" in command[0]:
            return (Path(project) / candidate).resolve()
        executable = shutil.which(command[0])
        return Path(executable).resolve() if executable else candidate

    def _prepared_validator_command(self, command, project):
        """Bind project-relative executables before a workspace process can replace them."""
        binary = self._validator_binary(command, project)
        binary_bytes = binary.read_bytes() if binary.is_file() else None
        binary_digest = hashlib.sha256(binary_bytes).hexdigest() if binary_bytes is not None else None
        if binary_bytes is None or "/" not in command[0] or Path(command[0]).is_absolute():
            return command, binary_digest, None
        descriptor, copy_name = tempfile.mkstemp(prefix="validator-", dir=self.root)
        copy_path = Path(copy_name)
        try:
            with os.fdopen(descriptor, "wb") as out:
                out.write(binary_bytes)
                out.flush()
                os.fsync(out.fileno())
            copy_path.chmod(binary.stat().st_mode & 0o777)
        except BaseException:
            copy_path.unlink(missing_ok=True)
            raise
        return [str(copy_path), *command[1:]], binary_digest, copy_path

    def _validator_receipt(self, validator, project):
        command = validator.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
            raise ValueError("Host validator command must be a nonempty argument list")
        timeout = validator.get("timeout_s", 30)
        if not isinstance(timeout, (int, float)) or timeout <= 0 or timeout > 300:
            raise ValueError("Host validator timeout_s must be between 0 and 300")
        run_command, binary_digest, protected_copy = self._prepared_validator_command(command, project)
        try:
            with tempfile.TemporaryFile(mode="w+b") as stdout, tempfile.TemporaryFile(mode="w+b") as stderr:
                try:
                    run = subprocess.run(run_command, stdout=stdout, stderr=stderr, timeout=timeout,
                                         check=False, cwd=project)
                    status = run.returncode
                except subprocess.TimeoutExpired:
                    status = "timeout"
                stdout.seek(0)
                output_bytes = stdout.read(8192)
                if len(output_bytes) < 8192:
                    stderr.seek(0)
                    output_bytes += stderr.read(8192 - len(output_bytes))
                output = output_bytes.decode("utf-8", errors="replace")
        finally:
            if protected_copy is not None:
                protected_copy.unlink(missing_ok=True)
        return {"command": command, "command_digest": _digest(command), "binary_digest": binary_digest,
                "exit_status": status, "output": output, "digest": hashlib.sha256(output.encode()).hexdigest(),
                "timeout_s": timeout, "version": validator.get("version", "host-configured")}

    def _validate(self, task):
        for validator in task["validators"]:
            validator_id = validator.get("id")
            if not validator_id:
                raise ValueError("Host validators require an id")
            receipt = self._validator_receipt(validator, task["project"])
            task["evidence"]["validators"][validator_id] = receipt
            self._event(task["task_id"], "validator_received", project=task["project"], validator_id=validator_id, receipt=receipt)
            if receipt["exit_status"] != 0:
                self._save_task(task)
                return validator_id, receipt
        return None

    def gate_final(self, task_id, attempt_id, content, *, project=None):
        with self._locked_task(task_id, project) as task:
            self._event(task_id, "final_attempt", project=task["project"], attempt_id=attempt_id, content_digest=hashlib.sha256(content.encode()).hexdigest())
            if task["status"] in TERMINAL_STATUSES | {"paused"}:
                return Decision(task["status"], message="Automatic continuation is disabled until an explicit resume.")
            task["withheld_final"] = {"attempt_id": attempt_id, "content": content,
                                      "content_digest": hashlib.sha256(content.encode()).hexdigest(),
                                      "withheld_at": _now()}
            self._save_task(task)
            interruption = self._interrupt_status(task_id, project=task["project"])
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
                self._event(task_id, "continuation_queued", project=task["project"], action_id=action_id)
                return Decision("continue", next_action={"id": action_id, **{k: v for k, v in action.items() if k not in {"attempts", "evidence", "status"}}})
            if remaining:
                action_id, action = remaining[0]
                operation = action.get("operation")
                message = self._authorization_message(operation)
                task["visible_messages"].append(message)
                task["status"] = "blocked"
                self._save_task(task)
                self._event(task_id, "authorization_blocked", project=task["project"], operation=operation,
                            action_id=action_id)
                self._event(task_id, "blocker_delivered", project=task["project"], blocker_id=action_id)
                return Decision("blocker", release=True, message=message)
            if task["blockers"]:
                blocker = task["blockers"][0]
                message = blocker.get("owner_action", "Owner authorization is required.")
                task["visible_messages"].append(message)
                task["status"] = "blocked"
                self._save_task(task)
                self._event(task_id, "blocker_delivered", project=task["project"], blocker_id=blocker.get("id"))
                return Decision("blocker", release=True, message=message)
            validator_failure = self._validate(task)
            interruption = self._interrupt_status(task_id, project=task["project"])
            if interruption:
                task["status"] = interruption
                self._save_task(task)
                self._event(task_id, interruption, project=task["project"], source="validator_interrupt")
                return Decision(interruption, message="Automatic continuation is disabled until an explicit resume.")
            if validator_failure:
                validator_id, receipt = validator_failure
                remediation = {"id": f"validator:{validator_id}", "kind": "validator_remediation",
                               "validator_id": validator_id, "exit_status": receipt["exit_status"],
                               "output": receipt["output"], "digest": receipt["digest"]}
                self._event(task_id, "continuation_queued", project=task["project"], reason="validator_failed",
                            next_action=remediation)
                return Decision("continue", message=f"Host validator {validator_id} failed; repair it and retry.",
                                next_action=remediation)
            task["visible_messages"].append(content)
            task["status"] = "complete"
            task.pop("withheld_final", None)
            self._save_task(task)
            self._event(task_id, "final_released", project=task["project"], attempt_id=attempt_id)
            return Decision("complete", release=True, message=content)

    def release_withheld_final(self, task_id, *, owner, reason, project=None):
        """Release exactly one stored candidate through a host-only owner action."""
        if not isinstance(owner, str) or not owner.strip() or not isinstance(reason, str) or not reason.strip():
            raise ValueError("Owner release requires a nonempty owner and reason")
        with self._locked_task(task_id, project) as task:
            withheld = task.get("withheld_final")
            if not isinstance(withheld, dict) or not isinstance(withheld.get("content"), str):
                raise ValueError("No withheld final is available for owner release")
            content = withheld["content"]
            task["visible_messages"].append(content)
            task["status"] = "complete"
            task.pop("withheld_final", None)
            self._save_task(task)
            self._event(task_id, "owner_override_final_released", project=task["project"],
                        attempt_id=withheld.get("attempt_id"), content_digest=withheld.get("content_digest"),
                        owner=owner.strip(), reason=reason.strip())
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
        self._request_interrupt(task_id, "paused", project=project)
        with self._locked_task(task_id, project) as task:
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
        with self._locked_task(task_id, project) as task:
            with self._locked_interrupt(task_id, task["project"]) as interrupt_path:
                pending = self._read(interrupt_path).get("status") if interrupt_path.exists() else None
                if task["status"] != "paused":
                    if task["status"] in TERMINAL_STATUSES:
                        raise ValueError("A terminal task cannot resume")
                    if pending == "paused":
                        raise ValueError("A pause transition is pending")
                    raise ValueError("Only a paused task can resume")
                interrupt_path.unlink(missing_ok=True)
                task["status"] = "active"
                self._save_task(task)
                self._event(task_id, "resumed", project=task["project"])

    def cancel(self, task_id, *, project=None):
        self._request_interrupt(task_id, "cancelled", project=project)
        with self._locked_task(task_id, project) as task:
            if task["status"] in TERMINAL_STATUSES:
                self._clear_interrupt(task_id, project=task["project"])
                self._event(task_id, "terminal_transition_ignored", project=task["project"], requested="cancel")
                return
            task["status"] = "cancelled"
            self._save_task(task)
            self._event(task_id, "cancelled", project=task["project"])

    def request_operation(self, task_id, operation, *, project=None):
        task = self.task(task_id, project=project)
        if self._operation_allowed(task, operation):
            return Decision("permitted")
        message = self._authorization_message(operation)
        self._event(task_id, "authorization_blocked", project=task["project"], operation=operation)
        return Decision("blocker", message=message)
