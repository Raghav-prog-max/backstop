"""The rule set is the part a panel will interrogate, so it is the part with tests."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from backstop.domain.case import Case
from backstop.domain.types import CaseType, Channel, Disposition
from backstop.planner.actions import Action, ActionKind
from backstop.policy.config import PolicyConfig
from backstop.policy.engine import PolicyEngine
from backstop.policy.rules import RuleContext

NOW = datetime(2026, 9, 10, 14, 0)


def make_case(**kw) -> Case:
    defaults = dict(
        case_id="c1",
        case_type=CaseType.CARD_FAILURE,
        amount_paise=250_000,
        customer_ref="cust_1",
        issuer="HDFC",
        instrument="card",
        failure_code="insufficient_funds",
        created_at=NOW - timedelta(days=2),
    )
    return Case(**{**defaults, **kw})


def ctx(**kw) -> RuleContext:
    defaults = dict(
        now=NOW,
        config=PolicyConfig(),
        expected_recovery_paise=50_000,
        mandate_notice_sent_at=NOW - timedelta(days=3),
    )
    return RuleContext(**{**defaults, **kw})


def msg(now: datetime = NOW) -> Action:
    return Action(ActionKind.SEND_MESSAGE, now, channel=Channel.WHATSAPP,
                  template="one_tap_link")


def retry(now: datetime = NOW) -> Action:
    return Action(ActionKind.RETRY_PAYMENT, now)


def test_clean_case_is_allowed():
    d = PolicyEngine().evaluate(make_case(), msg(), ctx())
    assert d.allowed


def test_pr01_denies_contact_without_consent():
    d = PolicyEngine().evaluate(make_case(), msg(), ctx(on_dnd_registry=True))
    assert d.disposition is Disposition.DENY
    assert "PR-01" in d.rule_ids


def test_pr01_does_not_touch_a_retry():
    """A DND listing must not block a silent retry — it is not a contact."""
    d = PolicyEngine().evaluate(make_case(), retry(), ctx(on_dnd_registry=True))
    assert d.allowed


def test_pr02_defers_outside_contact_window_rather_than_dropping():
    late = NOW.replace(hour=23)
    d = PolicyEngine().evaluate(make_case(), msg(late), ctx(now=late))
    assert d.disposition is Disposition.DEFER
    assert d.defer_until is not None and d.defer_until > late


def test_pr03_caps_contacts_per_channel():
    case = make_case()
    case.contacts_by_channel[Channel.WHATSAPP] = PolicyConfig().max_contacts_per_channel
    d = PolicyEngine().evaluate(case, msg(), ctx())
    assert d.disposition is Disposition.DENY
    assert "PR-03" in d.rule_ids


def test_pr04_denies_retry_but_leaves_other_actions_alone():
    case = make_case(retries_used=PolicyConfig().max_retries)
    assert PolicyEngine().evaluate(case, retry(), ctx()).disposition is Disposition.DENY
    assert PolicyEngine().evaluate(case, msg(), ctx()).allowed


def test_pr04_enforces_retry_spacing():
    d = PolicyEngine().evaluate(
        make_case(), retry(), ctx(last_retry_at=NOW - timedelta(hours=2))
    )
    assert d.disposition is Disposition.DEFER


def test_pr05_defers_mandate_debit_without_notice():
    case = make_case(case_type=CaseType.MANDATE_LAPSE, instrument="upi_mandate")
    d = PolicyEngine().evaluate(case, retry(), ctx(mandate_notice_sent_at=None))
    assert d.disposition is Disposition.DEFER
    assert "PR-05" in d.rule_ids


def test_pr06_suppresses_when_cost_exceeds_expected_recovery():
    d = PolicyEngine().evaluate(make_case(), msg(), ctx(expected_recovery_paise=100))
    assert d.disposition is Disposition.SUPPRESS
    assert "PR-06" in d.rule_ids


def test_pr06_goodwill_cost_rises_with_prior_contacts():
    """Same case, same expected value — only the contact history differs."""
    cheap = make_case()
    expensive = make_case()
    expensive.contacts_by_channel[Channel.WHATSAPP] = 3
    value = ctx(expected_recovery_paise=3_000)
    assert PolicyEngine().evaluate(cheap, msg(), value).allowed
    assert PolicyEngine().evaluate(expensive, msg(), value).disposition is Disposition.SUPPRESS


def test_pr07_hard_stop_beats_everything_else():
    d = PolicyEngine().evaluate(make_case(opted_out=True), msg(), ctx())
    assert d.disposition is Disposition.HARD_STOP


def test_pr08_holds_contact_during_a_promise_to_pay():
    case = make_case(promise_until=NOW + timedelta(days=3))
    d = PolicyEngine().evaluate(case, msg(), ctx())
    assert d.disposition is Disposition.DEFER
    assert "PR-08" in d.rule_ids


def test_every_decision_records_the_config_version():
    d = PolicyEngine().evaluate(make_case(), msg(), ctx())
    assert any(r.startswith("cfg:") for r in d.rule_ids)


def test_strictest_disposition_wins_when_several_rules_fire():
    case = make_case(opted_out=True)
    case.contacts_by_channel[Channel.WHATSAPP] = 5
    d = PolicyEngine().evaluate(case, msg(), ctx(on_dnd_registry=True))
    assert d.disposition is Disposition.HARD_STOP
    # …and the rules that lost are still on the record.
    assert {"PR-01", "PR-03", "PR-07"} <= set(d.rule_ids)


@pytest.mark.parametrize("kind", [ActionKind.WAIT, ActionKind.CLOSE_CASE])
def test_free_actions_are_never_suppressed_on_cost(kind):
    action = Action(kind, NOW)
    d = PolicyEngine().evaluate(make_case(), action, ctx(expected_recovery_paise=0))
    assert d.allowed
