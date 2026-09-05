"""PR-01 .. PR-08. Ordinary, versioned, unit-tested code. No model inference here.

Each rule is a pure function of (case, action, context) returning a RuleResult or None.
Returning None means "this rule has nothing to say about this action".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from ..domain.case import Case
from ..domain.types import Disposition
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


Rule = Callable[[Case, Action, RuleContext], RuleResult | None]


def pr01_consent(case: Case, action: Action, ctx: RuleContext) -> RuleResult | None:
    if action.kind not in CONTACT_ACTIONS:
        return None
    if ctx.on_dnd_registry or not ctx.has_channel_consent:
        return RuleResult(
            "PR-01", Disposition.DENY, f"no consent for channel {action.channel}"
        )
    return None


def pr02_quiet_hours(case: Case, action: Action, ctx: RuleContext) -> RuleResult | None:
    if action.kind not in CONTACT_ACTIONS:
        return None
    cfg = ctx.config
    hour = ctx.now.hour
    if cfg.quiet_hours_open <= hour < cfg.quiet_hours_close:
        return None
    # Right action, wrong time. Requeued, never dropped.
    next_open = ctx.now.replace(
        hour=cfg.quiet_hours_open, minute=0, second=0, microsecond=0
    )
    if hour >= cfg.quiet_hours_close:
        next_open += timedelta(days=1)
    return RuleResult(
        "PR-02", Disposition.DEFER, "outside merchant contact window", next_open
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
    if case.retries_used >= cfg.max_retries:
        return RuleResult("PR-04", Disposition.DENY, "retry budget spent")
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
    pr02_quiet_hours,
    pr06_economic_floor,
)
