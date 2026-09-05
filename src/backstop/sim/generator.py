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
from ..domain.types import CaseType

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


def generate(n: int, *, start: datetime, seed: int = 42) -> list[Case]:
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
                advice_code=_advice_for(rng, failure_code, network),
            )
        )

    return cases
