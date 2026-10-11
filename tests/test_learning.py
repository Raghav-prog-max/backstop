"""The planner learns what actions are worth instead of being told.

These pin the properties that make the learned table worth having: it moves toward
what actually happened, a thin arm leans on its general one, a promise is scored as
money rather than as a date obtained, and a contact is charged for the customers it
drives away.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from backstop.diagnosis.cohort import CohortModel
from backstop.diagnosis.engine import DiagnosisEngine
from backstop.domain.case import Case
from backstop.domain.types import CaseType, Channel, MessageClass
from backstop.planner.actions import Action, ActionKind
from backstop.planner.learning import ActionModel
from backstop.planner.planner import Planner

NOW = datetime(2026, 9, 10, 14, 0)


def make_case(**kw) -> Case:
    defaults = dict(
        case_id="c1", case_type=CaseType.CARD_FAILURE, amount_paise=500_000,
        customer_ref="cust_1", issuer="HDFC", instrument="card",
        failure_code="insufficient_funds", created_at=NOW - timedelta(days=3),
    )
    return Case(**{**defaults, **kw})


def diagnose(case: Case):
    return DiagnosisEngine(CohortModel()).diagnose(case)


def msg(template: str, **kw) -> Action:
    return Action(ActionKind.SEND_MESSAGE, NOW, channel=Channel.WHATSAPP,
                  template=template, message_class=MessageClass.SERVICE, **kw)


# --- the model ----------------------------------------------------------------------

def test_an_arm_moves_toward_what_happened():
    m = ActionModel()
    key = m.key("card_failure", "insufficient_funds", "send_message", "one_tap_link")
    before = m.estimate(key)
    for i in range(200):
        m.observe(key, i % 4 == 0)  # 25%
    after = m.estimate(key)
    assert after > before
    assert abs(after - 0.25) < 0.05


def test_a_thin_arm_reads_as_its_general_arm():
    """A cause with three observations should not get its own opinion."""
    m = ActionModel()
    common = m.key("card_failure", "insufficient_funds", "retry_payment", None)
    for i in range(400):
        m.observe(common, i % 2 == 0)  # 50% in general

    rare = m.key("mandate_lapse", "risk_decline", "retry_payment", None)
    assert abs(m.estimate(rare) - 0.5) < 0.1  # inherits the general arm

    for _ in range(300):
        m.observe(rare, False)  # ...until it has its own evidence
    assert m.estimate(rare) < 0.1


def test_stage_is_part_of_the_arm():
    """The fifth message does not work like the first, and the model can say so."""
    m = ActionModel()
    first = m.key("card_failure", "insufficient_funds", "send_message", "plain", "0")
    late = m.key("card_failure", "insufficient_funds", "send_message", "plain", "3")
    for _ in range(200):
        m.observe(first, True)
        m.observe(late, False)
    assert m.estimate(first) > 0.8 and m.estimate(late) < 0.2


def test_the_table_survives_a_round_trip():
    m = ActionModel()
    key = m.key("card_failure", "do_not_honour", "retry_payment", None, "r1")
    for i in range(50):
        m.observe(key, i % 5 == 0)
    back = ActionModel.from_json(m.to_json())
    assert back.estimate(key) == m.estimate(key)
    assert back.trials(key) == m.trials(key)


# --- the planner ---------------------------------------------------------------------

def test_the_planner_comes_to_prefer_the_template_that_works():
    planner = Planner(epsilon=0.0, rng=random.Random(5))
    case = make_case()
    dx = diagnose(case)
    good, bad = msg("one_tap_link"), msg("plain_reminder")

    for _ in range(300):
        planner.observe(case, dx, good, success=True)
        planner.observe(case, dx, bad, success=False)

    better = sum(planner._score(good, case, dx, NOW) > planner._score(bad, case, dx, NOW)
                 for _ in range(50))
    assert better == 50


def test_an_untried_action_still_gets_tried():
    """Thompson sampling is what keeps exploration alive once epsilon is small."""
    planner = Planner(epsilon=0.0, rng=random.Random(7))
    case = make_case()
    dx = diagnose(case)
    known, untried = msg("one_tap_link"), msg("value_reminder")
    for _ in range(200):
        planner.observe(case, dx, known, success=False)

    tried = sum(planner._score(untried, case, dx, NOW) > planner._score(known, case, dx, NOW)
                for _ in range(50))
    assert tried > 25


def test_a_promise_is_scored_on_being_kept_not_on_being_given():
    planner = Planner(epsilon=0.0, rng=random.Random(11))
    case = make_case(case_type=CaseType.INVOICE_OVERDUE, failure_code="invoice_overdue",
                     due_at=NOW - timedelta(days=20))
    dx = diagnose(case)
    ask = Action(ActionKind.REQUEST_PROMISE_TO_PAY, NOW, channel=Channel.EMAIL,
                 template="promise_to_pay", message_class=MessageClass.SERVICE)

    for _ in range(200):
        planner.observe(case, dx, ask, success=True)  # a date, every time
    with_dates = sum(planner._score(ask, case, dx, NOW) for _ in range(40)) / 40

    for _ in range(200):
        planner.settle(planner.keep_arm_for(case, dx), success=False)  # none of them held
    after_breaking = sum(planner._score(ask, case, dx, NOW) for _ in range(40)) / 40

    assert after_breaking < with_dates / 2


def test_a_contact_is_charged_for_the_customers_it_drives_away():
    planner = Planner(epsilon=0.0, rng=random.Random(13))
    case = make_case()
    dx = diagnose(case)
    m = msg("one_tap_link")
    for _ in range(200):
        planner.observe(case, dx, m, success=True)
    before = sum(planner._score(m, case, dx, NOW) for _ in range(40)) / 40

    for _ in range(200):
        planner.observe_contact(case, dx, m, opted_out=True)
    after = sum(planner._score(m, case, dx, NOW) for _ in range(40)) / 40

    assert after < before
    assert after < 0  # a contact that always loses the customer is never worth sending


# --- end to end -----------------------------------------------------------------------

def test_a_batch_learns_the_ranking_the_world_actually_has():
    """`one_tap_link` beats `plain_reminder` in the simulator. Nothing tells the
    planner that; by the end of a batch it should have worked it out and acted on it."""
    from backstop.runner import run

    result = run(4000, 14, 0.10, seed=3)
    arms = {label: (trials, rate) for label, trials, rate, _ in result.action_arms}

    good = [v for k, v in arms.items() if k.startswith("send_message/one_tap_link")]
    plain = [v for k, v in arms.items() if k.startswith("send_message/plain_reminder")]
    assert good and plain
    assert sum(t for t, _ in good) > sum(t for t, _ in plain)

    # And it is pricing decisions off its own evidence, not a table in the source.
    assert result.actions is not None
    retries = [t for k, (t, _) in arms.items() if k.startswith("retry_payment")]
    assert sum(retries) > 1000
