"""Choose the next action AND when it fires.

Timing is the larger lever: retrying an insufficient-funds decline the day after
salary credit beats any rewrite of the message that accompanies it. So `wait` is a
first-class action and it is the default one.

The epsilon exploration is not decoration — it generates the variance the measurement
layer needs in order to attribute anything. A purely greedy planner produces a system
that cannot explain its own results.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from ..diagnosis.engine import Diagnosis
from ..diagnosis.taxonomy import NO_RETRY_CAUSES
from ..domain.case import Case
from ..domain.types import CaseType, Channel, MessageClass
from .actions import Action, ActionKind

MESSAGE_TEMPLATES = ("plain_reminder", "one_tap_link", "value_reminder")

# A message about a payment that already failed on an existing agreement is a service
# message. A nudge to come back and complete an abandoned cart is an inducement to
# transact, and is promotional.
PROMOTIONAL_CASE_TYPES = frozenset({CaseType.CHECKOUT_ABANDONMENT})


def message_class_for(case: Case) -> MessageClass:
    return (
        MessageClass.PROMOTIONAL
        if case.case_type in PROMOTIONAL_CASE_TYPES
        else MessageClass.SERVICE
    )


class Planner:
    def __init__(self, epsilon: float = 0.1, rng: random.Random | None = None) -> None:
        self.epsilon = epsilon
        self.rng = rng or random.Random(0)

    def next_action(self, case: Case, dx: Diagnosis, now: datetime) -> Action:
        candidates = self._candidates(case, dx, now)
        if not candidates:
            return Action(ActionKind.WAIT, now + timedelta(days=1), reason="no candidate")

        if self.rng.random() < self.epsilon:
            return self.rng.choice(candidates)
        return max(candidates, key=lambda a: self._score(a, case, dx, now))

    def _candidates(self, case: Case, dx: Diagnosis, now: datetime) -> list[Action]:
        mc = message_class_for(case)
        out: list[Action] = [Action(ActionKind.WAIT, now + timedelta(days=1))]

        if dx.cause_class not in NO_RETRY_CAUSES:
            fire_at = max(now, dx.retry_window_start)
            out.append(Action(ActionKind.RETRY_PAYMENT, fire_at))

        if dx.cause_class in NO_RETRY_CAUSES:
            # Retry cannot work by construction; the customer has to act.
            out.append(
                Action(
                    ActionKind.REQUEST_REAUTH_LINK, now, channel=Channel.WHATSAPP,
                    template="update_instrument", message_class=mc,
                )
            )

        for template in MESSAGE_TEMPLATES:
            out.append(
                Action(ActionKind.SEND_MESSAGE, now, channel=Channel.WHATSAPP,
                       template=template, message_class=mc)
            )

        # High-value cases earn a human, not a louder robot.
        if case.amount_paise >= 500_000 and case.contacts_total >= 2:
            out.append(Action(ActionKind.ESCALATE_HUMAN, now, reason="high value, unresolved"))

        return out

    def _score(self, action: Action, case: Case, dx: Diagnosis, now: datetime) -> float:
        """Expected value, in paise, discounted by how long we have to wait for it."""
        if action.kind is ActionKind.WAIT:
            # Waiting is worth something when the retry window has not opened yet.
            return 1.0 if now < dx.retry_window_start else 0.5

        uplift = _UPLIFT.get(action.kind, 0.02)
        if action.kind is ActionKind.RETRY_PAYMENT:
            # A retry inside its window is worth far more than one outside it.
            in_window = action.fire_at >= dx.retry_window_start
            uplift *= 1.0 if in_window else 0.25

        value = case.amount_paise * dx.recoverability * uplift
        delay_days = max((action.fire_at - now).days, 0)
        return value / (1.0 + 0.05 * delay_days)


_UPLIFT: dict[ActionKind, float] = {
    ActionKind.RETRY_PAYMENT: 0.35,
    ActionKind.REQUEST_REAUTH_LINK: 0.22,
    ActionKind.SEND_MESSAGE: 0.12,
    ActionKind.VOICE_CALL: 0.20,
    ActionKind.OFFER_INSTALLMENT: 0.18,
    ActionKind.ESCALATE_HUMAN: 0.30,
}
