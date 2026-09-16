"""T2 — cohort posterior over (issuer, instrument, amount band, hour).

Cheap, explainable, and it sharpens as the batch runs. `posterior_n` travels with the
estimate so a thin cohort reads as thin instead of as a result.

The unit of observation is a case-day, not a case. Mid-batch, most open cases have not
failed — they have not finished. Counting only the cases that have resolved would see
the fast recoveries first and the failures last, and the estimate would start near 1.
So the model is Beta-Bernoulli over the *daily* recovery hazard: each day a case is
watched it either recovers or it does not, and a case still open is censored rather
than counted as lost. The hazard is turned back into P(recover over the horizon), the
same scale as the T1 coarse prior it is centred on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import count

CohortKey = tuple[str, str, str, str]


@dataclass(slots=True)
class Posterior:
    mean: float
    n: int
    tier_confident: bool


@dataclass(slots=True)
class _Cell:
    recoveries: float = 0.0
    quiet_days: float = 0.0
    cases: set[str] = field(default_factory=set)


class CohortModel:
    """Beta prior on the daily hazard. Prior strength is deliberately weak."""

    def __init__(self, prior_strength: float = 4.0, min_n: int = 25,
                 horizon_days: int = 14) -> None:
        self._prior_strength = prior_strength
        self._min_n = min_n
        self._horizon_days = horizon_days
        self._cells: dict[CohortKey, _Cell] = {}
        self._anonymous = count()

    @staticmethod
    def key(issuer: str, instrument: str, amount_band: str, hour: int) -> CohortKey:
        return (issuer, instrument, amount_band, _hour_bucket(hour))

    def observe(self, key: CohortKey, recovered: bool, case_id: str | None = None) -> None:
        """One case-day in this cohort: the case recovered that day, or it did not.

        `n` counts distinct cases, so a case watched for ten days is one case, not ten.
        Without a `case_id` every call is taken to be a different case.
        """
        cell = self._cells.setdefault(key, _Cell())
        if recovered:
            cell.recoveries += 1
        else:
            cell.quiet_days += 1
        cell.cases.add(case_id if case_id is not None else f"_anon{next(self._anonymous)}")

    def posterior(self, key: CohortKey, coarse_prior: float) -> Posterior:
        cell = self._cells.get(key) or _Cell()
        h = self._horizon_days
        # Centre the prior on the T1 coarse value, expressed as a daily hazard, worth
        # `prior_strength` cases watched for the whole horizon.
        coarse = min(max(coarse_prior, 1e-6), 1 - 1e-6)
        prior_hazard = 1.0 - (1.0 - coarse) ** (1.0 / h)
        prior_days = self._prior_strength * h
        hazard = (cell.recoveries + prior_hazard * prior_days) / (
            cell.recoveries + cell.quiet_days + prior_days
        )
        mean = 1.0 - (1.0 - hazard) ** h
        n = len(cell.cases)
        return Posterior(mean=mean, n=n, tier_confident=n >= self._min_n)

    def cohorts_seen(self) -> int:
        return len(self._cells)

    def cohorts_confident(self) -> int:
        return sum(1 for c in self._cells.values() if len(c.cases) >= self._min_n)


def _hour_bucket(hour: int) -> str:
    if 6 <= hour < 12:
        return "morning"
    if 12 <= hour < 18:
        return "afternoon"
    if 18 <= hour < 22:
        return "evening"
    return "night"
