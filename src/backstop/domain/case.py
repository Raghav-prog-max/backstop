"""The case projection and its state machine.

`Case` is derived state — rebuildable from the ledger at any time. If it is ever
wrong it gets rebuilt, never patched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .types import Arm, CaseState, CaseType, CauseClass, Channel, Paise, TERMINAL_STATES

# Suppression is reachable from DIAGNOSED, not from ATTEMPTING: a suppressed case is a
# decision the system made, not an attempt that failed. Mixing them corrupts reporting.
TRANSITIONS: dict[CaseState, frozenset[CaseState]] = {
    CaseState.DETECTED: frozenset({CaseState.DIAGNOSED, CaseState.SUPPRESSED}),
    CaseState.DIAGNOSED: frozenset(
        {CaseState.PLANNED, CaseState.SUPPRESSED, CaseState.ESCALATED}
    ),
    CaseState.PLANNED: frozenset(
        {CaseState.ATTEMPTING, CaseState.SUPPRESSED, CaseState.ABANDONED}
    ),
    CaseState.ATTEMPTING: frozenset(
        {
            CaseState.PLANNED,  # wait -> next attempt
            CaseState.RECOVERED,
            CaseState.ABANDONED,
            CaseState.ESCALATED,
        }
    ),
}


class IllegalTransition(Exception):
    pass


@dataclass(slots=True)
class RevenueAtRiskEvent:
    """One normalised signal, whatever surface it arrived from."""

    case_id: str
    case_type: CaseType
    amount_paise: Paise
    customer_ref: str
    issuer: str
    instrument: str
    failure_code: str
    occurred_at: datetime
    network: str | None = None
    advice_code: str | None = None
    merchant_id: str = "merch_demo"


@dataclass(slots=True)
class Case:
    case_id: str
    case_type: CaseType
    amount_paise: Paise
    customer_ref: str
    issuer: str
    instrument: str
    failure_code: str
    created_at: datetime
    # What the network said, if it said anything. Parsed in diagnosis/advice.py.
    network: str | None = None
    advice_code: str | None = None
    arm: Arm = Arm.TREATED
    state: CaseState = CaseState.DETECTED
    cause: CauseClass = CauseClass.UNKNOWN
    recoverability: float = 0.0

    retries_used: int = 0
    contacts_by_channel: dict[Channel, int] = field(default_factory=dict)
    opted_out: bool = False
    dispute_open: bool = False
    promise_until: datetime | None = None
    next_action_at: datetime | None = None

    terminal_reason: str | None = None
    stopping_rule: str | None = None
    recovered_at: datetime | None = None

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def contacts_total(self) -> int:
        return sum(self.contacts_by_channel.values())

    def transition(self, to: CaseState, *, reason: str | None = None,
                   rule_id: str | None = None) -> None:
        allowed = TRANSITIONS.get(self.state, frozenset())
        if to not in allowed:
            raise IllegalTransition(f"{self.case_id}: {self.state.value} -> {to.value}")
        self.state = to
        if to in TERMINAL_STATES:
            self.terminal_reason = reason
            self.stopping_rule = rule_id

    def record_contact(self, channel: Channel) -> None:
        self.contacts_by_channel[channel] = self.contacts_by_channel.get(channel, 0) + 1

    def amount_band(self) -> str:
        rupees = self.amount_paise / 100
        if rupees < 500:
            return "lt_500"
        if rupees < 2_000:
            return "500_2k"
        if rupees < 10_000:
            return "2k_10k"
        return "gte_10k"
