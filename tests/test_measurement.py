"""If these tests are wrong the whole submission is wrong, so they are explicit."""

from __future__ import annotations

from datetime import datetime

from backstop.diagnosis.cohort import CohortModel
from backstop.diagnosis.engine import DiagnosisEngine, EvidenceRequired, Diagnosis
from backstop.diagnosis.taxonomy import classify
from backstop.domain.case import Case
from backstop.domain.types import Arm, CaseState, CaseType, CauseClass
from backstop.measurement.assignment import assign
from backstop.measurement.report import compute_lift

NOW = datetime(2026, 9, 1, 11, 0)


def case(cid: str, arm: Arm, recovered: bool, amount: int = 100_000) -> Case:
    c = Case(
        case_id=cid, case_type=CaseType.CARD_FAILURE, amount_paise=amount,
        customer_ref="x", issuer="HDFC", instrument="card",
        failure_code="do_not_honour", created_at=NOW, arm=arm,
    )
    c.state = CaseState.RECOVERED if recovered else CaseState.ABANDONED
    return c


def test_lift_is_the_difference_not_the_gross_rate():
    cases = [case(f"t{i}", Arm.TREATED, i < 60) for i in range(100)]
    cases += [case(f"h{i}", Arm.HOLDOUT, i < 20) for i in range(100)]
    lift = compute_lift(cases)
    assert lift.treated.rate == 0.60
    assert lift.holdout.rate == 0.20
    assert round(lift.lift_pp, 6) == 40.0


def test_a_system_that_does_nothing_useful_reports_no_lift():
    cases = [case(f"t{i}", Arm.TREATED, i < 30) for i in range(100)]
    cases += [case(f"h{i}", Arm.HOLDOUT, i < 30) for i in range(100)]
    lift = compute_lift(cases)
    assert lift.lift_pp == 0.0
    assert not lift.significant
    assert lift.incremental_paise == 0


def test_thin_cohorts_are_not_significant():
    cases = [case("t1", Arm.TREATED, True), case("h1", Arm.HOLDOUT, False)]
    assert not compute_lift(cases).significant


def test_incremental_money_subtracts_the_counterfactual():
    # Holdout recovers half its money; treated recovers all of it.
    cases = [case(f"t{i}", Arm.TREATED, True, 100_000) for i in range(10)]
    cases += [case(f"h{i}", Arm.HOLDOUT, i < 5, 100_000) for i in range(10)]
    lift = compute_lift(cases)
    assert lift.treated.amount_recovered == 1_000_000
    assert lift.incremental_paise == 500_000  # not 1,000,000


def test_arm_assignment_is_deterministic_and_roughly_on_target():
    ids = [f"case-{i}" for i in range(20_000)]
    first = [assign(i, 0.10) for i in ids]
    assert first == [assign(i, 0.10) for i in ids]  # reruns cannot reshuffle
    share = sum(a is Arm.HOLDOUT for a in first) / len(first)
    assert 0.09 < share < 0.11


def test_taxonomy_maps_known_codes_and_falls_back_safely():
    assert classify("expired_card") is CauseClass.EXPIRED_INSTRUMENT
    assert classify("something_new_from_the_gateway") is CauseClass.UNKNOWN


def test_diagnosis_requires_evidence():
    try:
        Diagnosis(CauseClass.UNKNOWN, 0.5, 0, NOW, "T1", evidence=[])
    except EvidenceRequired:
        return
    raise AssertionError("a Diagnosis without evidence must not be constructible")


def test_cohort_posterior_moves_toward_observed_reality():
    model = CohortModel(min_n=5)
    key = model.key("HDFC", "card", "lt_500", 11)
    before = model.posterior(key, 0.30).mean
    for _ in range(60):
        model.observe(key, True)
    after = model.posterior(key, 0.30)
    assert after.mean > before and after.tier_confident


def test_diagnosis_reports_the_tier_it_actually_used():
    model = CohortModel(min_n=5)
    engine = DiagnosisEngine(model)
    c = case("c1", Arm.TREATED, False)
    assert engine.diagnose(c).tier == "T1"
    key = model.key(c.issuer, c.instrument, c.amount_band(), c.created_at.hour)
    for _ in range(10):
        model.observe(key, True)
    assert engine.diagnose(c).tier == "T2"


def test_open_cases_are_censored_not_counted_as_lost_or_won():
    """Mid-batch, the fast recoveries are in and the slow ones are still open.

    Counting only resolved cases would read that as near-certain recovery. The daily
    hazard sees the open cases as days survived, and lands near the truth.
    """
    import random

    horizon, stop_at, daily = 14, 5, 0.05
    truth = 1 - (1 - daily) ** horizon  # ~0.51
    rng = random.Random(3)
    model = CohortModel(min_n=5, horizon_days=horizon)
    key = model.key("SBI", "card", "500_2k", 10)
    for i in range(400):
        for _day in range(stop_at):  # the batch is only five days in
            recovered = rng.random() < daily
            model.observe(key, recovered, case_id=f"c{i}")
            if recovered:
                break
    post = model.posterior(key, 0.30)
    assert post.n == 400  # cases, not case-days
    assert abs(post.mean - truth) < 0.08


def test_refresh_reprices_without_asking_the_model_again():
    calls = []

    class FakeLLM:
        def diagnose(self, case, free_text):
            calls.append(free_text)
            from backstop.diagnosis.engine import Evidence
            return CauseClass.INSUFFICIENT_FUNDS, [Evidence("free_text", "no balance", "T3")]

    model = CohortModel(min_n=5)
    engine = DiagnosisEngine(model, llm=FakeLLM())
    c = case("c9", Arm.TREATED, False)
    c.failure_code = "payment_failed"
    dx = engine.diagnose(c, "no balance")
    assert len(calls) == 1 and dx.tier == "T3"

    key = model.key(c.issuer, c.instrument, c.amount_band(), c.created_at.hour)
    for i in range(10):
        model.observe(key, False, case_id=f"q{i}")
    fresh = engine.refresh(c, dx)
    assert len(calls) == 1
    assert fresh.cause_class is CauseClass.INSUFFICIENT_FUNDS
    assert fresh.tier == "T2" and fresh.recoverability < dx.recoverability
    assert any(e.tier == "T3" for e in fresh.evidence)  # the model's quote survives


def test_runner_prices_later_decisions_with_the_learned_cohort():
    from backstop.domain.events import EventKind
    from backstop.runner import run

    result = run(2000, 10, 0.1, seed=5)
    assert result.decisions_priced_by_t2 > 0
    priced = [e.payload for e in result.ledger.all_events()
              if e.kind is EventKind.POLICY_DECIDED]
    assert all("p_recover" in p and "priced_by" in p for p in priced)
    assert any(p["priced_by"] == "T2" for p in priced)
