"""Tiered diagnosis: taxonomy, then cohort posterior, then (only then) a model.

Every Diagnosis must cite its inputs. A diagnosis that cannot is rejected at the type
boundary — including, especially, a T3 one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from ..domain.case import Case
from ..domain.types import CauseClass
from .cohort import CohortModel
from .taxonomy import COARSE_PRIOR, RETRY_OFFSET_DAYS, classify


@dataclass(frozen=True, slots=True)
class Evidence:
    source_field: str
    raw_value: str
    tier: str


class EvidenceRequired(ValueError):
    pass


@dataclass(slots=True)
class Diagnosis:
    cause_class: CauseClass
    recoverability: float
    posterior_n: int
    retry_window_start: datetime
    tier: str
    evidence: list[Evidence] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.evidence:
            raise EvidenceRequired("a Diagnosis must cite the fields it was derived from")


class LLMDiagnoser(Protocol):
    """T3 seam. Runs on the unstructured residual only — email threads, replies, notes.

    Implementations must return a cause class AND the evidence spans that justify it.
    """

    def diagnose(self, case: Case, free_text: str) -> tuple[CauseClass, list[Evidence]]: ...


class NoLLM:
    """Default T3: refuses rather than guessing. Keeps the model out of the hot path."""

    def diagnose(self, case: Case, free_text: str) -> tuple[CauseClass, list[Evidence]]:
        return CauseClass.UNKNOWN, [
            Evidence("free_text", free_text[:64], "T3-unavailable")
        ]


class DiagnosisEngine:
    def __init__(self, cohort: CohortModel, llm: LLMDiagnoser | None = None) -> None:
        self.cohort = cohort
        self.llm = llm or NoLLM()

    def diagnose(self, case: Case, free_text: str | None = None) -> Diagnosis:
        cause = classify(case.failure_code)
        evidence = [Evidence("failure_code", case.failure_code, "T1")]
        tier = "T1"

        if cause is CauseClass.UNKNOWN and free_text:
            cause, llm_evidence = self.llm.diagnose(case, free_text)
            evidence.extend(llm_evidence)
            tier = "T3"

        coarse = COARSE_PRIOR[cause]
        key = self.cohort.key(
            case.issuer, case.instrument, case.amount_band(), case.created_at.hour
        )
        post = self.cohort.posterior(key, coarse)
        if post.tier_confident:
            tier = "T2"
            evidence.append(Evidence("cohort", "/".join(key), "T2"))

        offset = RETRY_OFFSET_DAYS.get(cause, 2)
        return Diagnosis(
            cause_class=cause,
            recoverability=post.mean,
            posterior_n=post.n,
            retry_window_start=case.created_at + timedelta(days=offset),
            tier=tier,
            evidence=evidence,
        )
