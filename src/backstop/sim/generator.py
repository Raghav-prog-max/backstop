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

        cases.append(
            Case(
                case_id=str(uuid.uuid4()),
                case_type=case_type,
                amount_paise=int(rupees * 100),
                customer_ref=f"cust_{rng.randrange(10**7):07d}",
                issuer=rng.choice(ISSUERS),
                instrument=instrument,
                failure_code=_weighted(rng, codes),
                created_at=start + timedelta(minutes=rng.randrange(0, 60 * 24)),
            )
        )

    return cases
