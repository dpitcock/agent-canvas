#!/usr/bin/env python3
"""Custom-client rendering boundary for Codex App Server notifications.

Callers render the dictionaries returned by ``consume``. Agent-message deltas are
never returned until HostSupervisor.gate_final releases them after turn/completed.
"""
import uuid


class SupervisedRenderer:
    def __init__(self, supervisor, task_id):
        self.supervisor = supervisor
        self.task_id = task_id
        self._buffer = []

    def consume(self, event):
        method = event.get("method")
        if method == "item/agentMessage/delta":
            self._buffer.append(event.get("params", {}).get("delta", ""))
            return []
        if method != "turn/completed":
            return [{"kind": "progress", "event": event}]
        decision = self.supervisor.gate_final(self.task_id, str(uuid.uuid4()), "".join(self._buffer))
        self._buffer = []
        if decision.release:
            return [{"kind": "final", "content": decision.message, "decision": decision.kind}]
        if decision.kind == "continue":
            return [{"kind": "continuation", "next_action": decision.next_action, "message": decision.message}]
        return [{"kind": decision.kind, "message": decision.message}]
