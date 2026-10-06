#!/usr/bin/env python3
"""Custom-client rendering boundary for Codex App Server notifications.

Callers render the dictionaries returned by ``consume``. Agent-message deltas are
never returned until HostSupervisor.gate_final releases them after turn/completed.
"""
import uuid


class SupervisedRenderer:
    def __init__(self, supervisor, task_id, *, project=None):
        self.supervisor = supervisor
        self.task_id = task_id
        self.project = project
        self._buffer = []

    def consume(self, event):
        method = event.get("method")
        if method == "item/agentMessage/delta":
            self._buffer.append(event.get("params", {}).get("delta", ""))
            return []
        if self._is_agent_message_completion(event):
            return []
        if method != "turn/completed":
            return [{"kind": "progress", "event": event}]
        if self._turn_did_not_complete(event):
            self._buffer = []
            return [{"kind": "progress", "event": event}]
        decision = self.supervisor.gate_final(self.task_id, str(uuid.uuid4()), "".join(self._buffer), project=self.project)
        self._buffer = []
        if decision.release:
            return [{"kind": "final", "content": decision.message, "decision": decision.kind}]
        if decision.kind == "continue":
            return [{"kind": "continuation", "next_action": decision.next_action, "message": decision.message}]
        return [{"kind": decision.kind, "message": decision.message}]

    @staticmethod
    def _is_agent_message_completion(event):
        """Identify non-delta notifications that contain a completed agent message."""
        method = event.get("method", "")
        if method.startswith("item/agentMessage/"):
            return True
        if method != "item/completed":
            return False
        item = event.get("params", {}).get("item", {})
        return item.get("type") in {"agentMessage", "agent_message"}

    @staticmethod
    def _turn_did_not_complete(event):
        """Failed and interrupted turns do not have a candidate final to gate."""
        params = event.get("params", {})
        status = params.get("status")
        if status is None and isinstance(params.get("turn"), dict):
            status = params["turn"].get("status")
        return str(status).lower() in {"failed", "interrupted"}
