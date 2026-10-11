"""What an action is worth, learned from outcomes rather than asserted.

The planner used to score actions with a table of hand-set uplifts. Two problems with
that. The small one is that the numbers were guesses. The large one is that they were
guesses tuned against this repo's own simulator, so a reviewer cannot tell how much of
the reported lift is the agent deciding well and how much is the author having known
the answer. A learned table has neither problem: it starts deliberately ignorant and
every number it ends with was paid for by an observed outcome.

The estimate is a Beta posterior on "this action, on this kind of case, produced the
result it exists to produce" — a retry that captured the payment, a message that got
the customer to pay, an ask that obtained a date. What counts as success differs by
action, exactly as the old constants did; arms are only ever compared within the same
decision, where that difference is already priced into the action's own history.

Two levels, because `(case type, cause, action, template)` is thin early on:

    (kind, template)                       -> the general arm
    (case_type, cause, kind, template)     -> the specific arm, centred on the general

A specific arm with no data of its own reads as its general arm; one with plenty
overrides it. `prior_mean` is a flat, uninformative starting point applied to the
general arms — not a per-action guess.

Selection samples from the posterior (Thompson) rather than taking its mean, so an arm
that is merely untried keeps being tried. That is also what keeps the measurement layer
fed: variance in what the planner does is what lets lift be attributed at all.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass

# (case_type, cause, kind, template, stage) — "" for the wildcards on the general arm.
# `stage` is how much has already been spent on this case: the number of contacts it
# has had, or retries already attempted. Without it the model learns one number for
# "a message" and never discovers that the fifth one does not work like the first.
ArmKey = tuple[str, str, str, str, str]


@dataclass(slots=True)
class Arm:
    successes: float = 0.0
    trials: float = 0.0

    @property
    def rate(self) -> float:
        return self.successes / self.trials if self.trials else 0.0


class ActionModel:
    def __init__(self, prior_mean: float = 0.08, prior_strength: float = 8.0,
                 backoff_strength: float = 12.0) -> None:
        # One flat prior for every arm: the model is not told which actions work.
        self.prior_mean = prior_mean
        self.prior_strength = prior_strength
        # How much specific arms lean on their general arm before their own data
        # outweighs it, in pseudo-trials.
        self.backoff_strength = backoff_strength
        self._arms: dict[ArmKey, Arm] = {}

    @staticmethod
    def key(case_type: str, cause: str, kind: str, template: str | None,
            stage: str = "") -> ArmKey:
        return (case_type, cause, kind, template or "", stage)

    @staticmethod
    def general(key: ArmKey) -> ArmKey:
        return ("", "", key[2], key[3], key[4])

    def observe(self, key: ArmKey, success: bool) -> None:
        """One dispatched action and what came back. Both levels see it."""
        for k in (self.general(key), key):
            arm = self._arms.setdefault(k, Arm())
            arm.trials += 1
            if success:
                arm.successes += 1

    def _general_posterior(self, key: ArmKey) -> tuple[float, float]:
        arm = self._arms.get(self.general(key)) or Arm()
        a = arm.successes + self.prior_mean * self.prior_strength
        b = (arm.trials - arm.successes) + (1 - self.prior_mean) * self.prior_strength
        return a, b

    def posterior(self, key: ArmKey) -> tuple[float, float]:
        """Beta(a, b) for the specific arm, centred on its general arm."""
        ga, gb = self._general_posterior(key)
        centre = ga / (ga + gb)
        arm = self._arms.get(key) or Arm()
        a = arm.successes + centre * self.backoff_strength
        b = (arm.trials - arm.successes) + (1 - centre) * self.backoff_strength
        return a, b

    def estimate(self, key: ArmKey) -> float:
        a, b = self.posterior(key)
        return a / (a + b)

    def sample(self, key: ArmKey, rng: random.Random) -> float:
        """A draw from the posterior. Untried arms are wide, so they get their turn."""
        a, b = self.posterior(key)
        return rng.betavariate(a, b)

    def trials(self, key: ArmKey) -> int:
        arm = self._arms.get(key)
        return int(arm.trials) if arm else 0

    def to_json(self) -> str:
        """The learned table, as text. A batch's education is worth keeping: a fresh
        model pays for its own out of the first batch's revenue."""
        return json.dumps({"prior_mean": self.prior_mean,
                           "prior_strength": self.prior_strength,
                           "backoff_strength": self.backoff_strength,
                           "arms": [[list(k), a.successes, a.trials]
                                    for k, a in self._arms.items()]})

    @classmethod
    def from_json(cls, text: str) -> "ActionModel":
        raw = json.loads(text)
        model = cls(prior_mean=raw["prior_mean"], prior_strength=raw["prior_strength"],
                    backoff_strength=raw["backoff_strength"])
        for key, successes, trials in raw["arms"]:
            model._arms[tuple(key)] = Arm(successes=successes, trials=trials)
        return model

    def arms_learned(self) -> int:
        return sum(1 for k in self._arms if k[0] or k[1])

    def general_arms(self) -> list[tuple[str, int, float, float]]:
        """(label, trials, observed rate, posterior mean) per general arm, busiest first.

        What the planner believes about each action, and on what evidence — the table
        that used to be a constant, printed so it can be argued with.
        """
        rows = []
        for k, arm in self._arms.items():
            if k[0] or k[1]:
                continue
            label = k[2] + (f"/{k[3]}" if k[3] else "") + (f" #{k[4]}" if k[4] else "")
            rows.append((label, int(arm.trials), arm.rate, self.estimate(k)))
        rows.sort(key=lambda r: -r[1])
        return rows
