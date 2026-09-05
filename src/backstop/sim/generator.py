"""Synthetic at-risk case generator.

DECLARED SYNTHETIC. The mixes below are hand-calibrated to plausible ranges, not
measured from production. This is stated in the README and on the report screen.
Declared-synthetic costs nothing with a technical panel; undeclared-synthetic
presented as production data costs everything.
"""

from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta

from ..domain.case import Case
from ..domain.types import CaseType, CauseClass

ISSUERS = ("HDFC", "ICICI", "SBI", "AXIS", "KOTAK", "PAYTM")

# Which network an issuer's cards run on.
NETWORK_BY_ISSUER = {
    "HDFC": "visa", "ICICI": "mastercard", "SBI": "visa",
    "AXIS": "mastercard", "KOTAK": "visa", "PAYTM": "mastercard",
}

# What each network plausibly returns alongside a given failure code. Weighted, because
# a real issuer does not always populate the advice field — roughly a third of declines
# arrive with no instruction at all, and the system has to cope with that.
ADVICE_BY_CODE: dict[str, dict[str, tuple[tuple[str | None, float], ...]]] = {
    "insufficient_funds": {
        "mastercard": ((None, .25), ("25", .40), ("26", .25), ("02", .10)),
        "visa": ((None, .30), ("2", .70)),
    },
    "do_not_honour": {
        "mastercard": ((None, .35), ("02", .35), ("21", .10), ("27", .20)),
        "visa": ((None, .35), ("4", .45), ("2", .20)),
    },
    "expired_card": {
        "mastercard": ((None, .20), ("01", .70), ("04", .10)),
        "visa": ((None, .25), ("3", .75)),
    },
    "invalid_card": {
        "mastercard": ((None, .20), ("01", .50), ("03", .30)),
        "visa": ((None, .20), ("3", .40), ("1", .40)),
    },
    "issuer_unavailable": {
        "mastercard": ((None, .30), ("24", .55), ("02", .15)),
        "visa": ((None, .35), ("2", .65)),
    },
    "gateway_timeout": {
        "mastercard": ((None, .45), ("24", .55)),
        "visa": ((None, .50), ("2", .50)),
    },
    "risk_blocked": {
        "mastercard": ((None, .15), ("03", .85)),
        "visa": ((None, .15), ("1", .85)),
    },
    "fraud_suspected": {
        "mastercard": ((None, .10), ("03", .90)),
        "visa": ((None, .10), ("1", .90)),
    },
    "3ds_abandoned": {"mastercard": ((None, 1.0),), "visa": ((None, 1.0),)},
    "otp_not_entered": {"mastercard": ((None, 1.0),), "visa": ((None, 1.0),)},
    "mandate_pre_debit_missing": {
        "mastercard": ((None, .60), ("02", .40)),
        "visa": ((None, .60), ("2", .40)),
    },
}
INSTRUMENTS = ("card", "upi_mandate", "netbanking", "wallet")

# Failure code mix for card failures.
CARD_CODES = (
    ("insufficient_funds", 0.31),
    ("do_not_honour", 0.24),
    ("expired_card", 0.11),
    ("issuer_unavailable", 0.10),
    ("3ds_abandoned", 0.13),
    ("risk_blocked", 0.06),
    ("gateway_timeout", 0.05),
)

MANDATE_CODES = (
    ("insufficient_funds", 0.46),
    ("mandate_pre_debit_missing", 0.19),
    ("do_not_honour", 0.20),
    ("issuer_unavailable", 0.15),
)


def _advice_for(rng: random.Random, code: str, network: str) -> str | None:
    table = ADVICE_BY_CODE.get(code, {}).get(network)
    if not table:
        return None
    r, cum = rng.random(), 0.0
    for value, weight in table:
        cum += weight
        if r <= cum:
            return value
    return table[-1][0]


def _weighted(rng: random.Random, table: tuple[tuple[str, float], ...]) -> str:
    r = rng.random()
    cum = 0.0
    for value, weight in table:
        cum += weight
        if r <= cum:
            return value
    return table[-1][0]


# --- The T3 residual -------------------------------------------------------------
#
# A share of real declines arrive with a gateway code that maps to nothing useful
# ("payment_failed", an unmapped issuer response) but with text attached: the customer
# replied on WhatsApp, support left a note, the customer forwarded a bank SMS. T1 reads
# UNKNOWN; only the text says what happened. This is the population the model sees.
#
# The snippet corpus is the latent truth: `latent_cause()` inverts it, so the simulated
# world's behaviour for a residual case does not depend on whether the model was on.
# Without that, switching T3 on would change customer behaviour, not just diagnosis.

