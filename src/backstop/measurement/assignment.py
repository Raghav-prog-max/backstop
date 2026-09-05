"""Randomised holdout assignment.

Deterministic on case_id so a rerun assigns identically — the experiment is
reproducible, and re-running the batch cannot quietly reshuffle arms until the
number looks better.
"""

from __future__ import annotations

import hashlib

from ..domain.types import Arm


def assign(case_id: str, holdout_fraction: float = 0.10, salt: str = "backstop-v1") -> Arm:
    digest = hashlib.sha256(f"{salt}:{case_id}".encode()).digest()
    bucket = int.from_bytes(digest[:8], "big") / 2**64
    return Arm.HOLDOUT if bucket < holdout_fraction else Arm.TREATED
