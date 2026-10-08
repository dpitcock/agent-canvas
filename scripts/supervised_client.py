#!/usr/bin/env python3
"""Custom-client rendering boundary for Codex App Server notifications.

Callers render the dictionaries returned by ``consume``. Agent-message deltas are
never returned until HostSupervisor.gate_final releases them after turn/completed.
"""
import threading
import uuid


class SupervisedRenderer:
    def __init__(
        self, supervisor, task_id, *, project=None, thread_id=None, turn_id=None,
        legacy_test_mode=False,
    ):
        self.supervisor = supervisor
        self.task_id = task_id
        self.project = project
        if (thread_id is None) != (turn_id is None):
            raise ValueError("thread_id and turn_id must be bound together")
        if thread_id is None and not legacy_test_mode:
            raise ValueError("thread_id and turn_id are required outside legacy test mode")
        if thread_id is not None and (
            not self._valid_identifier(thread_id) or not self._valid_identifier(turn_id)
        ):
            raise ValueError("thread_id and turn_id must be non-empty strings")
        self.thread_id = thread_id
        self.turn_id = turn_id
        self._message_buffers = {}
        self._completed_message_ids = set()
        self._completed_messages = []
        self._completed_turn_ids = set()
        self._completed_turn_lock = threading.Lock()

    def consume(self, event):
        if not isinstance(event, dict):
            return [{"kind": "progress", "event": event}]
        method = event.get("method")
        # Raw response items may carry assistant text.  This renderer has no
        # safe representation for them before the supervisor has gated a turn.
        if method == "rawResponseItem/completed":
            return []
        if self._is_private_event(method) and not self._matches_bound_turn(event):
            return []
        if method == "item/agentMessage/delta":
            self._buffer_agent_message_delta(event)
            return []
        if self._complete_agent_message(event):
            return []
        if isinstance(method, str) and method.startswith("item/"):
            # Other response-item types may also carry assistant content, but
            # only completed agent messages have a safe gated representation.
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
        final_message = self._select_final_message()
        if not final_message:
            self._reset_messages()
            return [{"kind": "progress", "event": self._sanitized_turn_completion(event)}]
        try:
            decision = self.supervisor.gate_final(
                self.task_id, str(uuid.uuid4()), final_message, project=self.project
            )
        except Exception:
            with self._completed_turn_lock:
                self._completed_turn_ids.discard(turn_id)
            raise
        self._reset_messages()
        if decision.release:
            return [{"kind": "final", "content": decision.message, "decision": decision.kind}]
        if decision.kind == "continue":
            return [{"kind": "continuation", "next_action": decision.next_action, "message": decision.message}]
        return [{"kind": decision.kind, "message": decision.message}]

    @staticmethod
    def _is_private_event(method):
        return method == "turn/completed" or method == "item/completed" or (
            isinstance(method, str) and method.startswith("item/agentMessage/")
        )

    def _matches_bound_turn(self, event):
        """Accept private events only from the renderer's configured App Server turn."""
        params = event.get("params")
        if not isinstance(params, dict):
            return self.thread_id is None
        # Legacy fixtures without App Server routing IDs remain compatible, but
        # an actual routed notification is unsafe until its destination is bound.
        if self.thread_id is None:
            return "threadId" not in params
        if params.get("threadId") != self.thread_id:
            return False
        event_turn_id = params.get("turnId")
        turn = params.get("turn")
        nested_turn_id = turn.get("id") if isinstance(turn, dict) else None
        identifiers = [identifier for identifier in (event_turn_id, nested_turn_id) if identifier is not None]
        return bool(identifiers) and all(identifier == self.turn_id for identifier in identifiers)

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
    def _valid_identifier(identifier):
        return isinstance(identifier, str) and bool(identifier.strip()) and len(identifier) <= 512

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
        if not identifiers or any(not SupervisedRenderer._valid_identifier(identifier) for identifier in identifiers):
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
        sources = [params]
        if isinstance(params.get("turn"), dict):
            sources.append(params["turn"])
        statuses = [source["status"] for source in sources if "status" in source]
        if not statuses or any(
            not isinstance(status, str) or status not in {"completed", "failed", "interrupted", "cancelled"}
            for status in statuses
        ):
            return "unknown"
        return statuses[0] if all(status == statuses[0] for status in statuses) else "unknown"
