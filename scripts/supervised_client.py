#!/usr/bin/env python3
"""Custom-client rendering boundary for Codex App Server notifications.

Callers render the dictionaries returned by ``consume``. Agent-message deltas are
never returned until HostSupervisor.gate_final releases them after turn/completed.
"""
import uuid
import threading


class SupervisedRenderer:
    def __init__(self, supervisor, task_id, *, thread_id, turn_id, project=None):
        # These identifiers come from the host's turn/start response, never from
        # the first notification on a shared App Server event stream.
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError("A host-bound thread_id is required")
        if not isinstance(turn_id, str) or not turn_id.strip():
            raise ValueError("A host-bound turn_id is required")
        self.supervisor = supervisor
        self.task_id = task_id
        self.project = project
        self.thread_id = thread_id
        self.turn_id = turn_id
        self._closed = False
        self._lock = threading.Lock()
        self._attempt_id = str(uuid.uuid4())
        self._deltas = []
        self._completed_messages = []
        self._final_message = None

    def consume(self, event):
        with self._lock:
            return self._consume(event)

    def _consume(self, event):
        params = event.get("params", {})
        if self._closed or params.get("threadId") != self.thread_id:
            return []
        # Turn completion carries its ID in turn.id; item notifications carry
        # turnId. Reject missing IDs and conflicting duplicate fields.
        nested_id = params.get("turn", {}).get("id")
        direct_id = params.get("turnId")
        event_turn_id = nested_id if event.get("method") == "turn/completed" else direct_id
        if event_turn_id != self.turn_id:
            return []
        if any(value is not None and value != self.turn_id for value in (nested_id, direct_id)):
            return []
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
                    self._final_message = item.get("text", "")
                elif phase is None:
                    self._completed_messages = [item.get("text", "")]
                return []
        if method != "turn/completed":
            return [{"kind": "progress", "event": event}]
        # A renderer is single-use, including unsuccessful completion and gate
        # exceptions. Continuation must bind a fresh renderer to a new host turn.
        self._closed = True
        status = event.get("params", {}).get("turn", {}).get("status")
        if status != "completed":
            self._deltas = []
            self._completed_messages = []
            self._final_message = None
            return [{"kind": "turn_incomplete", "status": status,
                     "message": "Turn did not complete; host state remains recoverable."}]
        content = self._final_message if self._final_message is not None else "".join(self._completed_messages or self._deltas)
        decision = self.supervisor.gate_final(self.task_id, self._attempt_id, content, project=self.project)
        self._deltas = []
        self._completed_messages = []
        self._final_message = None
        if decision.release:
            return [{"kind": "final", "content": decision.message, "decision": decision.kind}]
        if decision.kind == "continue":
            return [{"kind": "continuation", "next_action": decision.next_action, "message": decision.message}]
        return [{"kind": decision.kind, "message": decision.message}]
