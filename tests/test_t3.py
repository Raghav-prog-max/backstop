"""T3 — the model tier. No network in tests: the SDK call is behind a fake.

What these pin down:
  * the model cannot return a cause without a quote that is actually in the text
  * the residual population is exactly "unmapped code + free text", nothing else
  * the simulated world's behaviour for a residual case does not change when T3 is on
  * the runner records model + evidence in the ledger for a T3 diagnosis
"""

from __future__ import annotations

import json
from datetime import datetime

from backstop.diagnosis.cohort import CohortModel
from backstop.diagnosis.engine import DiagnosisEngine, Evidence
from backstop.diagnosis.llm import ClaudeDiagnoser, parse_t3
from backstop.diagnosis.taxonomy import CODE_TO_CAUSE
from backstop.domain.case import Case
from backstop.domain.events import EventKind
from backstop.domain.types import CaseType, CauseClass
from backstop.runner import run
from backstop.sim.generator import FREE_TEXT_CORPUS, generate, latent_cause
from backstop.sim.world import World

NOW = datetime(2026, 9, 10, 12, 0)


def residual_case(text: str = "Salary comes on 1st, balance was low that day. Please retry after 1st.") -> Case:
    return Case(
        case_id="r1", case_type=CaseType.CARD_FAILURE, amount_paise=180_000,
        customer_ref="cust_1", issuer="HDFC", instrument="card",
        failure_code="payment_failed", created_at=NOW, network="visa", free_text=text,
    )


class FakeLLM:
    """Answers from the corpus, the way a perfect T3 would. Cites the whole text."""

    model = "fake-t3"

    def __init__(self) -> None:
        self.calls = 0

    def diagnose(self, case: Case, free_text: str) -> tuple[CauseClass, list[Evidence]]:
        self.calls += 1
        truth = latent_cause(case) or CauseClass.UNKNOWN
        return truth, [Evidence("free_text", free_text, "T3")]


# --- parse_t3: grounding is enforced, not requested ---------------------------------

def test_parse_accepts_a_grounded_cause():
    text = "Bank app was showing server busy, will try after some time."
    raw = json.dumps({
        "cause_class": "issuer_unavailable", "confidence": 0.9,
        "evidence": [{"quote": "server busy", "why": "issuer side"}],
        "customer_intent": "will_pay",
    })
    call = parse_t3(raw, text)
    assert call.cause is CauseClass.ISSUER_UNAVAILABLE
    assert call.evidence[0].raw_value == "server busy" and call.evidence[0].tier == "T3"


def test_parse_rejects_a_cause_whose_quote_is_not_in_the_text():
    """A confident answer with fabricated evidence is UNKNOWN, whatever the confidence."""
    text = "will check and revert"
    raw = json.dumps({
        "cause_class": "insufficient_funds", "confidence": 0.95,
        "evidence": [{"quote": "balance was low", "why": "says so"}],
        "customer_intent": "will_pay",
    })
    call = parse_t3(raw, text)
    assert call.cause is CauseClass.UNKNOWN
    assert call.evidence[0].tier == "T3-ungrounded"


def test_parse_keeps_only_the_grounded_quotes():
    text = "I didn't get any OTP so I closed the page."
    raw = json.dumps({
        "cause_class": "auth_abandoned", "confidence": 0.8,
        "evidence": [{"quote": "didn't get any OTP", "why": "x"},
                     {"quote": "app crashed", "why": "invented"}],
        "customer_intent": "unclear",
    })
    call = parse_t3(raw, text)
    assert call.cause is CauseClass.AUTH_ABANDONED
    assert [e.raw_value for e in call.evidence] == ["didn't get any OTP"]


def test_parse_survives_garbage():
    assert parse_t3("not json", "x").cause is CauseClass.UNKNOWN
    assert parse_t3(json.dumps({"cause_class": "made_up", "evidence": [{"quote": "x", "why": ""}]}), "x").cause is CauseClass.UNKNOWN


# --- the engine only shows the model the residual -----------------------------------

