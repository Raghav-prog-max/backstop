"""SQLite ledger — same shape as the Postgres table in the architecture doc.

SEAM: swapping this for Postgres is a driver change plus `jsonb` instead of `text`.
The uniqueness constraint on idem_key is what enforces exactly-once, not app code.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Iterable

from ..domain.events import CaseEvent, EventKind

SCHEMA = """
CREATE TABLE IF NOT EXISTS case_event (
    event_id    TEXT PRIMARY KEY,
    case_id     TEXT NOT NULL,
    kind        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    rule_ids    TEXT NOT NULL,
    actor       TEXT NOT NULL,
    idem_key    TEXT UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_case_event_case ON case_event(case_id, occurred_at);
"""


class SqliteLedger:
    def __init__(self, path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(path)
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def append(self, event: CaseEvent) -> bool:
        try:
            self._conn.execute(
                "INSERT INTO case_event VALUES (?,?,?,?,?,?,?,?)",
                (
                    event.event_id,
                    event.case_id,
                    event.kind.value,
                    json.dumps(event.payload, default=str),
                    json.dumps(list(event.rule_ids)),
                    event.actor,
                    event.key(),
                    event.occurred_at.isoformat(),
                ),
            )
        except sqlite3.IntegrityError:
            return False  # idem_key already present
        self._conn.commit()
        return True

    def events_for(self, case_id: str) -> list[CaseEvent]:
        rows = self._conn.execute(
            "SELECT event_id, case_id, kind, payload, rule_ids, actor, idem_key,"
            " occurred_at FROM case_event WHERE case_id=? ORDER BY occurred_at, rowid",
            (case_id,),
        ).fetchall()
        return [_row_to_event(r) for r in rows]

    def all_events(self) -> Iterable[CaseEvent]:
        rows = self._conn.execute(
            "SELECT event_id, case_id, kind, payload, rule_ids, actor, idem_key,"
            " occurred_at FROM case_event ORDER BY occurred_at, rowid"
        ).fetchall()
        return (_row_to_event(r) for r in rows)

    def close(self) -> None:
        self._conn.close()

    def __len__(self) -> int:
        (n,) = self._conn.execute("SELECT COUNT(*) FROM case_event").fetchone()
        return int(n)


def _row_to_event(row: tuple) -> CaseEvent:
    return CaseEvent(
        event_id=row[0],
        case_id=row[1],
        kind=EventKind(row[2]),
        payload=json.loads(row[3]),
        rule_ids=tuple(json.loads(row[4])),
        actor=row[5],
        idem_key=row[6],
        occurred_at=datetime.fromisoformat(row[7]),
    )
