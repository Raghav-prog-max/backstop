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
