"""Core value types. Money is integer paise everywhere — never float."""

from __future__ import annotations

from enum import Enum

Paise = int


def rupees(paise: Paise) -> str:
    return f"Rs {paise / 100:,.2f}"


class CaseType(str, Enum):
    CARD_FAILURE = "card_failure"
    MANDATE_LAPSE = "mandate_lapse"
    CHECKOUT_ABANDONMENT = "checkout_abandonment"
    INVOICE_OVERDUE = "invoice_overdue"


class CauseClass(str, Enum):
    INSUFFICIENT_FUNDS = "insufficient_funds"
    EXPIRED_INSTRUMENT = "expired_instrument"
    ISSUER_UNAVAILABLE = "issuer_unavailable"
    AUTH_ABANDONED = "auth_abandoned"
    RISK_DECLINE = "risk_decline"
    DO_NOT_HONOUR = "do_not_honour"
    MANDATE_NOT_NOTIFIED = "mandate_not_notified"
    UNKNOWN = "unknown"


class CaseState(str, Enum):
    DETECTED = "detected"
    DIAGNOSED = "diagnosed"
    PLANNED = "planned"
    ATTEMPTING = "attempting"
    # terminal
    RECOVERED = "recovered"
    ABANDONED = "abandoned"
    ESCALATED = "escalated"
    SUPPRESSED = "suppressed"


TERMINAL_STATES = frozenset(
    {CaseState.RECOVERED, CaseState.ABANDONED, CaseState.ESCALATED, CaseState.SUPPRESSED}
)


class Channel(str, Enum):
    SMS = "sms"
    WHATSAPP = "whatsapp"
    EMAIL = "email"
    VOICE = "voice"


class MessageClass(str, Enum):
    """TCCCPR 2018 draws the line here, and it changes which rules apply.

    SERVICE     — relates to an existing transaction or relationship (a failed
                  subscription debit, a mandate lapse, an overdue invoice). Not
                  DND-scrubbed, not confined to promotional hours.
    PROMOTIONAL — an inducement to transact (a checkout-abandonment nudge). DND
                  applies, and delivery is confined to the permitted window.
    """

    SERVICE = "service"
    PROMOTIONAL = "promotional"


class Disposition(str, Enum):
    """Four dispositions, deliberately distinct. Collapsing them hides compliance bugs."""

    ALLOW = "allow"
    DENY = "deny"          # this action, now, on this channel
    DEFER = "defer"        # right action, wrong time — requeue
    SUPPRESS = "suppress"  # case not worth working; terminal until new information
    HARD_STOP = "hard_stop"  # cease all contact; terminal and irreversible


class Arm(str, Enum):
    """Experiment assignment. Holdout cases are never touched."""

    TREATED = "treated"
    HOLDOUT = "holdout"
