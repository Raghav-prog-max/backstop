"""Choose the next action AND when it fires.

Timing is the larger lever: retrying an insufficient-funds decline the day after
salary credit beats any rewrite of the message that accompanies it. So `wait` is a
first-class action and it is the default one.

What each action is worth is **learned**, not asserted: the score asks
[`ActionModel`](learning.py) for a posterior draw on this action for this kind of
case, and every dispatched result updates it. Nothing in this module encodes how the
simulator behaves, so the lift the report prints is not partly the author's prior
knowledge of the world the agent is being graded in.

The exploration is not decoration — Thompson sampling, plus a small epsilon for the
actions no case has reached yet, generates the variance the measurement layer needs in
order to attribute anything. A purely greedy planner produces a system that cannot
explain its own results.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from ..diagnosis.engine import Diagnosis
from ..diagnosis.taxonomy import NO_RETRY_CAUSES
from ..domain.case import Case
from ..domain.types import CaseType, CauseClass, Channel, MessageClass
from ..policy.config import PolicyConfig
from ..policy.rules import CONTACT_ACTIONS
from .actions import Action, ActionKind
from .learning import ActionModel, ArmKey

MESSAGE_TEMPLATES = ("plain_reminder", "one_tap_link", "value_reminder")

# Actions whose outcome the model learns from. `wait` is never dispatched, and
# `pre_debit_notice` is a compliance step scored by the debit it unlocks — it always
# lands, so an arm for it would learn nothing.
LEARNABLE = frozenset({
    ActionKind.RETRY_PAYMENT,
    ActionKind.REQUEST_REAUTH_LINK,
    ActionKind.SEND_MESSAGE,
    ActionKind.VOICE_CALL,
    ActionKind.OFFER_INSTALLMENT,
    ActionKind.REQUEST_PROMISE_TO_PAY,
})

# Escalation is deliberately not an arm. The only way an escalated case comes back is
# the customer paying on its own, so an arm for it would learn the self-heal rate and
# call it the value of hiring a human — and then buy more of the most expensive action
# in the catalogue. It stays a fallback with a stated value, priced against its cost
# like everything else.
ESCALATION_VALUE = 0.30

# A promise is worth P(the buyer names a date) x P(the buyer keeps it), and those two
# are learned apart. Scoring the ask on dates obtained alone is the "a promise is not
# a recovery" error in bandit form; waiting for the date to fall due before crediting
# anything is worse, because the refusals land immediately and the kept promises land
# a week later, so the arm starves before its wins arrive.
PROMISE_KEPT = "promise_kept"

# The other thing a contact can do: make the customer leave. Learned per stage, like
# everything else, and charged against the action that risks it. Without this the
# planner reads a 5%-per-message recovery rate as a licence to keep sending, buys its
# recoveries with opt-outs, and the only thing standing between it and the customer
# is PR-03's cap. Restraint the agent worked out for itself beats restraint imposed
# on it, because the cap cannot know which message was the one too many.
OPT_OUT = "opt_out"

# A message about a payment that already failed on an existing agreement is a service
# message. A nudge to come back and complete an abandoned cart is an inducement to
# transact, and is promotional.
PROMOTIONAL_CASE_TYPES = frozenset({CaseType.CHECKOUT_ABANDONMENT})


# How far into a case an action falls, capped so the tail stays dense enough to learn
# from. A third message and a sixth are both "late".
STAGE_CAP = 3


def _stage(case: Case, action: Action) -> str:
    if action.kind in CONTACT_ACTIONS:
        return str(min(case.contacts_total, STAGE_CAP))
    if action.kind is ActionKind.RETRY_PAYMENT:
        return f"r{min(case.retries_used, STAGE_CAP)}"
    return ""


def message_class_for(case: Case) -> MessageClass:
    return (
        MessageClass.PROMOTIONAL
        if case.case_type in PROMOTIONAL_CASE_TYPES
        else MessageClass.SERVICE
    )


class Planner:
    def __init__(self, epsilon: float = 0.1, rng: random.Random | None = None,
                 mandate_notice_hours: int | None = None,
                 max_retries: int | None = None,
                 actions: ActionModel | None = None) -> None:
        self.epsilon = epsilon
        self.rng = rng or random.Random(0)
        self.actions = actions if actions is not None else ActionModel()
        # PR-05's lead time and PR-04's merchant budget, read from the policy config
        # rather than restated here.
        cfg = PolicyConfig()
        self.mandate_notice = timedelta(
            hours=mandate_notice_hours if mandate_notice_hours is not None
            else cfg.mandate_notice_hours
        )
        self.max_retries = max_retries if max_retries is not None else cfg.max_retries
        self._cfg = cfg

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

        if case.case_type is CaseType.INVOICE_OVERDUE:
            return self._receivable_candidates(case, dx, now, out)

        # The network's instruction outranks the taxonomy in both directions: it can
        # rule out a retry the cause class would have allowed, and it is the reason a
        # reauth link becomes the sensible action instead.
        advice = dx.advice
        network_forbids = advice is not None and not advice.retryable
        needs_credential = advice is not None and advice.needs_new_credential
        retry_pointless = dx.cause_class in NO_RETRY_CAUSES or network_forbids

        if not retry_pointless:
            fire_at = max(now, dx.retry_window_start)
            if case.case_type is CaseType.MANDATE_LAPSE:
                if case.mandate_notice_sent_at is None:
                    # No debit without notice. Proposing the retry anyway would only
                    # be deferred by PR-05, forever; the notice is the next step —
                    # unless there is no debit left to notice about, in which case
                    # sending one spends money on a debit PR-04 will deny.
                    if case.retries_used < self.max_retries:
                        out.append(Action(ActionKind.PRE_DEBIT_NOTICE, now,
                                          channel=Channel.SMS, template="pre_debit_notice",
                                          message_class=MessageClass.SERVICE))
                else:
                    fire_at = max(fire_at, case.mandate_notice_sent_at + self.mandate_notice)
                    out.append(Action(ActionKind.RETRY_PAYMENT, fire_at))
            else:
                out.append(Action(ActionKind.RETRY_PAYMENT, fire_at))

        if retry_pointless or needs_credential:
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

    def _receivable_candidates(
        self, case: Case, dx: Diagnosis, now: datetime, out: list[Action]
    ) -> list[Action]:
        """B2B receivables. No instrument to retry; every touch is a service message
        about an existing contract; the levers are a reminder, a date, or a human."""
        mc = MessageClass.SERVICE
        overdue = case.days_overdue(now)

        if dx.cause_class is CauseClass.INVOICE_QUERY:
            # A disputed invoice is a conversation, not a dunning sequence. Chasing it
            # burns the relationship for revenue the buyer already says it will not pay
            # as billed. Only a human can resolve it, so that is the only candidate.
            out.append(Action(ActionKind.ESCALATE_HUMAN, now, reason="invoice under query"))
            return out

        if case.promises_broken >= 1 or overdue > 60:
            out.append(Action(
                ActionKind.ESCALATE_HUMAN, now,
                reason=f"{overdue}d overdue, {case.promises_broken} promise(s) broken",
            ))

        out.append(Action(ActionKind.SEND_MESSAGE, now, channel=Channel.EMAIL,
                          template="statement_reminder", message_class=mc))

        # A buyer on its own approval cycle does not need to be asked for a date in
        # the first week; it needs the statement and then patience.
        early_ap_cycle = dx.cause_class is CauseClass.AP_CYCLE and overdue < 10
        if not early_ap_cycle:
            channel = Channel.VOICE if case.amount_paise >= 10_000_000 else Channel.EMAIL
            out.append(Action(ActionKind.REQUEST_PROMISE_TO_PAY, now, channel=channel,
                              template="promise_to_pay", message_class=mc))

        if dx.cause_class is CauseClass.CASH_CONSTRAINED and case.contacts_total >= 1:
            out.append(Action(ActionKind.OFFER_INSTALLMENT, now, channel=Channel.EMAIL,
                              template="installment_plan", message_class=mc))
        return out

    def _score(self, action: Action, case: Case, dx: Diagnosis, now: datetime) -> float:
        """Expected value, in paise, discounted by how long we have to wait for it."""
        if action.kind is ActionKind.WAIT:
            # Waiting is worth something when the retry window has not opened yet.
            return 1.0 if now < dx.retry_window_start else 0.5

        if action.kind is ActionKind.PRE_DEBIT_NOTICE:
            # Worth what the debit it unlocks is worth, from the earliest time that
            # debit could lawfully fire.
            retry = Action(ActionKind.RETRY_PAYMENT,
                           max(now + self.mandate_notice, dx.retry_window_start))
            return self._score(retry, case, dx, now)

        if action.kind is ActionKind.ESCALATE_HUMAN:
            uplift = ESCALATION_VALUE
        elif action.kind is ActionKind.REQUEST_PROMISE_TO_PAY:
            # Dates obtained, discounted by how often a date is honoured.
            uplift = (self.actions.sample(self.arm_for(case, dx, action), self.rng)
                      * self.actions.sample(self.keep_arm_for(case, dx), self.rng))
        elif action.kind in LEARNABLE:
            # A draw from what this action has actually done on cases like this one.
            uplift = self.actions.sample(self.arm_for(case, dx, action), self.rng)
        else:
            uplift = 0.02

        if action.kind is ActionKind.RETRY_PAYMENT:
            # A retry inside its window is worth far more than one outside it.
            in_window = action.fire_at >= dx.retry_window_start
            uplift *= 1.0 if in_window else 0.25

        # Net of what the action costs to take. Without this the planner happily
        # spends a 250-rupee human handover to chase 40 rupees and leaves PR-06 to
        # catch it — a stopping rule is a backstop, not an objective.
        value = case.amount_paise * dx.recoverability * uplift - self._cost(case, action)
        if action.kind in CONTACT_ACTIONS:
            # A customer who opts out takes the rest of the case with them. Posterior
            # mean, not a draw: optimism belongs on the upside, not on the harm.
            p_out = self.actions.estimate(self.opt_out_arm_for(case, dx, action))
            lost = case.amount_paise * (dx.recoverability + self._cfg.optout_lifetime_multiple)
            value -= p_out * lost
        delay_days = max((action.fire_at - now).days, 0)
        return value / (1.0 + 0.05 * delay_days)

    def _cost(self, case: Case, action: Action) -> float:
        """What the action costs, in the same terms PR-06 uses.

        The second term is the one that matters: every contact already on the case
        makes the next one dearer, because a customer's patience is not a renewable
        resource. A planner that prices only the send fee learns to buy recoveries
        with messages, and hands the report a rising contacts-per-recovery.
        """
        cost = float(self._cfg.cost_of(action.kind.value))
        if action.kind in CONTACT_ACTIONS:
            cost += case.contacts_total * self._cfg.goodwill_cost_per_contact_paise
        return cost

    def arm_for(self, case: Case, dx: Diagnosis, action: Action) -> ArmKey:
        """The arm this action falls under. The runner holds on to it for the actions
        that are only settled days later."""
        return self.actions.key(case.case_type.value, dx.cause_class.value,
                                action.kind.value, action.template,
                                _stage(case, action))

    _arm = arm_for

    def opt_out_arm_for(self, case: Case, dx: Diagnosis, action: Action) -> ArmKey:
        """How often a contact like this one, this late, ends the relationship."""
        return self.actions.key(case.case_type.value, dx.cause_class.value,
                                OPT_OUT, action.kind.value, _stage(case, action))

    def keep_arm_for(self, case: Case, dx: Diagnosis) -> ArmKey:
        """The companion arm: of the dates this kind of buyer gives, how many hold."""
        return self.actions.key(case.case_type.value, dx.cause_class.value,
                                PROMISE_KEPT, None)

    def observe(self, case: Case, dx: Diagnosis, action: Action, *, success: bool) -> None:
        """Record what a dispatched action did, as soon as its result is in.

        Success means the action did what it is for: the payment was captured, or the
        buyer named a date. Whether that date is then honoured is the separate
        `promise_kept` arm, settled when it falls due.
        """
        if action.kind not in LEARNABLE:
            return
        self.actions.observe(self.arm_for(case, dx, action), success)

    def observe_contact(self, case: Case, dx: Diagnosis, action: Action,
                        *, opted_out: bool) -> None:
        """Whether this contact cost us the customer. Called with every dispatched
        contact, before the opt-out is allowed to influence the next decision."""
        if action.kind not in CONTACT_ACTIONS:
            return
        self.actions.observe(self.opt_out_arm_for(case, dx, action), opted_out)

    def settle(self, arm: ArmKey, *, success: bool) -> None:
        """Credit an arm whose outcome only arrived later: a promise kept or broken."""
        self.actions.observe(arm, success)
