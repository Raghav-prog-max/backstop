"""Thirty slices at 95% confidence produce findings from nothing.

The decomposition exists so a single lucky cohort cannot carry the headline. Read
without a correction it does the opposite: it hands a reader twenty-odd chances to
find one, and the one they quote is the most extreme. These pin the correction.
"""

from __future__ import annotations

from datetime import datetime

from backstop.domain.case import Case
from backstop.domain.types import Arm, CaseState, CaseType
from backstop.measurement.report import (
    MIN_N_PER_ARM, Restraint, apply_fdr, compute_lift, decompose_all, render,
)

NOW = datetime(2026, 9, 1, 11, 0)


def case(cid: str, arm: Arm, recovered: bool, issuer: str = "HDFC",
         amount: int = 100_000) -> Case:
    c = Case(
        case_id=cid, case_type=CaseType.CARD_FAILURE, amount_paise=amount,
        customer_ref="x", issuer=issuer, instrument="card",
        failure_code="do_not_honour", created_at=NOW, arm=arm,
    )
    c.state = CaseState.RECOVERED if recovered else CaseState.ABANDONED
    c.tier = "T1"
    return c


def group(n_t: int, n_h: int, rate_t: float, rate_h: float, issuer: str) -> list[Case]:
    out = [case(f"{issuer}t{i}", Arm.TREATED, i < int(n_t * rate_t), issuer)
           for i in range(n_t)]
    out += [case(f"{issuer}h{i}", Arm.HOLDOUT, i < int(n_h * rate_h), issuer)
            for i in range(n_h)]
    return out


def test_bh_matches_the_textbook():
    """Benjamini-Hochberg 1995, worked by hand: m=5, alpha=0.05."""
    ps = [0.001, 0.008, 0.039, 0.041, 0.042]
    lifts = []
    for p in ps:
        lf = compute_lift(group(200, 200, 0.5, 0.5, "X"))
        lf.p_value = p
        lifts.append(lf)
    apply_fdr({"d": {str(i): lf for i, lf in enumerate(lifts)}})

    # q = p * m / rank, then the running minimum taken from the largest p downward,
    # so rank 3 (0.039 * 5/3 = 0.065 on its own) inherits rank 5's 0.042.
    assert [round(lf.q_value, 4) for lf in lifts] == [0.005, 0.02, 0.042, 0.042, 0.042]
    # The largest p survives on its own rank, so everything below it does too.
    assert [lf.survives_fdr for lf in lifts] == [True] * 5


def test_a_slice_that_only_clears_its_own_interval_is_called_out():
    cases: list[Case] = []
    # One genuine, large effect.
    cases += group(400, 400, 0.70, 0.30, "REAL")
    # A marginal one, plus a crowd of flat slices to make the family big.
    cases += group(120, 120, 0.50, 0.37, "MARGINAL")
    for i in range(12):
        cases += group(120, 120, 0.40, 0.40, f"FLAT{i}")

    groups = decompose_all(cases)
    by_issuer = groups["by issuer"]
    assert by_issuer["REAL"].significant and by_issuer["REAL"].survives_fdr
    marginal = by_issuer["MARGINAL"]
    assert marginal.significant          # clears its own interval
    assert not marginal.survives_fdr     # not in the company it keeps
    assert marginal.q_value > 0.05


def test_thin_slices_are_not_part_of_the_family():
    """A slice that makes no claim cannot make a false one, and must not make the
    correction harsher for the slices that do."""
    enough = group(400, 400, 0.70, 0.30, "REAL")
    thin = [c for i in range(6)
            for c in group(MIN_N_PER_ARM - 5, MIN_N_PER_ARM - 5, 0.9, 0.1, f"TINY{i}")]

    without = decompose_all(list(enough))
    with_thin = decompose_all(enough + thin)
    assert without["by issuer"]["REAL"].q_value == with_thin["by issuer"]["REAL"].q_value
    assert not with_thin["by issuer"]["TINY0"].testable
    assert not with_thin["by issuer"]["TINY0"].survives_fdr


def test_q_values_never_fall_as_p_rises():
    cases: list[Case] = []
    for i, (rt, n) in enumerate([(0.8, 300), (0.6, 300), (0.5, 200), (0.45, 200),
                                 (0.42, 150), (0.41, 150)]):
        cases += group(n, n, rt, 0.40, f"I{i}")
    buckets = decompose_all(cases)["by issuer"]
    ordered = sorted((lf for lf in buckets.values() if lf.testable),
                     key=lambda lf: lf.p_value)
    qs = [lf.q_value for lf in ordered]
    assert qs == sorted(qs)


def test_the_report_says_how_many_tests_it_ran():
    cases = group(400, 400, 0.70, 0.30, "REAL") + group(120, 120, 0.50, 0.37, "MARGINAL")
    for i in range(12):
        cases += group(120, 120, 0.40, 0.40, f"FLAT{i}")
    text = render(cases, Restraint(), 0)
    assert "Benjamini-Hochberg" in text
    assert "slices tested" in text
    assert "chance, q=" in text
