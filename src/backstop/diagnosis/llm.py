"""T3 — the model, confined to the unstructured residual.

This is the only place in Backstop that calls an LLM, and it is reached only when T1
returned UNKNOWN *and* the case carries free text (a customer reply, a support note, an
email thread). Everything the model returns is forced through the same `Diagnosis`
type as T0-T2, which means it must cite evidence spans or it is rejected at
construction — the model does not get a looser contract than the code table does.

The model never decides an action. It names a cause; the cohort posterior prices it;
the policy engine disposes. See ARCHITECTURE.md §6.

SEAM: `anthropic` is an optional extra (`pip install -e ".[llm]"`). Without it, or
without ANTHROPIC_API_KEY, the engine keeps its default `NoLLM` and refuses to guess.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from ..domain.case import Case
from ..domain.types import CauseClass
from .engine import Evidence

MODEL = "claude-opus-5"

# The closed set the model may answer with. UNKNOWN is a legitimate answer and the
# prompt says so — an honest "cannot tell" costs nothing; a confident wrong cause
# spends a contact on the wrong fix.
_CAUSES = [c.value for c in CauseClass]

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cause_class": {"type": "string", "enum": _CAUSES},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    # Verbatim span from the free text that supports the cause.
                    "quote": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["quote", "why"],
                "additionalProperties": False,
            },
        },
        "customer_intent": {
            "type": "string",
            "enum": ["will_pay", "needs_new_instrument", "disputes", "wants_out", "unclear"],
        },
    },
    "required": ["cause_class", "confidence", "evidence", "customer_intent"],
    "additionalProperties": False,
}

_SYSTEM = f"""You classify why a payment to an Indian merchant failed, from unstructured text
the structured decline codes could not explain: a customer's reply, a support-desk note,
a bank SMS the customer forwarded, an email thread.

Return exactly one cause_class from this closed set:
{chr(10).join(f'  - {c}' for c in _CAUSES)}

Meanings:
  insufficient_funds     balance / limit was short; customer usually pays after salary or top-up
  expired_instrument     card expired, blocked, reissued, closed, or details changed
  issuer_unavailable     bank / UPI app / network was down or timing out; nothing wrong with the customer
  auth_abandoned         customer did not complete OTP / 3DS / UPI PIN, often distracted or app crashed
  risk_decline           bank or merchant risk system blocked it; retrying is harmful
  do_not_honour          generic issuer refusal with no stated reason
  mandate_not_notified   recurring debit failed because the pre-debit notice was not sent / not seen
  unknown                the text does not say — choose this rather than guess

Rules:
  - Every evidence.quote must be a verbatim substring of the text you were given.
  - If the text supports nothing specific, answer unknown with low confidence.
  - customer_intent is what the customer says they will do, not what you think they should.
  - You are not choosing an action. A separate policy engine decides what happens next."""


@dataclass(slots=True)
class T3Call:
    """One model call, as the ledger and the report see it."""

    cause: CauseClass
    confidence: float
    intent: str
    evidence: list[Evidence]
    input_tokens: int = 0
    output_tokens: int = 0
    refused: bool = False


@dataclass(slots=True)
class ClaudeDiagnoser:
    """T3 via the Claude API. Structured output; evidence spans are mandatory."""

    model: str = MODEL
    max_calls: int | None = None
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    refusals: int = 0
    _client: Any = field(default=None, repr=False)

    @classmethod
    def from_env(cls, *, max_calls: int | None = None) -> "ClaudeDiagnoser | None":
        """None when the SDK or a credential is missing — the caller keeps NoLLM."""
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            return None
        try:
            import anthropic  # noqa: F401  (optional extra)
        except ImportError:
            return None
        return cls(max_calls=max_calls)

    def _sdk(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def diagnose(self, case: Case, free_text: str) -> tuple[CauseClass, list[Evidence]]:
        call = self.call(case, free_text)
        return call.cause, call.evidence

    def call(self, case: Case, free_text: str) -> T3Call:
        if self.max_calls is not None and self.calls >= self.max_calls:
            return T3Call(
                CauseClass.UNKNOWN, 0.0, "unclear",
                [Evidence("free_text", free_text[:64], "T3-budget-exhausted")],
            )

        import anthropic

        self.calls += 1
        user = (
            f"Case: {case.case_type.value}, {case.instrument} via {case.issuer}, "
            f"amount Rs {case.amount_paise / 100:,.2f}, gateway code "
            f"'{case.failure_code}' (unmapped).\n\nText:\n\"\"\"\n{free_text}\n\"\"\""
        )
        try:
            resp = self._sdk().messages.create(
                model=self.model,
                max_tokens=1024,
                system=[{"type": "text", "text": _SYSTEM,
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": _SCHEMA},
                },
            )
        except anthropic.APIStatusError as e:
            # A failed call is an unknown, not an exception in the batch. It is
            # recorded as such so the report can count it.
            return T3Call(
                CauseClass.UNKNOWN, 0.0, "unclear",
                [Evidence("free_text", free_text[:64], f"T3-error-{e.status_code}")],
            )
        except anthropic.APIConnectionError:
            return T3Call(
                CauseClass.UNKNOWN, 0.0, "unclear",
                [Evidence("free_text", free_text[:64], "T3-error-connection")],
            )

        self.input_tokens += resp.usage.input_tokens
        self.output_tokens += resp.usage.output_tokens

        if resp.stop_reason == "refusal":
            self.refusals += 1
            return T3Call(
                CauseClass.UNKNOWN, 0.0, "unclear",
                [Evidence("free_text", free_text[:64], "T3-refused")], refused=True,
            )

        text = next((b.text for b in resp.content if b.type == "text"), "")
        return parse_t3(text, free_text)


def parse_t3(raw_json: str, free_text: str) -> T3Call:
    """Turn the model's JSON into a T3Call, dropping any quote that is not actually in
    the source text. If nothing survives, the answer is UNKNOWN — a cause the model
    cannot ground is not a diagnosis, whatever its confidence field says.
    """
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError:
        return T3Call(CauseClass.UNKNOWN, 0.0, "unclear",
                      [Evidence("free_text", free_text[:64], "T3-unparseable")])

    grounded = [
        Evidence("free_text", str(item.get("quote", ""))[:160], "T3")
        for item in data.get("evidence", [])
        if isinstance(item, dict) and str(item.get("quote", "")) and
        str(item["quote"]) in free_text
    ]
    try:
        cause = CauseClass(data.get("cause_class", "unknown"))
    except ValueError:
        cause = CauseClass.UNKNOWN

    if not grounded:
        return T3Call(CauseClass.UNKNOWN, 0.0, str(data.get("customer_intent", "unclear")),
                      [Evidence("free_text", free_text[:64], "T3-ungrounded")])

    return T3Call(
        cause=cause,
        confidence=float(data.get("confidence", 0.0)),
        intent=str(data.get("customer_intent", "unclear")),
        evidence=grounded,
    )
