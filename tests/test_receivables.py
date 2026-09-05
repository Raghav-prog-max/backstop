"""B2B receivables and promise-to-pay.

What these pin down:
  * an overdue invoice never gets a payment retry — there is no instrument
  * a disputed invoice goes to a human, not into a dunning sequence
  * a promise puts the case on PR-08 hold; contact is deferred, not dropped
  * a kept promise is a recovery; a broken one is counted and changes the plan
  * asking for a date is a contact and counts against the frequency cap
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from backstop.diagnosis.cohort import CohortModel
from backstop.diagnosis.engine import DiagnosisEngine
from backstop.domain.case import Case
from backstop.domain.events import EventKind
from backstop.domain.types import CaseState, CaseType, CauseClass, Channel, Disposition
from backstop.planner.actions import Action, ActionKind
from backstop.planner.planner import Planner
from backstop.policy.config import PolicyConfig
from backstop.policy.engine import PolicyEngine
from backstop.policy.rules import CONTACT_ACTIONS, RuleContext
from backstop.runner import run
from backstop.sim.generator import generate
from backstop.sim.world import World

NOW = datetime(2026, 9, 10, 14, 0)


def invoice(code: str = "overdue_cash_flow", overdue_days: int = 20, **kw) -> Case:
    defaults = dict(
        case_id="inv1", case_type=CaseType.INVOICE_OVERDUE, amount_paise=42_000_000,
        customer_ref="buyer_1", issuer="B2B", instrument="invoice",
        failure_code=code, created_at=NOW - timedelta(days=2),
        due_at=NOW - timedelta(days=overdue_days),
    )
    return Case(**{**defaults, **kw})


def diagnose(case: Case):
    return DiagnosisEngine(CohortModel()).diagnose(case, case.free_text)


def greedy() -> Planner:
    return Planner(epsilon=0.0, rng=random.Random(0))


def candidates(case: Case) -> set[ActionKind]:
    p = greedy()
    return {a.kind for a in p._candidates(case, diagnose(case), NOW)}


# --- generator ---------------------------------------------------------------------

def test_batch_contains_invoices_with_due_dates_and_no_network():
    cases = generate(3000, start=NOW, seed=5)
    inv = [c for c in cases if c.case_type is CaseType.INVOICE_OVERDUE]
    assert 0.12 < len(inv) / len(cases) < 0.20
    for c in inv:
        assert c.due_at is not None and c.due_at < c.created_at
        assert c.network is None and c.advice_code is None
        assert c.instrument == "invoice"


def test_receivable_codes_map_to_receivable_causes():
    assert diagnose(invoice("overdue_ap_pending")).cause_class is CauseClass.AP_CYCLE
    assert diagnose(invoice("overdue_cash_flow")).cause_class is CauseClass.CASH_CONSTRAINED
    assert diagnose(invoice("overdue_query_raised")).cause_class is CauseClass.INVOICE_QUERY
    assert diagnose(invoice("overdue_no_response")).cause_class is CauseClass.UNKNOWN


# --- planner -----------------------------------------------------------------------

def test_invoice_never_gets_a_payment_retry():
    for code in ("overdue_ap_pending", "overdue_cash_flow", "overdue_query_raised", "overdue_no_response"):
        assert ActionKind.RETRY_PAYMENT not in candidates(invoice(code))
        assert ActionKind.REQUEST_REAUTH_LINK not in candidates(invoice(code))


def test_disputed_invoice_only_offers_a_human():
    kinds = candidates(invoice("overdue_query_raised"))
    assert kinds == {ActionKind.WAIT, ActionKind.ESCALATE_HUMAN}


def test_cash_constrained_buyer_is_asked_for_a_date():
    assert ActionKind.REQUEST_PROMISE_TO_PAY in candidates(invoice("overdue_cash_flow"))


def test_early_ap_cycle_is_not_asked_for_a_date_yet():
    early = invoice("overdue_ap_pending", overdue_days=5)
    late = invoice("overdue_ap_pending", overdue_days=25)
    assert ActionKind.REQUEST_PROMISE_TO_PAY not in candidates(early)
    assert ActionKind.REQUEST_PROMISE_TO_PAY in candidates(late)


def test_broken_promise_or_deep_overdue_puts_a_human_on_the_table():
    assert ActionKind.ESCALATE_HUMAN not in candidates(invoice("overdue_cash_flow", overdue_days=20))
    assert ActionKind.ESCALATE_HUMAN in candidates(invoice("overdue_cash_flow", overdue_days=70))
    broken = invoice("overdue_cash_flow", overdue_days=20)
    broken.promises_broken = 1
    assert ActionKind.ESCALATE_HUMAN in candidates(broken)


def test_large_invoice_asks_by_voice_small_one_by_email():
    big = invoice("overdue_cash_flow", amount_paise=25_000_000)
    small = invoice("overdue_cash_flow", amount_paise=2_500_000)
    ch = lambda c: next(a.channel for a in greedy()._candidates(c, diagnose(c), NOW)
                        if a.kind is ActionKind.REQUEST_PROMISE_TO_PAY)
    assert ch(big) is Channel.VOICE and ch(small) is Channel.EMAIL


# --- policy ------------------------------------------------------------------------

def ask(now: datetime = NOW) -> Action:
    return Action(ActionKind.REQUEST_PROMISE_TO_PAY, now, channel=Channel.EMAIL,
                  template="promise_to_pay")


def ctx(**kw) -> RuleContext:
    defaults = dict(now=NOW, config=PolicyConfig(), expected_recovery_paise=5_000_000)
    return RuleContext(**{**defaults, **kw})


def test_asking_for_a_date_is_a_contact():
    assert ActionKind.REQUEST_PROMISE_TO_PAY in CONTACT_ACTIONS
    case = invoice()
    case.contacts_by_channel[Channel.EMAIL] = PolicyConfig().max_contacts_per_channel
    d = PolicyEngine().evaluate(case, ask(), ctx())
    assert d.disposition is Disposition.DENY and d.deciding_rule == "PR-03"


def test_open_promise_defers_further_contact_until_after_grace():
    case = invoice()
    case.promise_until = NOW + timedelta(days=3)
    case.promise_status = "open"
    d = PolicyEngine().evaluate(case, ask(), ctx())
    assert d.disposition is Disposition.DEFER and d.deciding_rule == "PR-08"
    assert d.defer_until == case.promise_until + timedelta(hours=PolicyConfig().promise_grace_hours)


# --- world -------------------------------------------------------------------------

def test_world_can_answer_a_promise_request_with_a_date_and_then_resolve_it():
    w = World(14, seed=3)
    case = invoice("overdue_cash_flow")
    w.admit(case, CauseClass.CASH_CONSTRAINED)
    dates, kept = 0, 0
    for _ in range(200):
        r = w.execute(case, ask(), NOW)
        if r.promise_until is not None:
            dates += 1
            assert r.ok and r.recovered_paise == 0 and r.promise_until > NOW
            kept += w.promise_resolves(case)
    assert 40 < dates < 180          # buyers do commit, and not always
    assert 0 < kept < dates          # and a kept promise is not a certainty


# --- runner ------------------------------------------------------------------------

def test_runner_tracks_promises_and_never_counts_an_open_one_as_recovered():
    result = run(3000, 21, 0.1, seed=9)
    r = result.restraint
    assert r.promises_made > 0
    assert r.promises_kept + r.promises_broken <= r.promises_made
    assert r.promises_kept > 0 and r.promises_broken > 0

    inv = [c for c in result.cases if c.case_type is CaseType.INVOICE_OVERDUE]
    assert inv
    for c in inv:
        if c.promise_status == "open":
            assert c.state is not CaseState.RECOVERED or c.terminal_reason == "self-healed"
        if c.state is CaseState.RECOVERED and c.terminal_reason == "promise kept":
            assert c.promise_status == "kept"
    # The ledger carries the promise lifecycle as plain events.
    kept_case = next(c for c in inv if c.terminal_reason == "promise kept")
    notes = [e.payload.get("promise") for e in result.ledger.events_for(kept_case.case_id)
             if e.kind is EventKind.NOTE]
    assert notes == ["made", "kept"]


def test_disputed_invoices_are_escalated_not_chased():
    result = run(3000, 14, 0.1, seed=9)
    queried = [c for c in result.cases
               if c.case_type is CaseType.INVOICE_OVERDUE and c.cause is CauseClass.INVOICE_QUERY
               and c.arm.value == "treated"]
    assert queried
    for c in queried:
        assert c.contacts_total == 0
        assert c.state in (CaseState.ESCALATED, CaseState.RECOVERED, CaseState.SUPPRESSED)
