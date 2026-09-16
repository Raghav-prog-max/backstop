"""Transactional outbox.

The intent is committed to the ledger in the same step as the state change, then
dispatched, then the result is appended. A crash between "decided to send" and "sent"
can only cause a re-dispatch against a stable idempotency key — never a lost action,
and never a duplicate contact. A duplicate contact is a compliance incident, not a bug.

Three states on replay, told apart by what the ledger already holds:
  * no intent            -> queue it and dispatch
  * intent, no result    -> the previous run died mid-dispatch; dispatch again under the
                            same key, which the provider uses to drop a send it has seen
  * intent and result    -> already done; return the recorded result, send nothing
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
    def execute(self, case: Case, action: Action, now: datetime,
                idem_key: str) -> ActionResult:
        """`idem_key` is stable across retries of the same intent. A real adapter passes
        it to the provider so a re-dispatch after a crash cannot deliver twice."""
        ...


class Outbox:
    def __init__(self, ledger: LedgerStore, backend: ExecutorBackend) -> None:
        self.ledger = ledger
        self.backend = backend
        self.dispatched = 0
        self.deduped = 0
        self.redispatched = 0

    def queue_and_dispatch(
        self, case: Case, action: Action, attempt: int, now: datetime
    ) -> ActionResult:
        idem = action.idem_key(case.case_id, attempt)
        result_key = f"{idem}:result"

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
            recorded = self._recorded_result(case.case_id, result_key)
            if recorded is not None:
                # Already done in a previous run. The caller gets the same answer it
                # got then, so replaying a batch cannot change what the case did.
                self.deduped += 1
                return recorded
            # Queued but never completed: the intent must not be lost.
            self.redispatched += 1

        result = self.backend.execute(case, action, now, idem)
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
                    "promise_until": (result.promise_until.isoformat()
                                      if result.promise_until else None),
                },
                actor="executor",
                idem_key=result_key,
            )
        )
        return result

    def _recorded_result(self, case_id: str, result_key: str) -> ActionResult | None:
        for event in self.ledger.events_for(case_id):
            if event.idem_key == result_key:
                p = event.payload
                until = p.get("promise_until")
                return ActionResult(
                    ok=bool(p.get("ok")),
                    detail=str(p.get("detail", "")),
                    recovered_paise=int(p.get("recovered_paise", 0)),
                    promise_until=datetime.fromisoformat(until) if until else None,
                )
        return None