def test_engine_routes_unknown_code_with_text_to_t3():
    llm = FakeLLM()
    dx = DiagnosisEngine(CohortModel(), llm=llm).diagnose(residual_case(), residual_case().free_text)
    assert llm.calls == 1
    assert dx.tier == "T3" and dx.cause_class is CauseClass.INSUFFICIENT_FUNDS
    assert any(e.tier == "T3" for e in dx.evidence)


def test_engine_never_calls_the_model_when_the_code_is_readable():
    llm = FakeLLM()
    case = residual_case()
    case.failure_code = "expired_card"  # T1 can read this; the text is irrelevant
    dx = DiagnosisEngine(CohortModel(), llm=llm).diagnose(case, case.free_text)
    assert llm.calls == 0 and dx.cause_class is CauseClass.EXPIRED_INSTRUMENT


def test_engine_never_calls_the_model_without_text():
    llm = FakeLLM()
    case = residual_case()
    case.free_text = None
    dx = DiagnosisEngine(CohortModel(), llm=llm).diagnose(case, None)
    assert llm.calls == 0 and dx.cause_class is CauseClass.UNKNOWN


def test_claude_diagnoser_is_absent_without_a_credential(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert ClaudeDiagnoser.from_env() is None


def test_claude_diagnoser_call_budget_answers_unknown_not_exception():
    d = ClaudeDiagnoser(max_calls=0)
    cause, ev = d.diagnose(residual_case(), "anything")
    assert cause is CauseClass.UNKNOWN and ev[0].tier == "T3-budget-exhausted"


# --- the generator's residual ------------------------------------------------------

def test_residual_cases_have_unmapped_codes_and_text_and_nothing_else_does():
    cases = generate(2000, start=NOW, seed=3)
    residual = [c for c in cases if c.free_text is not None]
    assert 0.04 < len(residual) / len(cases) < 0.10
    for c in residual:
        assert c.failure_code not in CODE_TO_CAUSE
        assert c.advice_code is None
        assert latent_cause(c) is not None
    for c in cases:
        if c.free_text is None:
            assert c.failure_code in CODE_TO_CAUSE


def test_every_corpus_entry_is_its_own_ground_truth():
    for text, cause in FREE_TEXT_CORPUS:
        c = residual_case(text)
        assert latent_cause(c) is cause


def test_world_self_heal_does_not_depend_on_diagnosis():
    """Same case, diagnosed UNKNOWN (T3 off) vs correctly (T3 on): identical latent."""
    case = residual_case("This card expired in Aug. I have new card number, how to update?")
    w_off, w_on = World(14, seed=1), World(14, seed=1)
    w_off.admit(case, CauseClass.UNKNOWN)
    w_on.admit(case, CauseClass.EXPIRED_INSTRUMENT)
    assert w_off._latent[case.case_id].daily_self_heal == w_on._latent[case.case_id].daily_self_heal


# --- end to end through the runner -------------------------------------------------

def test_runner_records_model_and_evidence_for_t3_diagnoses():
    llm = FakeLLM()
    result = run(600, 5, 0.1, seed=11, llm=llm)
    assert result.llm.enabled and result.llm.model == "fake-t3"
    assert result.llm.residual_cases > 0
    assert 0 < result.llm.resolved <= result.llm.residual_cases

    t3 = [c for c in result.cases if c.tier == "T3"]
    assert t3, "some treated residual cases should carry a T3 diagnosis"
    ev = next(e for e in result.ledger.events_for(t3[0].case_id)
              if e.kind is EventKind.DIAGNOSED)
    assert ev.payload["tier"] == "T3"
    assert ev.payload["model"] == "fake-t3"
    assert ev.payload["evidence"] and ev.payload["evidence"][0] == t3[0].free_text


def test_runner_without_llm_leaves_residual_unknown_and_says_so():
    result = run(600, 5, 0.1, seed=11)
    assert not result.llm.enabled and result.llm.residual_cases > 0
    assert result.llm.calls == 0 and result.llm.resolved == 0
    assert not any(c.tier == "T3" for c in result.cases)
