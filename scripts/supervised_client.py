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
        self._deltas = []
        self._completed_messages = []

    def consume(self, event):
        method = event.get("method")
        if method == "item/agentMessage/delta":
            self._deltas.append(event.get("params", {}).get("delta", ""))
            return []
        if method == "item/completed":
            item = event.get("params", {}).get("item", {})
            if item.get("type") == "agentMessage":
                # Prefer the protocol's terminal item. Legacy servers omit phase,
                # so retain the last completed message as a compatibility fallback.
                phase = item.get("phase")
                if phase == "final_answer":
                    self._completed_messages = [item.get("text", "")]
                elif phase is None:
                    self._completed_messages.append(item.get("text", ""))
                return []
        if method != "turn/completed":
            return [{"kind": "progress", "event": event}]
        status = event.get("params", {}).get("turn", {}).get("status")
        if status != "completed":
            self._deltas = []
            self._completed_messages = []
            return [{"kind": "turn_incomplete", "status": status,
                     "message": "Turn did not complete; host state remains recoverable."}]
        content = "".join(self._completed_messages or self._deltas)
        decision = self.supervisor.gate_final(self.task_id, str(uuid.uuid4()), content, project=self.project)
        self._deltas = []
        self._completed_messages = []
        if decision.release:
            return [{"kind": "final", "content": decision.message, "decision": decision.kind}]
        if decision.kind == "continue":
            return [{"kind": "continuation", "next_action": decision.next_action, "message": decision.message}]
        return [{"kind": decision.kind, "message": decision.message}]
