"""The dry-run world.

A simulated executor backend lets a full batch run end to end with zero external side
effects. It is the same code path as production, with the adapter swapped at this
boundary — the pipeline above cannot tell the difference.

The world holds latent per-case truth the agent never sees:
  * a daily self-heal hazard (cards get topped up, customers come back on their own)
  * a responsiveness to intervention

Holdout cases are only ever subject to self-heal. That gap is the lift.

NOTE: these figures are synthetic and calibrated by hand to plausible ranges. They are
not measured production rates and are labelled as such everywhere they surface.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime

from ..domain.case import Case
from ..domain.types import CauseClass
from ..execution.outbox import ActionResult
from ..planner.actions import Action, ActionKind

# Probability a case recovers on its own over the horizon, with no contact at all.
# This is the number that makes gross recovery a lie.
SELF_HEAL_OVER_HORIZON: dict[CauseClass, float] = {
    CauseClass.INSUFFICIENT_FUNDS: 0.34,
    CauseClass.EXPIRED_INSTRUMENT: 0.06,
    CauseClass.ISSUER_UNAVAILABLE: 0.46,
    CauseClass.AUTH_ABANDONED: 0.22,
    CauseClass.RISK_DECLINE: 0.02,
    CauseClass.DO_NOT_HONOUR: 0.18,
    CauseClass.MANDATE_NOT_NOTIFIED: 0.30,
    CauseClass.UNKNOWN: 0.12,
}

# How much an action can move a responsive customer, before timing is applied.
ACTION_POWER: dict[ActionKind, float] = {
    ActionKind.RETRY_PAYMENT: 0.38,
    ActionKind.REQUEST_REAUTH_LINK: 0.26,
    ActionKind.SEND_MESSAGE: 0.11,
    ActionKind.VOICE_CALL: 0.24,
    ActionKind.OFFER_INSTALLMENT: 0.20,
    ActionKind.ESCALATE_HUMAN: 0.33,
}

TEMPLATE_MULTIPLIER: dict[str, float] = {
    "plain_reminder": 0.85,
    "one_tap_link": 1.20,
    "value_reminder": 1.0,
    "update_instrument": 1.0,
}


@dataclass(slots=True)
class Latent:
    daily_self_heal: float
    responsiveness: float
    fatigue: float = 0.0


class World:
    def __init__(self, horizon_days: int, seed: int = 7) -> None:
        self.horizon_days = horizon_days
        self.rng = random.Random(seed)
        self._latent: dict[str, Latent] = {}

    def admit(self, case: Case, cause: CauseClass) -> None:
        total = SELF_HEAL_OVER_HORIZON.get(cause, 0.12)
        # Convert a horizon probability into a per-day hazard.
        daily = 1.0 - (1.0 - total) ** (1.0 / self.horizon_days)
        self._latent[case.case_id] = Latent(
            daily_self_heal=daily,
            responsiveness=self.rng.betavariate(2.2, 2.2),
        )

    def self_heals_today(self, case: Case) -> bool:
        lat = self._latent[case.case_id]
        return self.rng.random() < lat.daily_self_heal

    def execute(self, case: Case, action: Action, now: datetime) -> ActionResult:
        if action.kind in (ActionKind.WAIT, ActionKind.CLOSE_CASE):
            return ActionResult(ok=True, detail="no-op")

        lat = self._latent[case.case_id]
        power = ACTION_POWER.get(action.kind, 0.05)
        power *= TEMPLATE_MULTIPLIER.get(action.template or "", 1.0)
        # Each prior contact makes the next one less effective, not more.
        power *= max(0.25, 1.0 - lat.fatigue)

        p = lat.responsiveness * power
        if self.rng.random() < p:
            return ActionResult(
                ok=True, detail=f"{action} recovered", recovered_paise=case.amount_paise
            )

        if action.kind in (
            ActionKind.SEND_MESSAGE,
            ActionKind.VOICE_CALL,
            ActionKind.REQUEST_REAUTH_LINK,
        ):
            lat.fatigue += 0.18
            # A fatigued customer sometimes just leaves.
            if self.rng.random() < 0.04 * case.contacts_total:
                case.opted_out = True
                return ActionResult(ok=False, detail=f"{action} -> customer opted out")

        return ActionResult(ok=False, detail=f"{action} no response")
