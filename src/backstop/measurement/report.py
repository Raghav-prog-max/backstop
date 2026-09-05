"""The batch report.

Gross recovery answers neither of the track's measurement criteria, because a large
share of at-risk revenue recovers on its own. The reported number is the difference
between the treated arm and an untouched holdout, with an interval, decomposed by
cohort so no single lucky segment can carry it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from ..domain.case import Case
from ..domain.types import Arm, CaseState, Paise, rupees

Z_95 = 1.959964

# Below this many cases per arm we do not claim significance at all, whatever the
# interval says. A cohort of six that happens to recover is not a finding.
MIN_N_PER_ARM = 30


@dataclass(slots=True)
class ArmStats:
    n: int = 0
    recovered: int = 0
    amount_at_risk: Paise = 0
    amount_recovered: Paise = 0

    @property
    def rate(self) -> float:
        return self.recovered / self.n if self.n else 0.0


@dataclass(slots=True)
class Lift:
    treated: ArmStats
    holdout: ArmStats
    lift_pp: float
    ci_pp: float
    incremental_paise: Paise

    @property
    def significant(self) -> bool:
        if self.treated.n < MIN_N_PER_ARM or self.holdout.n < MIN_N_PER_ARM:
            return False
        return abs(self.lift_pp) > self.ci_pp


@dataclass(slots=True)
class Restraint:
    suppressed: int = 0
    escalated: int = 0
    denied_by_rule: dict[str, int] = field(default_factory=dict)
    contacts_sent: int = 0
    opt_outs: int = 0
    # Cases where the network's own advice ruled out retrying at all.
    retry_ruled_out_by_network: int = 0
    # The subset where a retry would have attracted a per-attempt network fee
    # (Mastercard MAC 03 / 21, Visa category 1).
    fee_events_avoided: int = 0
    # B2B promise-to-pay. A made promise is a hold, not a recovery; only kept counts.
    promises_made: int = 0
    promises_kept: int = 0
    promises_broken: int = 0

    def contacts_per_recovery(self, recoveries: int) -> float:
        return self.contacts_sent / recoveries if recoveries else float("inf")


def _stats(cases: list[Case], arm: Arm) -> ArmStats:
    s = ArmStats()
    for c in cases:
        if c.arm is not arm:
            continue
        s.n += 1
        s.amount_at_risk += c.amount_paise
        if c.state is CaseState.RECOVERED:
            s.recovered += 1
            s.amount_recovered += c.amount_paise
    return s


def compute_lift(cases: list[Case]) -> Lift:
    treated = _stats(cases, Arm.TREATED)
    holdout = _stats(cases, Arm.HOLDOUT)

    lift = treated.rate - holdout.rate

    # Agresti-Caffo interval on a difference of two proportions: add one success and
    # one failure to each arm before computing the variance. A plain Wald interval
    # collapses to +/-0 when a rate hits 0 or 1, which reports a 1-vs-1 cohort as
    # significant — the exact overstatement this whole layer exists to prevent.
    def var(s: ArmStats) -> float:
        if not s.n:
            return 0.0
        p_adj = (s.recovered + 1) / (s.n + 2)
        return p_adj * (1 - p_adj) / (s.n + 2)

    ci = Z_95 * math.sqrt(var(treated) + var(holdout))

    # Money attributable: what the treated arm recovered, minus what it would have
    # recovered at the holdout's amount-weighted rate.
    holdout_amount_rate = (
        holdout.amount_recovered / holdout.amount_at_risk if holdout.amount_at_risk else 0.0
    )
    counterfactual = int(treated.amount_at_risk * holdout_amount_rate)
    incremental = treated.amount_recovered - counterfactual

    return Lift(treated, holdout, lift * 100, ci * 100, incremental)


def decompose(cases: list[Case], key) -> dict[str, Lift]:
    """Same lift, sliced. A thin cohort must read as thin, not as a result."""
    buckets: dict[str, list[Case]] = {}
    for c in cases:
        buckets.setdefault(str(key(c)), []).append(c)
    return {k: compute_lift(v) for k, v in sorted(buckets.items())}


def render(
    cases: list[Case], restraint: Restraint, action_cost_paise: Paise, *, llm: Any = None
) -> str:
    """`llm` is the runner's LLMStats (duck-typed here to keep this module free of the
    runner). None or `enabled=False` renders the T3 block as off — still printed, so a
    reader sees that the model was absent rather than wondering where it went."""
    overall = compute_lift(cases)
    t, h = overall.treated, overall.holdout
    out: list[str] = []
    w = out.append

    w("=" * 74)
    w("BACKSTOP BATCH REPORT      (synthetic data - not measured production rates)")
    w("=" * 74)
    w("")
    w("HEADLINE")
    w(f"  cases in batch            {t.n + h.n:>14,}")
    w(f"  amount at risk            {rupees(t.amount_at_risk + h.amount_at_risk):>14}")
    w(f"  recovered gross (treated) {rupees(t.amount_recovered):>14}")
    w(f"  recovered incremental     {rupees(overall.incremental_paise):>14}   <- attributable")
    w(f"  action cost               {rupees(action_cost_paise):>14}")
    w(f"  net                       {rupees(overall.incremental_paise - action_cost_paise):>14}")
    w("")
    w("LIFT vs UNTOUCHED HOLDOUT")
    w(f"  treated   n={t.n:>6,}   recovery {t.rate * 100:>5.1f}%")
    w(f"  holdout   n={h.n:>6,}   recovery {h.rate * 100:>5.1f}%")
    w(f"  lift      {overall.lift_pp:>+5.1f} pp  +/- {overall.ci_pp:.1f}  "
      f"({'significant' if overall.significant else 'NOT significant'} at 95%)")
    w("")
    w("DECOMPOSITION  (lift pp +/- ci, n treated)")
    for label, fn in (
        ("by case type", lambda c: c.case_type.value),
        ("by cause", lambda c: c.cause.value),
        ("by amount band", lambda c: c.amount_band()),
        ("by issuer", lambda c: c.issuer),
        # T3 rows here are the model's contribution, measured the same way as
        # everything else: against the holdout, with an interval.
        ("by diagnosis tier", lambda c: c.tier or "n/a"),
    ):
        w(f"  {label}")
        for name, lf in decompose(cases, fn).items():
            flag = "" if lf.significant else "   (thin)"
            w(f"    {name:<24} {lf.lift_pp:>+6.1f} +/- {lf.ci_pp:>4.1f}"
              f"   n={lf.treated.n:>5,}{flag}")
    w("")
    w("RESTRAINT")
    w(f"  cases suppressed          {restraint.suppressed:>14,}")
    w(f"  cases escalated to human  {restraint.escalated:>14,}")
    w(f"  contacts sent             {restraint.contacts_sent:>14,}")
    w(f"  contacts per recovery     {restraint.contacts_per_recovery(t.recovered):>14.2f}")
    w(f"  customer opt-outs caused  {restraint.opt_outs:>14,}")
    w(f"  retry ruled out by network{restraint.retry_ruled_out_by_network:>14,}")
    w(f"    of which fee events     {restraint.fee_events_avoided:>14,}")
    w(f"  promises to pay obtained  {restraint.promises_made:>14,}")
    w(f"    kept                    {restraint.promises_kept:>14,}")
    w(f"    broken                  {restraint.promises_broken:>14,}")
    w("  denied / deferred by rule")
    for rule_id, count in sorted(restraint.denied_by_rule.items()):
        w(f"    {rule_id:<24} {count:>14,}")
    w("")
    w("MODEL (T3 - free-text residual only)")
    if llm is None or not getattr(llm, "enabled", False):
        residual = getattr(llm, "residual_cases", 0) if llm is not None else 0
        w(f"  status                            off  (NoLLM: residual diagnosed UNKNOWN)")
        w(f"  residual cases            {residual:>14,}")
    else:
        w(f"  model                     {llm.model:>14}")
        w(f"  residual cases            {llm.residual_cases:>14,}")
        w(f"  model calls               {llm.calls:>14,}")
        w(f"  resolved to a cause       {llm.resolved:>14,}")
        w(f"  refusals                  {llm.refusals:>14,}")
        w(f"  tokens in / out           {llm.input_tokens:>7,} / {llm.output_tokens:<6,}")
    w("")
    return "\n".join(out)
