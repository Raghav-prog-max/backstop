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
from datetime import datetime, timedelta

from ..domain.case import Case
from ..domain.types import CauseClass
from ..execution.outbox import ActionResult
from ..planner.actions import Action, ActionKind
from .generator import latent_cause

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
    # Receivables. An AP cycle pays itself given time; that is the whole reason gross
    # recovery on invoices flatters the agent. A query almost never self-resolves.
    CauseClass.AP_CYCLE: 0.55,
    CauseClass.CASH_CONSTRAINED: 0.14,
    CauseClass.INVOICE_QUERY: 0.04,
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
    "statement_reminder": 0.9,
    "installment_plan": 1.1,
}

# Promise-to-pay: P(buyer commits to a date | asked), and P(kept | committed), by cause.
# A cash-constrained buyer will readily name a date and keep it less often than an AP
# desk that simply needed the statement in front of it.
PROMISE_RATE: dict[CauseClass, tuple[float, float]] = {
    CauseClass.AP_CYCLE: (0.55, 0.80),
    CauseClass.CASH_CONSTRAINED: (0.65, 0.55),
    CauseClass.INVOICE_QUERY: (0.10, 0.30),
    CauseClass.UNKNOWN: (0.35, 0.60),
}
PROMISE_HORIZON_DAYS = (3, 5, 7, 10)


@dataclass(slots=True)
class Latent:
    daily_self_heal: float
    responsiveness: float
    fatigue: float = 0.0
    # Decided when the promise is made, revealed when it falls due.
    will_keep_promise: bool | None = None


class World:
    def __init__(self, horizon_days: int, seed: int = 7) -> None:
        self.horizon_days = horizon_days
        self.rng = random.Random(seed)
        self._latent: dict[str, Latent] = {}
        # The cause the world behaves according to (corpus truth for residual cases).
        self._cause: dict[str, CauseClass] = {}

    def admit(self, case: Case, cause: CauseClass) -> None:
        # A residual case's true cause lives in the generator's corpus, not in whatever
        # the agent diagnosed. Otherwise turning T3 on would change how customers
        # behave, and the measured T3 lift would be a simulator artefact.
        truth = latent_cause(case)
        effective = truth if truth is not None else cause
        total = SELF_HEAL_OVER_HORIZON.get(effective, 0.12)
        # Convert a horizon probability into a per-day hazard.
        daily = 1.0 - (1.0 - total) ** (1.0 / self.horizon_days)
        self._latent[case.case_id] = Latent(
            daily_self_heal=daily,
            responsiveness=self.rng.betavariate(2.2, 2.2),
        )
        self._cause[case.case_id] = effective

    def self_heals_today(self, case: Case) -> bool:
        lat = self._latent[case.case_id]
        return self.rng.random() < lat.daily_self_heal

    def promise_resolves(self, case: Case) -> bool:
        """Called once, when a promise falls due. True = the buyer paid as promised."""
        lat = self._latent[case.case_id]
        kept = bool(lat.will_keep_promise)
        lat.will_keep_promise = None
        if not kept:
            # A broken promise is worse than no promise: the next ask lands colder.
            lat.fatigue += 0.25
        return kept

    def execute(self, case: Case, action: Action, now: datetime) -> ActionResult:
        if action.kind in (ActionKind.WAIT, ActionKind.CLOSE_CASE):
            return ActionResult(ok=True, detail="no-op")

        lat = self._latent[case.case_id]

        if action.kind is ActionKind.REQUEST_PROMISE_TO_PAY:
            commit_p, keep_p = PROMISE_RATE.get(self._cause[case.case_id], (0.35, 0.60))
            commit_p *= max(0.25, 1.0 - lat.fatigue)
            if action.channel is not None and action.channel.value == "voice":
                commit_p *= 1.25  # a call gets a date more often than an email does
            if self.rng.random() < commit_p:
                lat.will_keep_promise = self.rng.random() < keep_p * lat.responsiveness * 1.6
                until = now + timedelta(days=self.rng.choice(PROMISE_HORIZON_DAYS))
                return ActionResult(ok=True, detail=f"{action} -> promised by {until:%Y-%m-%d}",
                                    promise_until=until)
            lat.fatigue += 0.12
            return ActionResult(ok=False, detail=f"{action} no commitment")
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
