"""PR-01 .. PR-08. Ordinary, versioned, unit-tested code. No model inference here.

Each rule is a pure function of (case, action, context) returning a RuleResult or None.
Returning None means "this rule has nothing to say about this action".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from ..diagnosis.advice import NetworkAdvice
from ..domain.case import Case
from ..domain.types import Disposition, MessageClass
from ..planner.actions import Action, ActionKind
from .config import PolicyConfig

CONTACT_ACTIONS = frozenset(
    {ActionKind.SEND_MESSAGE, ActionKind.VOICE_CALL, ActionKind.REQUEST_REAUTH_LINK}
)


@dataclass(frozen=True, slots=True)
class RuleResult:
    rule_id: str
    disposition: Disposition
    reason: str
    defer_until: datetime | None = None


@dataclass(frozen=True, slots=True)
class RuleContext:
    now: datetime
    config: PolicyConfig
    expected_recovery_paise: int
    last_retry_at: datetime | None = None
    on_dnd_registry: bool = False
    has_channel_consent: bool = True
    mandate_notice_sent_at: datetime | None = None
    afa_completed: bool = False
    mandate_category: str | None = None
    network: str | None = None
    retries_in_network_window: int = 0
    # T0 — what the network told us. Outranks everything we inferred.
    advice: NetworkAdvice | None = None
    last_decline_at: datetime | None = None


Rule = Callable[[Case, Action, RuleContext], RuleResult | None]


def pr01_consent(case: Case, action: Action, ctx: RuleContext) -> RuleResult | None:
    """DND scrubbing applies to promotional communication, not to service messages.

    Telling a customer their subscription debit failed is a service message about an
    existing relationship. Suppressing it because the number is DND-registered would
    be both wrong under TCCCPR and expensive.
    """
    if action.kind not in CONTACT_ACTIONS:
        return None
    if not ctx.has_channel_consent:
        return RuleResult(
            "PR-01", Disposition.DENY, f"no consent for channel {action.channel}"
        )
    if ctx.on_dnd_registry and action.message_class is MessageClass.PROMOTIONAL:
        return RuleResult(
            "PR-01", Disposition.DENY, "promotional contact to a DND-registered number"
        )
    return None


def pr02_promo_hours(case: Case, action: Action, ctx: RuleContext) -> RuleResult | None:
    """The 10:00-21:00 IST window binds promotional communication only.

    A voice call is treated as promotional-hours-bound regardless of class: nobody
    wants a collections call at 2am, whatever the regulation permits.
    """
    if action.kind not in CONTACT_ACTIONS:
        return None
    bound = (
        action.message_class is MessageClass.PROMOTIONAL
        or action.kind is ActionKind.VOICE_CALL
    )
    if not bound:
        return None
    cfg = ctx.config
    hour = ctx.now.hour
    if cfg.promo_hours_open <= hour < cfg.promo_hours_close:
        return None
    # Right action, wrong time. Requeued, never dropped.
    next_open = ctx.now.replace(
        hour=cfg.promo_hours_open, minute=0, second=0, microsecond=0
    )
    if hour >= cfg.promo_hours_close:
        next_open += timedelta(days=1)
    return RuleResult(
        "PR-02", Disposition.DEFER, "outside permitted contact window", next_open
    )


def pr03_freq_cap(case: Case, action: Action, ctx: RuleContext) -> RuleResult | None:
    if action.kind not in CONTACT_ACTIONS or action.channel is None:
        return None
    cfg = ctx.config
    if case.contacts_by_channel.get(action.channel, 0) >= cfg.max_contacts_per_channel:
        return RuleResult("PR-03", Disposition.DENY, "per-channel contact cap reached")
    if case.contacts_total >= cfg.max_contacts_total:
        return RuleResult("PR-03", Disposition.DENY, "total contact cap reached")
    return None


def pr04_retry_ceiling(case: Case, action: Action, ctx: RuleContext) -> RuleResult | None:
    """Denies the retry only. Other actions on the case are unaffected."""
    if action.kind is not ActionKind.RETRY_PAYMENT:
        return None
    cfg = ctx.config

    # The network's instruction comes first. Retrying against a "do not try again"
    # is not merely wasted — it is a per-attempt fee and, after enough of them, a
    # non-compliance assessment.
    advice = ctx.advice
    if advice is not None and not advice.retryable:
        return RuleResult(
            "PR-04",
            Disposition.DENY,
            f"{advice.network} advice {advice.code}: {advice.reason}"
            + (" (reattempt is a fee event)" if advice.penalised_if_retried else ""),
        )

    ceiling = cfg.network_ceiling_for(ctx.network)
    if ctx.retries_in_network_window >= ceiling:
        return RuleResult(
            "PR-04",
            Disposition.DENY,
            f"{ctx.network or 'network'} reattempt ceiling ({ceiling} in "
            f"{cfg.network_retry_window_days}d) reached",
        )
    if case.retries_used >= cfg.max_retries:
        return RuleResult("PR-04", Disposition.DENY, "merchant retry budget spent")
    # An explicit "retry after N" is an instruction, not a suggestion.
    if advice is not None and advice.earliest_retry is not None:
        since = ctx.last_decline_at or ctx.last_retry_at
        if since is not None:
            not_before = since + advice.earliest_retry
            if ctx.now < not_before:
                return RuleResult(
                    "PR-04",
                    Disposition.DEFER,
                    f"{advice.network} advice {advice.code}: {advice.reason}",
                    not_before,
                )

    if ctx.last_retry_at is not None:
        gap = ctx.now - ctx.last_retry_at
        if gap < timedelta(hours=cfg.min_hours_between_retries):
            return RuleResult(
                "PR-04",
                Disposition.DEFER,
                "minimum retry spacing not met",
                ctx.last_retry_at + timedelta(hours=cfg.min_hours_between_retries),
            )
    return None


def pr05_mandate_notice(case: Case, action: Action, ctx: RuleContext) -> RuleResult | None:
    if action.kind is not ActionKind.RETRY_PAYMENT:
        return None
    if case.case_type.value != "mandate_lapse":
        return None
    cfg = ctx.config
    sent = ctx.mandate_notice_sent_at
    due = ctx.now - timedelta(hours=cfg.mandate_notice_hours)
    if sent is None or sent > due:
        serve_at = (sent or ctx.now) + timedelta(hours=cfg.mandate_notice_hours)
        return RuleResult(
            "PR-05", Disposition.DEFER, "pre-debit notification lead time not met", serve_at
        )
    # Above the AFA ceiling the debit needs authentication; a silent retry cannot
    # supply it, so the case has to go to the customer rather than to the network.
    ceiling = cfg.afa_ceiling_for(ctx.mandate_category)
    if case.amount_paise > ceiling and not ctx.afa_completed:
        return RuleResult(
            "PR-05",
            Disposition.DENY,
            f"amount exceeds AFA ceiling ({ceiling}p) and no authentication on file",
        )
    return None


def pr06_economic_floor(case: Case, action: Action, ctx: RuleContext) -> RuleResult | None:
    """The rule that removes 'contact everyone and see what sticks'."""
    if action.kind in (ActionKind.WAIT, ActionKind.CLOSE_CASE, ActionKind.ESCALATE_HUMAN):
        return None
    cfg = ctx.config
    action_cost = cfg.cost_of(action.kind.value)
    goodwill = case.contacts_total * cfg.goodwill_cost_per_contact_paise
    if ctx.expected_recovery_paise <= action_cost + goodwill:
        return RuleResult(
            "PR-06",
            Disposition.SUPPRESS,
            f"expected {ctx.expected_recovery_paise}p <= cost {action_cost + goodwill}p",
        )
    return None


def pr07_hard_stop(case: Case, action: Action, ctx: RuleContext) -> RuleResult | None:
    if case.opted_out:
        return RuleResult("PR-07", Disposition.HARD_STOP, "customer opted out")
    if case.dispute_open:
        return RuleResult("PR-07", Disposition.HARD_STOP, "dispute or chargeback open")
    return None


def pr08_promise_hold(case: Case, action: Action, ctx: RuleContext) -> RuleResult | None:
    if case.promise_until is None:
        return None
    release = case.promise_until + timedelta(hours=ctx.config.promise_grace_hours)
    if ctx.now < release:
        return RuleResult(
            "PR-08", Disposition.DEFER, "promise-to-pay in force", release
        )
    return None


# Order matters: hard stops are evaluated before anything that could contact a customer.
RULES: tuple[Rule, ...] = (
    pr07_hard_stop,
    pr01_consent,
    pr08_promise_hold,
    pr05_mandate_notice,
    pr04_retry_ceiling,
    pr03_freq_cap,
    pr02_promo_hours,
    pr06_economic_floor,
)
