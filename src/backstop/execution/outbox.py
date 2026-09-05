"""Transactional outbox.

The intent is committed to the ledger in the same step as the state change, then
dispatched, then the result is appended. A crash between "decided to send" and "sent"
can only cause a re-dispatch against a stable idempotency key — never a lost action,
and never a duplicate contact. A duplicate contact is a compliance incident, not a bug.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ..domain.case import Case
from ..domain.events import CaseEvent, EventKind
from ..ledger.store import LedgerStore
from ..planner.actions import Action


@dataclass(slots=True)
class ActionResult:
    ok: bool
    detail: str
    recovered_paise: int = 0
    # Set when the customer committed to a date rather than paying. The case goes on
    # PR-08 hold until then; what happens at that date is a separate event.
    promise_until: datetime | None = None


class ExecutorBackend(Protocol):
    def execute(self, case: Case, action: Action, now: datetime) -> ActionResult: ...


class Outbox:
    def __init__(self, ledger: LedgerStore, backend: ExecutorBackend) -> None:
        self.ledger = ledger
        self.backend = backend
        self.dispatched = 0
        self.deduped = 0

    def queue_and_dispatch(
        self, case: Case, action: Action, attempt: int, now: datetime
    ) -> ActionResult | None:
        idem = action.idem_key(case.case_id, attempt)

        queued = self.ledger.append(
            CaseEvent(
                case_id=case.case_id,
                kind=EventKind.INTENT_QUEUED,
                occurred_at=now,
                payload={"action": str(action), "fire_at": action.fire_at.isoformat()},
                actor="planner",
                idem_key=idem,
            )
        )
        if not queued:
            # Already queued in a previous run. Do not dispatch a second time.
            self.deduped += 1
            return None

        result = self.backend.execute(case, action, now)
        self.dispatched += 1
        self.ledger.append(
            CaseEvent(
                case_id=case.case_id,
                kind=EventKind.ACTION_RESULT,
                occurred_at=now,
                payload={
                    "action": str(action),
                    "ok": result.ok,
                    "detail": result.detail,
                    "recovered_paise": result.recovered_paise,
                },
                actor="executor",
                idem_key=f"{idem}:result",
            )
        )
        return result
