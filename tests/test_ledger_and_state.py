from __future__ import annotations

from datetime import datetime

import pytest

from backstop.domain.case import Case, IllegalTransition
from backstop.domain.events import CaseEvent, EventKind
from backstop.domain.types import CaseState, CaseType
from backstop.execution.outbox import ActionResult, Outbox
from backstop.ledger.sqlite import SqliteLedger
from backstop.ledger.store import InMemoryLedger
from backstop.planner.actions import Action, ActionKind

NOW = datetime(2026, 9, 10, 12, 0)


def make_case() -> Case:
    return Case(
        case_id="c1", case_type=CaseType.CARD_FAILURE, amount_paise=100_000,
        customer_ref="cust_1", issuer="HDFC", instrument="card",
        failure_code="do_not_honour", created_at=NOW,
    )


def event(idem: str | None = None) -> CaseEvent:
    return CaseEvent("c1", EventKind.NOTE, NOW, idem_key=idem)


@pytest.mark.parametrize("ledger", [InMemoryLedger(), SqliteLedger()])
def test_ledger_is_idempotent(ledger):
    assert ledger.append(event("k1")) is True
    assert ledger.append(event("k1")) is False
    assert len(ledger.events_for("c1")) == 1


def test_sqlite_ledger_round_trips_payload_and_rules():
    led = SqliteLedger()
    led.append(CaseEvent("c1", EventKind.POLICY_DECIDED, NOW,
                         payload={"disposition": "deny"}, rule_ids=("PR-03", "cfg:v1")))
    (got,) = led.events_for("c1")
    assert got.payload["disposition"] == "deny"
    assert got.rule_ids == ("PR-03", "cfg:v1")


def test_suppression_is_reachable_from_diagnosed_not_from_attempting():
    """A suppressed case is a decision, not a failed attempt."""
    case = make_case()
    case.transition(CaseState.DIAGNOSED)
    case.transition(CaseState.SUPPRESSED, reason="PR-06", rule_id="PR-06")
    assert case.is_terminal and case.stopping_rule == "PR-06"

    other = make_case()
    other.transition(CaseState.DIAGNOSED)
    other.transition(CaseState.PLANNED)
    other.transition(CaseState.ATTEMPTING)
    with pytest.raises(IllegalTransition):
        other.transition(CaseState.SUPPRESSED)


def test_attempting_can_cycle_back_to_planned():
    case = make_case()
    for state in (CaseState.DIAGNOSED, CaseState.PLANNED, CaseState.ATTEMPTING):
        case.transition(state)
    case.transition(CaseState.PLANNED)  # wait -> next attempt
    assert case.state is CaseState.PLANNED


def test_outbox_never_dispatches_the_same_intent_twice():
    """A crash between 'decided to send' and 'sent' must not double-contact anyone."""
    sent: list[str] = []

    class Backend:
        def execute(self, case, action, now):
            sent.append(str(action))
            return ActionResult(ok=False, detail="no response")

    ledger = InMemoryLedger()
    outbox = Outbox(ledger, Backend())
    case = make_case()
    action = Action(ActionKind.SEND_MESSAGE, NOW, template="one_tap_link")

    assert outbox.queue_and_dispatch(case, action, 1, NOW) is not None
    assert outbox.queue_and_dispatch(case, action, 1, NOW) is None  # replay
    assert len(sent) == 1
    assert outbox.deduped == 1
