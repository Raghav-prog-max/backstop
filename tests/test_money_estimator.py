"""The rupee figure, and how much of it is real.

Recovery rates are counts. Rupees are counts times an amount distribution that spans
four orders of magnitude, so the money estimate is the fragile one — and it is the
one a slide quotes. These pin the two things that keep it honest: it is priced within
amount strata rather than off one amount-weighted holdout rate, and it carries an
interval that is allowed to say the number is not distinguishable from zero.
"""

from __future__ import annotations

from datetime import datetime

from backstop.domain.case import Case
from backstop.domain.types import Arm, CaseState, CaseType
from backstop.measurement.report import _incremental, compute_lift

NOW = datetime(2026, 9, 1, 11, 0)


def case(cid: str, arm: Arm, recovered: bool, amount: int) -> Case:
    c = Case(
        case_id=cid, case_type=CaseType.CARD_FAILURE, amount_paise=amount,
        customer_ref="x", issuer="HDFC", instrument="card",
        failure_code="do_not_honour", created_at=NOW, arm=arm,
    )
    c.state = CaseState.RECOVERED if recovered else CaseState.ABANDONED
    return c


def _naive(cases: list[Case]) -> int:
    """The estimator this replaced: the holdout's amount-weighted rate, applied whole."""
    t = [c for c in cases if c.arm is Arm.TREATED]
    h = [c for c in cases if c.arm is Arm.HOLDOUT]
    at_risk = sum(c.amount_paise for c in h)
    rate = sum(c.amount_paise for c in h if c.state is CaseState.RECOVERED) / at_risk
    return sum(c.amount_paise for c in t if c.state is CaseState.RECOVERED) - int(
        sum(c.amount_paise for c in t) * rate
    )


def mixed_batch() -> tuple[list[Case], int]:
    """Small cases the agent helps a lot, large ones it does not — and a holdout whose
    amount mix differs from the treated arm's, which is what randomising a few hundred
    heavy-tailed cases actually produces. Returns the batch and the money genuinely
    attributable to the agent."""
    cases: list[Case] = []
    # 400 small cases at Rs 100: treated recover 60%, holdout 20%. Real lift here.
    for i in range(400):
        cases.append(case(f"ts{i}", Arm.TREATED, i % 10 < 6, 10_000))
    for i in range(150):
        cases.append(case(f"hs{i}", Arm.HOLDOUT, i % 10 < 2, 10_000))
    # 400 large cases at Rs 5,000: both arms recover 30%. No lift at all — and the
    # holdout happens to hold fewer of them than its share.
    for i in range(400):
        cases.append(case(f"tl{i}", Arm.TREATED, i % 10 < 3, 500_000))
    for i in range(50):
        cases.append(case(f"hl{i}", Arm.HOLDOUT, i % 10 < 3, 500_000))

    truth = int(0.40 * 400 * 10_000)  # only the small stratum moved
    return cases, truth


def test_money_is_priced_within_amount_strata():
    cases, truth = mixed_batch()
    assert abs(_incremental(cases) - truth) < 0.05 * truth


def test_the_estimator_it_replaced_gets_this_wrong():
    """Not a straw man: pooling the arms' money hides where the lift came from."""
    cases, truth = mixed_batch()
    naive = _naive(cases)
    assert abs(naive - truth) > abs(_incremental(cases) - truth)


def test_a_stratum_with_no_holdout_of_its_own_borrows_the_pooled_rate():
    """One enormous case with no counterfactual must not invent its own."""
    cases, truth = mixed_batch()
    cases.append(case("whale", Arm.TREATED, True, 50_000_000))
    out = _incremental(cases)
    # It contributes at the pooled difference, not at 100% of a Rs 5 lakh invoice.
    assert truth < out < truth + 50_000_000 * 0.4


def test_the_money_figure_carries_an_interval_and_can_admit_it_is_noise():
    # Same rates in both arms, but the amounts are lumpy: no real effect to find.
    cases = [case(f"t{i}", Arm.TREATED, i % 4 == 0, 10_000 + (i % 7) * 400_000)
             for i in range(600)]
    cases += [case(f"h{i}", Arm.HOLDOUT, i % 4 == 0, 10_000 + (i % 7) * 400_000)
              for i in range(200)]
    lift = compute_lift(cases, money_ci_draws=200)
    assert lift.incremental_ci_paise > 0
    assert not lift.money_significant

    # And a real, uniform effect does clear its interval.
    strong = [case(f"t{i}", Arm.TREATED, i % 10 < 8, 100_000) for i in range(600)]
    strong += [case(f"h{i}", Arm.HOLDOUT, i % 10 < 2, 100_000) for i in range(200)]
    assert compute_lift(strong, money_ci_draws=200).money_significant


def test_the_interval_is_reproducible():
    cases, _ = mixed_batch()
    a = compute_lift(cases, money_ci_draws=100).incremental_ci_paise
    b = compute_lift(cases, money_ci_draws=100).incremental_ci_paise
    assert a == b and a > 0


def test_asking_for_no_interval_costs_nothing_and_claims_nothing():
    cases, _ = mixed_batch()
    lift = compute_lift(cases)
    assert lift.incremental_ci_paise == 0
    assert not lift.money_significant  # absence of an interval is not significance
