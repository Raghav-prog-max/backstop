"""The batch report.

Gross recovery answers neither of the track's measurement criteria, because a large
share of at-risk revenue recovers on its own. The reported number is the difference
between the treated arm and an untouched holdout, with an interval, decomposed by
cohort so no single lucky segment can carry it.
"""

from __future__ import annotations

import math
import random
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
    # Half-width of the 95% bootstrap interval on the money, when one was asked for.
    # Zero means it was not computed, not that the estimate is exact.
    incremental_ci_paise: Paise = 0

    @property
    def money_significant(self) -> bool:
        """Whether the rupee figure clears its own interval. It usually does not at a
        10% holdout, and saying so is the point of printing it."""
        if not self.incremental_ci_paise:
            return False
        return abs(self.incremental_paise) > self.incremental_ci_paise

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


def compute_lift(cases: list[Case], *, money_ci_draws: int = 0) -> Lift:
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

    incremental = _incremental(cases)
    money_ci = _incremental_ci(cases, draws=money_ci_draws) if money_ci_draws else 0

    return Lift(treated, holdout, lift * 100, ci * 100, incremental, money_ci)


# Amount strata for the money estimate, as quantiles of this batch's own amounts. A
# fixed band table would put every B2B invoice in one bucket spanning two orders of
# magnitude, which is the bucket that does the damage.
MONEY_STRATA = 10
# Below this many holdout cases a stratum has no usable counterfactual of its own and
# falls back to the pooled one.
MIN_STRATUM_HOLDOUT = 5


def _strata_bounds(cases: list[Case]) -> list[Paise]:
    amounts = sorted(c.amount_paise for c in cases)
    if not amounts:
        return []
    step = max(len(amounts) // MONEY_STRATA, 1)
    return sorted({amounts[i] for i in range(step, len(amounts), step)})


def _stratum_of(amount: Paise, bounds: list[Paise]) -> int:
    lo, hi = 0, len(bounds)
    while lo < hi:
        mid = (lo + hi) // 2
        if amount < bounds[mid]:
            hi = mid
        else:
            lo = mid + 1
    return lo


def _incremental(cases: list[Case], bounds: list[Paise] | None = None) -> Paise:
    """Money attributable to the agent, post-stratified by amount.

    The obvious estimator — treated recoveries minus the holdout's amount-weighted
    rate applied to the treated arm — divides by a holdout of a few hundred cases
    whose amounts span four orders of magnitude. Whether two or three large invoices
    happened to land in the holdout and happened to self-pay then moves the headline
    by crores, which is how a number that is mostly sampling noise ends up on a slide.

    Stratifying by amount first takes that out: within a stratum the amounts are
    comparable, so a difference in recovery *rates* can be priced at the treated
    arm's own money. A stratum too thin to have a counterfactual of its own borrows
    the pooled one rather than inventing a local miracle.
    """
    if bounds is None:
        bounds = _strata_bounds(cases)

    pooled_t = _stats(cases, Arm.TREATED)
    pooled_h = _stats(cases, Arm.HOLDOUT)
    pooled_delta = pooled_t.rate - pooled_h.rate

    buckets: dict[int, list[Case]] = {}
    for c in cases:
        buckets.setdefault(_stratum_of(c.amount_paise, bounds), []).append(c)

    total = 0.0
    for group in buckets.values():
        t = _stats(group, Arm.TREATED)
        h = _stats(group, Arm.HOLDOUT)
        delta = t.rate - h.rate if h.n >= MIN_STRATUM_HOLDOUT else pooled_delta
        total += delta * t.amount_at_risk
    return int(total)


def _incremental_ci(cases: list[Case], draws: int = 400, seed: int = 20_260_911) -> Paise:
    """Half-width of a 95% percentile bootstrap interval on the money estimate.

    Resampled within arm, because the arms are fixed by design and it is the cases in
    them that are the sample. A money figure printed without this reads as precision
    the batch does not have.
    """
    treated = [c for c in cases if c.arm is Arm.TREATED]
    holdout = [c for c in cases if c.arm is Arm.HOLDOUT]
    if len(treated) < 2 or len(holdout) < 2:
        return 0

    bounds = _strata_bounds(cases)
    rng = random.Random(seed)
    estimates = []
    for _ in range(draws):
        sample = rng.choices(treated, k=len(treated)) + rng.choices(holdout, k=len(holdout))
        estimates.append(_incremental(sample, bounds))
    estimates.sort()
    lo = estimates[int(0.025 * (draws - 1))]
    hi = estimates[int(0.975 * (draws - 1))]
    return int((hi - lo) / 2)


def decompose(cases: list[Case], key) -> dict[str, Lift]:
    """Same lift, sliced. A thin cohort must read as thin, not as a result."""
    buckets: dict[str, list[Case]] = {}
    for c in cases:
        buckets.setdefault(str(key(c)), []).append(c)
    return {k: compute_lift(v) for k, v in sorted(buckets.items())}


def render(
    cases: list[Case], restraint: Restraint, action_cost_paise: Paise, *,
    llm: Any = None, actions: list | None = None
) -> str:
    """`llm` is the runner's LLMStats (duck-typed here to keep this module free of the
    runner). None or `enabled=False` renders the T3 block as off — still printed, so a
    reader sees that the model was absent rather than wondering where it went."""
    overall = compute_lift(cases, money_ci_draws=400)
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
    w(f"  recovered incremental     {rupees(overall.incremental_paise):>14}"
      f"  +/- {rupees(overall.incremental_ci_paise)}  <- attributable")
    if overall.incremental_ci_paise and not overall.money_significant:
        # The headline number a deck would quote, and the reason not to quote it.
        # Recovery rates are counts; rupees are counts times a heavy-tailed amount,
        # and a few hundred holdout cases cannot pin that down. The percentage below
        # can be, and is.
        w("                            (interval spans zero - read the pp lift below,"
          " not this)")
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
    if actions:
        # The table that used to be hand-set constants, printed so it can be argued
        # with: what each action did in this batch, and what the planner now believes.
        value = [a for a in actions if not a[0].startswith("opt_out/")]
        harm = [a for a in actions if a[0].startswith("opt_out/") and a[1] >= 50]
        w("WHAT THE PLANNER LEARNED  (from this batch's own outcomes, no priors set by hand)")
        w(f"    {'action  (#n = contacts already made)':<40}{'used':>9}{'worked':>9}{'estimate':>11}")
        for label, trials, rate, mean in value[:12]:
            w(f"    {label:<40}{trials:>9,}{rate * 100:>8.1f}%{mean:>10.1%}")
        if harm:
            w("  and what it cost: contacts that ended the relationship")
            for label, trials, rate, _ in harm:
                w(f"    {label[len('opt_out/'):]:<40}{trials:>9,}{rate * 100:>8.1f}%")
        w("")
    return "\n".join(out)
