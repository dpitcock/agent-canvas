#!/usr/bin/env python3
"""Custom-client rendering boundary for Codex App Server notifications.

Callers render the dictionaries returned by ``consume``. Agent-message deltas are
never returned until HostSupervisor.gate_final releases them after turn/completed.
"""
import threading
import uuid


class SupervisedRenderer:
    def __init__(self, supervisor, task_id, *, project=None):
        self.supervisor = supervisor
        self.task_id = task_id
        self.project = project
        self._message_buffers = {}
        self._completed_message_ids = set()
        self._completed_messages = []
        self._completed_turn_ids = set()
        self._completed_turn_lock = threading.Lock()

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
        turn_id = self._turn_completion_id(event)
        if turn_id is None:
            self._reset_messages()
            return [{"kind": "progress", "event": self._sanitized_turn_completion(event)}]
        with self._completed_turn_lock:
            if turn_id in self._completed_turn_ids:
                return []
            self._remember_completed_turn(turn_id)
        if self._turn_status(event) != "completed":
            self._reset_messages()
            return [{"kind": "progress", "event": self._sanitized_turn_completion(event)}]
        decision = self.supervisor.gate_final(
            self.task_id, str(uuid.uuid4()), self._select_final_message(), project=self.project
        )
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
        text = item.get("text")
        if not isinstance(text, str):
            text = "".join(buffer) if buffer is not None else ""
        phase = item.get("phase")
        self._completed_messages.append((phase if isinstance(phase, str) else None, text))
        return True

    def _select_final_message(self):
        """Prefer an explicit final answer; commentary is never a fallback."""
        for phase, text in reversed(self._completed_messages):
            if phase == "final_answer":
                return text
        for phase, text in reversed(self._completed_messages):
            if phase is None:
                return text
        return ""

    def _reset_messages(self):
        self._message_buffers.clear()
        self._completed_messages.clear()

    def _remember_completed_turn(self, turn_id):
        """Remember every completed turn for this renderer/task lifetime."""
        self._completed_turn_ids.add(turn_id)

    @staticmethod
    def _turn_completion_id(event):
        """Return one well-formed completion ID, rejecting missing or conflicting fields."""
        params = event.get("params")
        if not isinstance(params, dict):
            return None
        turn = params.get("turn")
        nested_id = turn.get("id") if isinstance(turn, dict) else None
        direct_id = params.get("turnId")
        identifiers = [identifier for identifier in (nested_id, direct_id) if identifier is not None]
        if not identifiers or any(
            not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 512
            for identifier in identifiers
        ):
            return None
        if any(identifier != identifiers[0] for identifier in identifiers[1:]):
            return None
        return identifiers[0]

    @classmethod
    def _sanitized_turn_completion(cls, event):
        """Expose only a recognized terminal status, never agent-provided output."""
        return {"method": "turn/completed", "params": {"status": cls._turn_status(event)}}

    @staticmethod
    def _turn_status(event):
        """Return the protocol's explicit terminal status, or a safe unknown value."""
        params = event.get("params")
        if not isinstance(params, dict):
            return "unknown"
        status = params.get("status")
        if status is None and isinstance(params.get("turn"), dict):
            status = params["turn"].get("status")
        if isinstance(status, str) and status in {"completed", "failed", "interrupted", "cancelled"}:
            return status
        return "unknown"
