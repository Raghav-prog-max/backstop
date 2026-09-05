"""Versioned policy configuration.

Thresholds are config, not constants in code, because they are set by regulation and
by each merchant and they change. The rule IDENTITIES are stable; these numbers are
versioned, and the version lands in `rule_ids` on every decision alongside the rule ID.

VERIFY BEFORE SUBMISSION: the retry ceiling and mandate lead time below are
placeholders. Confirm current card-network retry limits and RBI e-mandate
pre-debit notification / AFA rules against primary sources and update `version`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PolicyConfig:
    version: str = "cfg-2026.09.0-UNVERIFIED"

    # PR-02 — merchant contact window, local time (IST).
    quiet_hours_open: int = 9
    quiet_hours_close: int = 21

    # PR-03 — contacts per channel per rolling window.
    max_contacts_per_channel: int = 2
    max_contacts_total: int = 4

    # PR-04 — retry budget for one authorisation.
    max_retries: int = 3
    min_hours_between_retries: int = 24

    # PR-05 — e-mandate pre-debit notification lead time.
    mandate_notice_hours: int = 24

    # PR-06 — economic floor.
    merchant_margin: float = 0.18
    action_cost_paise: dict[str, int] | None = None
    # Each prior contact makes the next one more expensive.
    goodwill_cost_per_contact_paise: int = 1_500

    # PR-08 — grace after a promised payment date.
    promise_grace_hours: int = 48

    def cost_of(self, action_kind: str) -> int:
        costs = self.action_cost_paise or DEFAULT_ACTION_COSTS
        return costs.get(action_kind, 0)


# Fully-loaded cost of taking an action once, in paise.
DEFAULT_ACTION_COSTS: dict[str, int] = {
    "wait": 0,
    "retry_payment": 200,
    "switch_instrument": 200,
    "request_reauth_link": 400,
    "send_message": 600,
    "voice_call": 9_000,
    "offer_installment": 1_200,
    "escalate_human": 25_000,
    "close_case": 0,
}
