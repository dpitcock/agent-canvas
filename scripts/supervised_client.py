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
        self._message_buffers = {}
        self._completed_message_ids = set()
        self._final_message = ""

    def consume(self, event):
        if not isinstance(event, dict):
            return [{"kind": "progress", "event": event}]
        method = event.get("method")
        if method == "item/agentMessage/delta":
            self._buffer_agent_message_delta(event)
            return []
        if self._complete_agent_message(event):
            return []
        if method != "turn/completed":
            return [{"kind": "progress", "event": event}]
        if self._turn_did_not_complete(event):
            self._reset_messages()
            return [{"kind": "progress", "event": event}]
        decision = self.supervisor.gate_final(self.task_id, str(uuid.uuid4()), self._final_message, project=self.project)
        self._reset_messages()
        if decision.release:
            return [{"kind": "final", "content": decision.message, "decision": decision.kind}]
        if decision.kind == "continue":
            return [{"kind": "continuation", "next_action": decision.next_action, "message": decision.message}]
        return [{"kind": decision.kind, "message": decision.message}]

    def _buffer_agent_message_delta(self, event):
        """Keep text private until the matching item enters its completed phase."""
        params = event.get("params")
        if not isinstance(params, dict):
            return
        item_id = params.get("itemId")
        delta = params.get("delta")
        if not isinstance(item_id, str) or not item_id or not isinstance(delta, str):
            return
        if item_id in self._completed_message_ids:
            return
        self._message_buffers.setdefault(item_id, []).append(delta)

    def _complete_agent_message(self, event):
        """Promote only a delta buffer whose ID matches a completed agent message."""
        method = event.get("method")
        if isinstance(method, str) and method.startswith("item/agentMessage/"):
            return True
        if method != "item/completed":
            return False
        params = event.get("params")
        if not isinstance(params, dict):
            return False
        item = params.get("item")
        if not isinstance(item, dict) or item.get("type") not in {"agentMessage", "agent_message"}:
            return False
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            return True
        if item_id in self._completed_message_ids:
            return True
        self._completed_message_ids.add(item_id)
        buffer = self._message_buffers.pop(item_id, None)
        if buffer is not None:
            self._final_message = "".join(buffer)
        return True

    def _reset_messages(self):
        self._message_buffers.clear()
        self._completed_message_ids.clear()
        self._final_message = ""

    @staticmethod
    def _turn_did_not_complete(event):
        """Failed and interrupted turns do not have a candidate final to gate."""
        params = event.get("params")
        if not isinstance(params, dict):
            return False
        status = params.get("status")
        if status is None and isinstance(params.get("turn"), dict):
            status = params["turn"].get("status")
        return str(status).lower() in {"failed", "interrupted"}
