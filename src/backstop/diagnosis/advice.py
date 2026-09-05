"""T0 — what the network told us to do.

The highest-authority signal in the system. A decline response can carry an explicit
instruction from the network, and that instruction outranks anything we inferred from
a decline-code taxonomy or learned from a cohort: the network knows the account is
closed, and it knows when the issuer will next look favourably on the transaction.

Sources — verified 2026-09-05.

  Mastercard Merchant Advice Code (MAC), returned in DE 48 subelement 84
    01  new account information available (fetch updated credential, do not retry)
    02  cannot approve at this time, try again later
    03  do not try again — account closed, fraudulent, or agreement cancelled
    04  token requirements not fulfilled for this token type
    21  payment cancellation — cardholder cancelled the agreement
    24  retry after 1 hour      27  retry after 4 days
    25  retry after 24 hours    28  retry after 6 days
    26  retry after 2 days      29  retry after 8 days
                                30  retry after 10 days
    Retrying after 03 or 21 attracts per-attempt fees under Mastercard's
    Transaction Processing Excellence programme.

  Visa decline response categories
    1  issuer will never approve — no circumstance in which this is approved;
       reattempting attracts a fee
    2  issuer cannot approve at this time — may approve later
    3  correct and retry — the issuer needs updated or additional information
    4  other — generally retryable later
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

HOUR = timedelta(hours=1)
DAY = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class NetworkAdvice:
    network: str
    code: str
    retryable: bool
    reason: str
    # None means the network permitted a retry without specifying when.
    earliest_retry: timedelta | None = None
    # The credential itself is the problem; a retry of the same one cannot work.
    needs_new_credential: bool = False
    # Reattempting after this advice is a fee event, not merely a wasted call.
    penalised_if_retried: bool = False


# code -> (retryable, earliest_retry, needs_new_credential, penalised, reason)
_MAC: dict[str, tuple[bool, timedelta | None, bool, bool, str]] = {
    "01": (False, None, True, False, "new account information available"),
    "02": (True, None, False, False, "cannot approve at this time"),
    "03": (False, None, False, True, "do not try again"),
    "04": (False, None, True, False, "token requirements not fulfilled"),
    "21": (False, None, False, True, "payment cancellation by cardholder"),
    "24": (True, 1 * HOUR, False, False, "retry after 1 hour"),
    "25": (True, 24 * HOUR, False, False, "retry after 24 hours"),
    "26": (True, 2 * DAY, False, False, "retry after 2 days"),
    "27": (True, 4 * DAY, False, False, "retry after 4 days"),
    "28": (True, 6 * DAY, False, False, "retry after 6 days"),
    "29": (True, 8 * DAY, False, False, "retry after 8 days"),
    "30": (True, 10 * DAY, False, False, "retry after 10 days"),
}

_VISA: dict[str, tuple[bool, timedelta | None, bool, bool, str]] = {
    "1": (False, None, False, True, "issuer will never approve"),
    "2": (True, None, False, False, "issuer cannot approve at this time"),
    "3": (False, None, True, False, "correct and retry with updated information"),
    "4": (True, None, False, False, "other — retryable later"),
}

_TABLES = {"mastercard": _MAC, "visa": _VISA}


def parse(network: str | None, code: str | None) -> NetworkAdvice | None:
    """Return the network's instruction, or None when it did not give one.

    An unrecognised code returns None rather than a guess: inventing permission the
    network did not grant is the failure mode this module exists to prevent.
    """
    if not network or not code:
        return None
    table = _TABLES.get(network.lower())
    if table is None:
        return None
    row = table.get(str(code).strip())
    if row is None:
        return None
    retryable, earliest, new_cred, penalised, reason = row
    return NetworkAdvice(
        network=network.lower(),
        code=str(code).strip(),
        retryable=retryable,
        reason=reason,
        earliest_retry=earliest,
        needs_new_credential=new_cred,
        penalised_if_retried=penalised,
    )
