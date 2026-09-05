"""Each test here pins a constant to the source it came from.

If a regulator moves a number, one of these fails and names the rule that changed.
Sources are listed in policy/config.py; verified 2026-09-05.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from backstop.domain.case import Case
from backstop.domain.types import CaseType, Channel, Disposition, MessageClass
from backstop.planner.actions import Action, ActionKind
from backstop.planner.planner import message_class_for
from backstop.policy.config import PolicyConfig
from backstop.policy.engine import PolicyEngine
from backstop.policy.rules import RuleContext

NOW = datetime(2026, 9, 10, 14, 0)  # 2pm — inside the permitted window


def make_case(**kw) -> Case:
    defaults = dict(
        case_id="c1", case_type=CaseType.CARD_FAILURE, amount_paise=250_000,
        customer_ref="cust_1", issuer="HDFC", instrument="card",
        failure_code="insufficient_funds", created_at=NOW - timedelta(days=2),
    )
    return Case(**{**defaults, **kw})


def ctx(**kw) -> RuleContext:
    defaults = dict(
        now=NOW, config=PolicyConfig(), expected_recovery_paise=5_000_000,
        mandate_notice_sent_at=NOW - timedelta(days=3),
    )
    return RuleContext(**{**defaults, **kw})


def msg(mc: MessageClass, now: datetime = NOW) -> Action:
    return Action(ActionKind.SEND_MESSAGE, now, channel=Channel.WHATSAPP,
                  template="one_tap_link", message_class=mc)


# --- TRAI TCCCPR 2018 -------------------------------------------------------

def test_promotional_window_opens_at_10_not_9():
    """TCCCPR permits promotional communication 10:00-21:00 IST."""
    cfg = PolicyConfig()
    assert (cfg.promo_hours_open, cfg.promo_hours_close) == (10, 21)


@pytest.mark.parametrize("hour,expect_deferred", [
    (9, True),    # before the window opens
    (10, False),  # first permitted hour
    (20, False),  # last permitted hour
    (21, True),   # window has closed
])
def test_promotional_messages_are_bound_to_the_window(hour, expect_deferred):
    at = NOW.replace(hour=hour)
    d = PolicyEngine().evaluate(
        make_case(), msg(MessageClass.PROMOTIONAL, at), ctx(now=at)
    )
    assert (d.disposition is Disposition.DEFER) is expect_deferred


@pytest.mark.parametrize("hour", [2, 9, 14, 23])
def test_service_messages_are_not_bound_to_the_window(hour):
    """A failed-debit notice is a service message and is not confined to promo hours."""
    at = NOW.replace(hour=hour)
    d = PolicyEngine().evaluate(make_case(), msg(MessageClass.SERVICE, at), ctx(now=at))
    assert d.allowed, d.reason


def test_dnd_blocks_promotional_but_not_service():
    """Suppressing a payment-failure notice because of a DND listing would be both
    wrong under TCCCPR and expensive."""
    promo = PolicyEngine().evaluate(
        make_case(), msg(MessageClass.PROMOTIONAL), ctx(on_dnd_registry=True))
    service = PolicyEngine().evaluate(
        make_case(), msg(MessageClass.SERVICE), ctx(on_dnd_registry=True))
    assert promo.disposition is Disposition.DENY
    assert service.allowed


def test_missing_consent_blocks_both_classes():
    for mc in (MessageClass.PROMOTIONAL, MessageClass.SERVICE):
        d = PolicyEngine().evaluate(make_case(), msg(mc), ctx(has_channel_consent=False))
        assert d.disposition is Disposition.DENY


def test_case_types_map_to_the_right_message_class():
    assert message_class_for(make_case(case_type=CaseType.CARD_FAILURE)) is MessageClass.SERVICE
    assert message_class_for(make_case(case_type=CaseType.MANDATE_LAPSE)) is MessageClass.SERVICE
    assert message_class_for(
        make_case(case_type=CaseType.CHECKOUT_ABANDONMENT)) is MessageClass.PROMOTIONAL


def test_an_action_that_forgets_its_class_gets_the_stricter_treatment():
    bare = Action(ActionKind.SEND_MESSAGE, NOW, channel=Channel.WHATSAPP)
    assert bare.message_class is MessageClass.PROMOTIONAL


# --- RBI e-mandate framework 2026 -------------------------------------------

def test_pre_debit_notice_is_24_hours():
    """RBI/CO.DPSS.POLC.No.S56/02.14.003/2026-27 — at least 24h before the debit."""
    assert PolicyConfig().mandate_notice_hours == 24


def test_afa_ceilings_match_the_framework():
    cfg = PolicyConfig()
    assert cfg.afa_ceiling_for(None) == 15_000_00
    for category in ("insurance_premium", "mutual_fund", "credit_card_bill"):
        assert cfg.afa_ceiling_for(category) == 1_00_000_00


def mandate_case(amount: int) -> Case:
    return make_case(case_type=CaseType.MANDATE_LAPSE, instrument="upi_mandate",
                     amount_paise=amount)


def test_debit_under_the_afa_ceiling_needs_no_authentication():
    d = PolicyEngine().evaluate(
        mandate_case(14_999_00), Action(ActionKind.RETRY_PAYMENT, NOW), ctx())
    assert d.allowed


def test_debit_over_the_afa_ceiling_is_denied_without_authentication():
    d = PolicyEngine().evaluate(
        mandate_case(15_001_00), Action(ActionKind.RETRY_PAYMENT, NOW), ctx())
    assert d.disposition is Disposition.DENY
    assert "PR-05" in d.rule_ids


def test_authentication_on_file_clears_the_afa_ceiling():
    d = PolicyEngine().evaluate(
        mandate_case(50_000_00), Action(ActionKind.RETRY_PAYMENT, NOW),
        ctx(afa_completed=True))
    assert d.allowed


def test_exempt_categories_carry_the_higher_ceiling():
    action = Action(ActionKind.RETRY_PAYMENT, NOW)
    amount = 90_000_00  # over the standard ceiling, under the category one
    assert PolicyEngine().evaluate(
        mandate_case(amount), action, ctx()).disposition is Disposition.DENY
    assert PolicyEngine().evaluate(
        mandate_case(amount), action, ctx(mandate_category="mutual_fund")).allowed


# --- Card network reattempt limits ------------------------------------------

def test_network_ceilings_match_published_limits():
    cfg = PolicyConfig()
    assert cfg.network_ceiling_for("visa") == 15
    assert cfg.network_ceiling_for("mastercard") == 10
    assert cfg.network_retry_window_days == 30
    # An unknown network gets the stricter of the two, not the looser.
    assert cfg.network_ceiling_for(None) == 10
    assert cfg.network_ceiling_for("some_new_network") == 10


def test_merchant_cap_binds_long_before_the_network_ceiling():
    """The network limit is the ceiling, not the target — economics stop paying first."""
    cfg = PolicyConfig()
    assert cfg.max_retries < min(cfg.network_ceiling_for("visa"),
                                 cfg.network_ceiling_for("mastercard"))


def test_network_ceiling_denies_the_retry_once_reached():
    d = PolicyEngine().evaluate(
        make_case(), Action(ActionKind.RETRY_PAYMENT, NOW),
        ctx(network="visa", retries_in_network_window=15))
    assert d.disposition is Disposition.DENY
    assert "PR-04" in d.rule_ids


def test_mastercard_ceiling_bites_five_retries_earlier_than_visa():
    action = Action(ActionKind.RETRY_PAYMENT, NOW)
    at_ten = ctx(network="mastercard", retries_in_network_window=10)
    assert PolicyEngine().evaluate(make_case(), action, at_ten).disposition is Disposition.DENY
    visa_at_ten = ctx(network="visa", retries_in_network_window=10)
    assert PolicyEngine().evaluate(make_case(), action, visa_at_ten).allowed


def test_network_ceiling_denies_the_retry_but_not_other_actions():
    hit = ctx(network="mastercard", retries_in_network_window=10)
    assert PolicyEngine().evaluate(
        make_case(), Action(ActionKind.RETRY_PAYMENT, NOW), hit
    ).disposition is Disposition.DENY
    assert PolicyEngine().evaluate(make_case(), msg(MessageClass.SERVICE), hit).allowed


# --- provenance -------------------------------------------------------------

def test_config_version_is_no_longer_marked_unverified():
    assert "UNVERIFIED" not in PolicyConfig().version


def test_config_module_cites_its_sources():
    from backstop.policy import config
    doc = config.__doc__ or ""
    for citation in ("RBI/CO.DPSS.POLC.No.S56/02.14.003/2026-27", "TCCCPR", "Visa",
                     "Mastercard", "verified 2026-09-05"):
        assert citation in doc, f"missing citation: {citation}"
