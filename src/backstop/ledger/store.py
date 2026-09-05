"""Ledger interface plus an in-memory implementation.

The ledger is append-only and idempotent: appending an event whose idem_key has
already been seen is a no-op that returns False. That is what makes a crash between
"decided to send" and "sent" safe.
"""

from __future__ import annotations

from typing import Iterable, Protocol

from ..domain.events import CaseEvent


class LedgerStore(Protocol):
    def append(self, event: CaseEvent) -> bool:
        """Returns False if this event was already recorded (idempotency hit)."""

    def events_for(self, case_id: str) -> list[CaseEvent]: ...

    def all_events(self) -> Iterable[CaseEvent]: ...


class InMemoryLedger:
    def __init__(self) -> None:
        self._events: list[CaseEvent] = []
        self._by_case: dict[str, list[CaseEvent]] = {}
        self._seen: set[str] = set()

    def append(self, event: CaseEvent) -> bool:
        key = event.key()
        if key in self._seen:
            return False
        self._seen.add(key)
        self._events.append(event)
        self._by_case.setdefault(event.case_id, []).append(event)
        return True

    def events_for(self, case_id: str) -> list[CaseEvent]:
        return list(self._by_case.get(case_id, ()))

    def all_events(self) -> Iterable[CaseEvent]:
        return iter(self._events)

    def __len__(self) -> int:
        return len(self._events)
