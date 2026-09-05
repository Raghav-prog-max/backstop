"""T2 — Beta-Bernoulli cohort posterior over (issuer, instrument, amount band, hour).

Cheap, explainable, and it sharpens as the batch runs. `posterior_n` travels with the
estimate so a thin cohort reads as thin instead of as a result.
"""

from __future__ import annotations

from dataclasses import dataclass

CohortKey = tuple[str, str, str, str]


@dataclass(slots=True)
class Posterior:
    mean: float
    n: int
    tier_confident: bool


class CohortModel:
    """Beta(alpha, beta) per cohort. Prior strength is deliberately weak."""

    def __init__(self, prior_strength: float = 4.0, min_n: int = 25) -> None:
        self._prior_strength = prior_strength
        self._min_n = min_n
        self._counts: dict[CohortKey, list[float]] = {}

    @staticmethod
    def key(issuer: str, instrument: str, amount_band: str, hour: int) -> CohortKey:
        return (issuer, instrument, amount_band, _hour_bucket(hour))

    def observe(self, key: CohortKey, recovered: bool) -> None:
        a, b = self._counts.setdefault(key, [0.0, 0.0])
        if recovered:
            self._counts[key][0] = a + 1
        else:
            self._counts[key][1] = b + 1

    def posterior(self, key: CohortKey, coarse_prior: float) -> Posterior:
        a, b = self._counts.get(key, (0.0, 0.0))
        n = int(a + b)
        # Centre the prior on the T1 coarse value, weighted by prior_strength.
        pa = coarse_prior * self._prior_strength
        pb = (1.0 - coarse_prior) * self._prior_strength
        mean = (a + pa) / (a + b + pa + pb)
        return Posterior(mean=mean, n=n, tier_confident=n >= self._min_n)

    def cohorts_seen(self) -> int:
        return len(self._counts)


def _hour_bucket(hour: int) -> str:
    if 6 <= hour < 12:
        return "morning"
    if 12 <= hour < 18:
        return "afternoon"
    if 18 <= hour < 22:
        return "evening"
    return "night"
