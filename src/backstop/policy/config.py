"""Versioned policy configuration.

Thresholds are config, not constants in code, because they are set by regulation and
by each merchant and they change. The rule IDENTITIES are stable; these numbers are
versioned, and the version lands in `rule_ids` on every decision alongside the rule ID.

SOURCES — verified 2026-09-05. Re-check before each submission or release.

  RBI Digital Payments E-Mandate Framework, 2026
  Circular RBI/CO.DPSS.POLC.No.S56/02.14.003/2026-27, dated 21 April 2026
    - pre-debit notification at least 24 hours before the debit, carrying merchant
      name, amount, date/time, mandate reference and reason
    - recurring debits up to Rs 15,000 may be processed without AFA
    - insurance premiums, mutual fund subscriptions and credit card bill payments
      may be processed without AFA up to Rs 1,00,000 per transaction
    - the first transaction under a mandate always requires AFA

  TRAI TCCCPR 2018 (as amended)
    - promotional commercial communication is confined to 10:00-21:00 IST and is
      scrubbed against the DND / National Customer Preference Register
    - service and transactional messages are not confined to that window and are
      not DND-scrubbed

  Card network reattempt limits (card-not-present)
    - Visa: at most 15 reattempts per declined transaction in any 30-day period
    - Mastercard: at most 10 retries in 30 days; Merchant Advice Code 03 means do
      not retry at all
    - "never retry" decline categories must not be resubmitted under any
      circumstances

  Network limits are the ceiling, not the target. The per-case cap below is a
  merchant-level policy choice set far under both networks' limits, because the
  economics stop paying long before the compliance limit binds.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# One lakh and fifteen thousand rupees, in paise.
AFA_EXEMPT_CEILING_PAISE = 1_00_000_00
AFA_STANDARD_CEILING_PAISE = 15_000_00

# Merchant categories carrying the higher AFA ceiling under the 2026 framework.
AFA_HIGHER_CEILING_CATEGORIES = frozenset(
    {"insurance_premium", "mutual_fund", "credit_card_bill"}
)


@dataclass(frozen=True, slots=True)
class PolicyConfig:
    version: str = "cfg-2026.09.1"

    # --- TRAI TCCCPR: promotional window only. Service messages are exempt. ---
    promo_hours_open: int = 10
    promo_hours_close: int = 21

    # --- PR-03: contacts per channel per rolling window (merchant policy). ---
    max_contacts_per_channel: int = 2
    max_contacts_total: int = 4

    # --- PR-04: retry budget. ---
    # Merchant-level cap per case, deliberately far under the network ceilings.
    max_retries: int = 3
    min_hours_between_retries: int = 24
    # Network ceilings, enforced over a rolling window per declined transaction.
    network_retry_window_days: int = 30
    network_retry_ceiling: dict[str, int] = field(
        default_factory=lambda: {"visa": 15, "mastercard": 10, "default": 10}
    )

    # --- PR-05: e-mandate. ---
    mandate_notice_hours: int = 24
    afa_ceiling_paise: int = AFA_STANDARD_CEILING_PAISE
    afa_higher_ceiling_paise: int = AFA_EXEMPT_CEILING_PAISE

    # --- PR-06: economic floor (merchant policy, not regulation). ---
    merchant_margin: float = 0.18
    action_cost_paise: dict[str, int] | None = None
    goodwill_cost_per_contact_paise: int = 1_500

    # --- PR-08 ---
    promise_grace_hours: int = 48

    def cost_of(self, action_kind: str) -> int:
        costs = self.action_cost_paise or DEFAULT_ACTION_COSTS
        return costs.get(action_kind, 0)

    def afa_ceiling_for(self, category: str | None) -> int:
        """Above this amount a recurring debit needs authentication."""
        if category in AFA_HIGHER_CEILING_CATEGORIES:
            return self.afa_higher_ceiling_paise
        return self.afa_ceiling_paise

    def network_ceiling_for(self, network: str | None) -> int:
        table = self.network_retry_ceiling
        return table.get((network or "").lower(), table["default"])


# Fully-loaded cost of taking an action once, in paise.
DEFAULT_ACTION_COSTS: dict[str, int] = {
    "wait": 0,
    "retry_payment": 200,
    "switch_instrument": 200,
    "request_reauth_link": 400,
    "send_message": 600,
    "voice_call": 9_000,
    "offer_installment": 1_200,
    # An email asking for a date, plus the AR analyst's time to log the reply.
    "request_promise_to_pay": 800,
    "escalate_human": 25_000,
    "close_case": 0,
}
