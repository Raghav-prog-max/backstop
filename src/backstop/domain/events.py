"""The append-only event record. Every state change in the system is one of these."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class EventKind(str, Enum):
    DETECTED = "detected"
    DIAGNOSED = "diagnosed"
    POLICY_DECIDED = "policy_decided"
    INTENT_QUEUED = "intent_queued"
    ACTION_RESULT = "action_result"
    OUTCOME = "outcome"
    NOTE = "note"


@dataclass(frozen=True, slots=True)
class CaseEvent:
    case_id: str
    kind: EventKind
    occurred_at: datetime
    payload: dict[str, Any] = field(default_factory=dict)
    # Every rule that fired, whether or not it allowed anything. This is the audit trail.
    rule_ids: tuple[str, ...] = ()
    actor: str = "system"
    # Two events with the same idem_key are the same event. The ledger keeps the first.
    idem_key: str | None = None
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def key(self) -> str:
        return self.idem_key or self.event_id
