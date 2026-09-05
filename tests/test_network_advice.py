"""T0 — the network's own instruction, and the fact that it outranks everything else.

Getting this wrong is expensive in a way the other tiers are not: a retry against a
"do not try again" is a per-attempt fee, not merely a wasted call.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from backstop.diagnosis.advice import parse
from backstop.diagnosis.cohort import CohortModel
from backstop.diagnosis.engine import DiagnosisEngine
from backstop.domain.case import Case
from backstop.domain.types import CaseType, Disposition
from backstop.planner.actions import Action, ActionKind
from backstop.planner.planner import Planner
from backstop.policy.config import PolicyConfig
from backstop.policy.engine import PolicyEngine
from backstop.policy.rules import RuleContext

NOW = datetime(2026, 9, 10, 14, 0)


def make_case(**kw) -> Case:
    defaults = dict(
        case_id="c1", case_type=CaseType.CARD_FAILURE, amount_paise=250_000,
        customer_ref="cust_1", issuer="ICICI", instrument="card",
        failure_code="do_not_honour", created_at=NOW - timedelta(days=5),
        network="mastercard",
    )
    return Case(**{**defaults, **kw})


def ctx(**kw) -> RuleContext:
    defaults = dict(now=NOW, config=PolicyConfig(), expected_recovery_paise=5_000_000,
                    mandate_notice_sent_at=NOW - timedelta(days=6))
    return RuleContext(**{**defaults, **kw})


RETRY = Action(ActionKind.RETRY_PAYMENT, NOW)


# --- the tables ------------------------------------------------------------

@pytest.mark.parametrize("code,hours", [
    ("24", 1), ("25", 24), ("26", 48), ("27", 96),
    ("28", 144), ("29", 192), ("30", 240),
])
def test_mastercard_retry_intervals(code, hours):
    a = parse("mastercard", code)
    assert a.retryable
    assert a.earliest_retry == timedelta(hours=hours)


@pytest.mark.parametrize("code", ["03", "21"])
def test_mastercard_never_retry_codes_are_fee_events(code):
    a = parse("mastercard", code)
    assert not a.retryable and a.penalised_if_retried


@pytest.mark.parametrize("code", ["01", "04"])
def test_mastercard_credential_codes_ask_for_a_new_instrument(code):
    a = parse("mastercard", code)
    assert not a.retryable and a.needs_new_credential
    # Not a fee event — the retry is pointless, not forbidden.
    assert not a.penalised_if_retried


def test_mastercard_02_is_retryable_without_a_stated_time():
    a = parse("mastercard", "02")
    assert a.retryable and a.earliest_retry is None


@pytest.mark.parametrize("code,retryable,new_cred,penalised", [
    ("1", False, False, True),
    ("2", True, False, False),
    ("3", False, True, False),
    ("4", True, False, False),
])
def test_visa_categories(code, retryable, new_cred, penalised):
    a = parse("visa", code)
    assert (a.retryable, a.needs_new_credential, a.penalised_if_retried) == (
        retryable, new_cred, penalised)


def test_unknown_input_yields_no_advice_rather_than_a_guess():
    """Inventing permission the network did not grant is the failure mode here."""
    assert parse("mastercard", "99") is None
    assert parse("amex", "03") is None
    assert parse(None, "03") is None
    assert parse("visa", None) is None


# --- PR-04 obeys it --------------------------------------------------------

def test_never_retry_advice_denies_the_retry():
    d = PolicyEngine().evaluate(
        make_case(advice_code="03"), RETRY, ctx(advice=parse("mastercard", "03")))
    assert d.disposition is Disposition.DENY
    assert "PR-04" in d.rule_ids
    assert "fee event" in d.reason


def test_never_retry_advice_does_not_block_other_actions():
    """MAC 03 forbids the retry. It does not forbid asking the customer for a new card."""
    hit = ctx(advice=parse("mastercard", "03"))
    link = Action(ActionKind.REQUEST_REAUTH_LINK, NOW)
    assert PolicyEngine().evaluate(make_case(), link, hit).allowed


def test_advice_outranks_a_healthy_retry_budget():
    """Budget remaining, spacing met, ceiling far away — and still denied."""
    case = make_case(retries_used=0)
    clean = ctx(network="mastercard", retries_in_network_window=0)
    assert PolicyEngine().evaluate(case, RETRY, clean).allowed
    with_advice = ctx(network="mastercard", retries_in_network_window=0,
                      advice=parse("mastercard", "21"))
    assert PolicyEngine().evaluate(case, RETRY, with_advice).disposition is Disposition.DENY


def test_retry_after_interval_defers_until_the_network_says_so():
    declined = NOW - timedelta(hours=2)
    d = PolicyEngine().evaluate(
        make_case(), RETRY,
        ctx(advice=parse("mastercard", "26"), last_decline_at=declined))  # 2 days
    assert d.disposition is Disposition.DEFER
    assert d.defer_until == declined + timedelta(days=2)


def test_retry_is_allowed_once_the_advised_interval_has_passed():
    declined = NOW - timedelta(days=3)
    d = PolicyEngine().evaluate(
        make_case(), RETRY,
        ctx(advice=parse("mastercard", "26"), last_decline_at=declined))
    assert d.allowed


def test_no_advice_falls_back_to_the_ordinary_rules():
    assert PolicyEngine().evaluate(make_case(), RETRY, ctx(advice=None)).allowed


# --- diagnosis and planner ------------------------------------------------

def diagnose(case: Case):
    return DiagnosisEngine(CohortModel()).diagnose(case)


def test_advice_sets_the_retry_window_and_reports_tier_T0():
    case = make_case(advice_code="27")  # retry after 4 days
    dx = diagnose(case)
    assert dx.tier == "T0"
    assert dx.retry_window_start == case.created_at + timedelta(days=4)
    assert any(e.tier == "T0" for e in dx.evidence)


def test_network_timing_overrides_the_taxonomy_offset():
    """insufficient_funds would otherwise wait 3 days for the payday effect."""
    base = make_case(failure_code="insufficient_funds", advice_code=None)
    advised = make_case(failure_code="insufficient_funds", advice_code="24")  # 1 hour
    assert diagnose(base).retry_window_start == base.created_at + timedelta(days=3)
    assert diagnose(advised).retry_window_start == advised.created_at + timedelta(hours=1)


def test_planner_stops_proposing_a_retry_the_network_ruled_out():
    planner = Planner(epsilon=0.0)
    case = make_case(advice_code="03")
    kinds = {a.kind for a in planner._candidates(case, diagnose(case), NOW)}
    assert ActionKind.RETRY_PAYMENT not in kinds


def test_planner_offers_a_reauth_link_when_the_credential_is_the_problem():
    planner = Planner(epsilon=0.0)
    case = make_case(advice_code="01")  # new account information available
    kinds = {a.kind for a in planner._candidates(case, diagnose(case), NOW)}
    assert ActionKind.REQUEST_REAUTH_LINK in kinds
    assert ActionKind.RETRY_PAYMENT not in kinds


def test_a_retryable_case_still_gets_its_retry():
    planner = Planner(epsilon=0.0)
    case = make_case(advice_code="25")
    kinds = {a.kind for a in planner._candidates(case, diagnose(case), NOW)}
    assert ActionKind.RETRY_PAYMENT in kinds


def test_generated_advice_codes_are_all_parseable():
    """A generator emitting codes the parser rejects would silently disable T0."""
    from backstop.sim.generator import ADVICE_BY_CODE
    for code, per_network in ADVICE_BY_CODE.items():
        for network, table in per_network.items():
            total = sum(w for _, w in table)
            assert abs(total - 1.0) < 1e-9, f"{code}/{network} weights sum to {total}"
            for value, _ in table:
                if value is not None:
                    assert parse(network, value) is not None, f"{network} {value}"