RESIDUAL_SHARE = 0.07

UNMAPPED_CODES = ("payment_failed", "issuer_response_unmapped", "bank_error_91", "BAD_REQUEST_ERROR")

FREE_TEXT_CORPUS: tuple[tuple[str, CauseClass], ...] = (
    ("Salary comes on 1st, balance was low that day. Please retry after 1st.", CauseClass.INSUFFICIENT_FUNDS),
    ("Account had only 200 rs when it tried. Will add money by Friday.", CauseClass.INSUFFICIENT_FUNDS),
    ("customer says limit exhausted on the credit card till statement date", CauseClass.INSUFFICIENT_FUNDS),
    ("My card got blocked last week, bank is sending new one. Old card won't work.", CauseClass.EXPIRED_INSTRUMENT),
    ("This card expired in Aug. I have new card number, how to update?", CauseClass.EXPIRED_INSTRUMENT),
    ("fwd: Dear Customer, your debit card ending 4471 has been deactivated. - HDFC Bank", CauseClass.EXPIRED_INSTRUMENT),
    ("Bank app was showing server busy, will try after some time.", CauseClass.ISSUER_UNAVAILABLE),
    ("Payment page kept loading after UPI PIN and then timed out. Money not debited.", CauseClass.ISSUER_UNAVAILABLE),
    ("support note: SBI netbanking outage 14:00-16:30, several failures in that window", CauseClass.ISSUER_UNAVAILABLE),
    ("I didn't get any OTP so I closed the page.", CauseClass.AUTH_ABANDONED),
    ("app crashed when it asked for UPI PIN, did not complete", CauseClass.AUTH_ABANDONED),
    ("got distracted, will do it tonight", CauseClass.AUTH_ABANDONED),
    ("fwd: Transaction declined due to security reasons. Contact your bank. - ICICI", CauseClass.RISK_DECLINE),
    ("bank called me asking if I did this transaction, they blocked it as suspicious", CauseClass.RISK_DECLINE),
    ("I never got the SMS about this month's debit, that's why it failed. Send notice first.", CauseClass.MANDATE_NOT_NOTIFIED),
    ("no pre-debit notification received for the autopay, bank rejected", CauseClass.MANDATE_NOT_NOTIFIED),
    ("Bank just said declined, no reason given. Tried twice.", CauseClass.DO_NOT_HONOUR),
    ("ok", CauseClass.UNKNOWN),
    ("?", CauseClass.UNKNOWN),
    ("will check and revert", CauseClass.UNKNOWN),
)

_TRUTH_BY_TEXT: dict[str, CauseClass] = {text: cause for text, cause in FREE_TEXT_CORPUS}


def latent_cause(case: Case) -> CauseClass | None:
    """The simulator's ground truth for a residual case, or None for a normal one.

    Only the world may call this. The agent never sees it.
    """
    if case.free_text is None:
        return None
    return _TRUTH_BY_TEXT.get(case.free_text)


def generate(
    n: int, *, start: datetime, seed: int = 42, residual_share: float = RESIDUAL_SHARE
) -> list[Case]:
    rng = random.Random(seed)
    cases: list[Case] = []

    for _ in range(n):
        case_type = (
            CaseType.CARD_FAILURE if rng.random() < 0.62 else CaseType.MANDATE_LAPSE
        )
        codes = CARD_CODES if case_type is CaseType.CARD_FAILURE else MANDATE_CODES
        instrument = "card" if case_type is CaseType.CARD_FAILURE else "upi_mandate"

        # Log-normal-ish amounts: many small, a long tail of large ones.
        rupees = min(round(rng.lognormvariate(6.6, 1.15), 2), 250_000.0)

        issuer = rng.choice(ISSUERS)
        network = NETWORK_BY_ISSUER[issuer]
        failure_code = _weighted(rng, codes)
        advice_code = _advice_for(rng, failure_code, network)
        free_text: str | None = None

        if rng.random() < residual_share:
            # The code says nothing; the text says everything (or nothing).
            failure_code = rng.choice(UNMAPPED_CODES)
            advice_code = None
            free_text, _ = rng.choice(FREE_TEXT_CORPUS)

        cases.append(
            Case(
                case_id=str(uuid.uuid4()),
                case_type=case_type,
                amount_paise=int(rupees * 100),
                customer_ref=f"cust_{rng.randrange(10**7):07d}",
                issuer=issuer,
                instrument=instrument,
                failure_code=failure_code,
                created_at=start + timedelta(minutes=rng.randrange(0, 60 * 24)),
                network=network,
                advice_code=advice_code,
                free_text=free_text,
            )
        )

    return cases
