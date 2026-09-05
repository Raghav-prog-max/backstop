"""T1 — decline-code taxonomy.

Most of a decline is already explained by its code. This tier is a lookup, not a model.

`do_not_honour` is deliberately NOT given a fixed recoverability here: it is the most
common and least informative decline in the book. Its recovery behaviour is learned
per cohort in T2 rather than asserted by anyone.
"""

from __future__ import annotations

from ..domain.types import CauseClass

# Gateway failure code -> cause class.
CODE_TO_CAUSE: dict[str, CauseClass] = {
    "insufficient_funds": CauseClass.INSUFFICIENT_FUNDS,
    "card_declined_low_balance": CauseClass.INSUFFICIENT_FUNDS,
    "expired_card": CauseClass.EXPIRED_INSTRUMENT,
    "invalid_card": CauseClass.EXPIRED_INSTRUMENT,
    "issuer_unavailable": CauseClass.ISSUER_UNAVAILABLE,
    "gateway_timeout": CauseClass.ISSUER_UNAVAILABLE,
    "3ds_abandoned": CauseClass.AUTH_ABANDONED,
    "otp_not_entered": CauseClass.AUTH_ABANDONED,
    "risk_blocked": CauseClass.RISK_DECLINE,
    "fraud_suspected": CauseClass.RISK_DECLINE,
    "do_not_honour": CauseClass.DO_NOT_HONOUR,
    "mandate_pre_debit_missing": CauseClass.MANDATE_NOT_NOTIFIED,
}

# Coarse prior used only until the cohort model has enough observations to speak.
COARSE_PRIOR: dict[CauseClass, float] = {
    CauseClass.INSUFFICIENT_FUNDS: 0.55,
    CauseClass.EXPIRED_INSTRUMENT: 0.20,
    CauseClass.ISSUER_UNAVAILABLE: 0.65,
    CauseClass.AUTH_ABANDONED: 0.40,
    CauseClass.RISK_DECLINE: 0.05,
    CauseClass.DO_NOT_HONOUR: 0.30,
    CauseClass.MANDATE_NOT_NOTIFIED: 0.70,
    CauseClass.UNKNOWN: 0.15,
}

# Days from detection at which a retry is most likely to land, by cause.
# The insufficient-funds window is the payday effect: it is the single largest lever
# in the whole system and it is a timing decision, not a copywriting one.
RETRY_OFFSET_DAYS: dict[CauseClass, int] = {
    CauseClass.INSUFFICIENT_FUNDS: 3,
    CauseClass.ISSUER_UNAVAILABLE: 1,
    CauseClass.DO_NOT_HONOUR: 2,
    CauseClass.AUTH_ABANDONED: 1,
    CauseClass.MANDATE_NOT_NOTIFIED: 2,
    CauseClass.EXPIRED_INSTRUMENT: 0,  # retry is pointless; needs a new instrument
    CauseClass.RISK_DECLINE: 0,        # retry is harmful; never retry a risk decline
    CauseClass.UNKNOWN: 2,
}

# Causes where retrying the same instrument cannot work by construction.
NO_RETRY_CAUSES = frozenset(
    {CauseClass.EXPIRED_INSTRUMENT, CauseClass.RISK_DECLINE}
)


def classify(failure_code: str) -> CauseClass:
    return CODE_TO_CAUSE.get(failure_code, CauseClass.UNKNOWN)
