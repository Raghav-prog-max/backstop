"""The result has to survive worlds it was not tuned in.

The simulator's constants were hand-calibrated by the same author who wrote the
agent, so a lift measured only there is a fact about this repository. These pin the
machinery that re-measures it elsewhere: the world's latent numbers really are
movable, moving them really does change outcomes, and the sweep reports what it
found rather than what would be nice.
"""

from __future__ import annotations

from backstop.domain.types import Arm, CaseState, CauseClass
from backstop.planner.actions import ActionKind
from backstop.robustness import Result, evaluate, render, scenarios
from backstop.runner import run
from backstop.sim.world import WorldConfig


def recovery_rate(cases, arm: Arm) -> float:
    pool = [c for c in cases if c.arm is arm]
    return sum(c.state is CaseState.RECOVERED for c in pool) / len(pool)


def test_scaling_moves_the_numbers_it_says_it_moves():
    base = WorldConfig()
    hot = base.scaled(self_heal=1.5, action_power=0.5, promise_keep=0.5,
                      fatigue=2.0, opt_out=2.0)
    assert hot.self_heal[CauseClass.INSUFFICIENT_FUNDS] == (
        base.self_heal[CauseClass.INSUFFICIENT_FUNDS] * 1.5)
    assert hot.action_power[ActionKind.RETRY_PAYMENT] == (
        base.action_power[ActionKind.RETRY_PAYMENT] * 0.5)
    assert hot.promise_rate[CauseClass.AP_CYCLE][1] == (
        base.promise_rate[CauseClass.AP_CYCLE][1] * 0.5)
    assert hot.promise_rate[CauseClass.AP_CYCLE][0] == base.promise_rate[CauseClass.AP_CYCLE][0]
    assert hot.fatigue_per_contact == base.fatigue_per_contact * 2
    assert hot.opt_out_per_contact == base.opt_out_per_contact * 2


def test_probabilities_stay_probabilities():
    """Scaling a 0.55 self-heal rate by 3 must not produce a 1.65 probability."""
    absurd = WorldConfig().scaled(self_heal=3.0, action_power=10.0)
    assert all(0.0 <= v < 1.0 for v in absurd.self_heal.values())
    assert all(0.0 <= v < 1.0 for v in absurd.action_power.values())


def test_flattening_templates_removes_the_difference_between_them():
    flat = WorldConfig().scaled(templates=0.0)
    assert set(flat.template_multiplier.values()) == {1.0}
    half = WorldConfig().scaled(templates=0.5)
    base = WorldConfig().template_multiplier["one_tap_link"]
    assert half.template_multiplier["one_tap_link"] == 1.0 + (base - 1.0) * 0.5


def test_a_changed_world_changes_what_happens_in_it():
    calibrated = run(800, 10, 0.2, seed=4)
    healing = run(800, 10, 0.2, seed=4, world_config=WorldConfig().scaled(self_heal=1.8))
    # The holdout is only ever subject to self-heal, so it is the clean read.
    assert (recovery_rate(healing.cases, Arm.HOLDOUT)
            > recovery_rate(calibrated.cases, Arm.HOLDOUT) + 0.05)

    weak = run(800, 10, 0.2, seed=4, world_config=WorldConfig().scaled(action_power=0.3))
    assert (recovery_rate(weak.cases, Arm.TREATED)
            < recovery_rate(calibrated.cases, Arm.TREATED) - 0.05)


def test_the_world_is_unchanged_by_default():
    """A WorldConfig nobody touched must behave exactly like no WorldConfig at all."""
    a = run(600, 8, 0.2, seed=6)
    b = run(600, 8, 0.2, seed=6, world_config=WorldConfig())
    assert [c.state for c in a.cases] == [c.state for c in b.cases]


def test_the_sweep_reports_a_world_where_the_lift_does_not_hold():
    """The harness has to be able to deliver bad news, or it is decoration."""
    holds = Result("ok", "", [20.0, 21.0], significant=2,
                   contacts_per_recovery=[1.2, 1.3], net_paise=[1, 1])
    fails = Result("rigged", "", [20.0, -3.0], significant=1,
                   contacts_per_recovery=[1.2, 1.3], net_paise=[1, 1])
    assert holds.holds and not fails.holds

    text = render([holds, fails], seeds=2)
    assert "1/2 worlds" in text
    assert "did not hold: rigged" in text


def test_the_sweep_runs_end_to_end():
    small = scenarios()[:2]
    results = [evaluate(s, [11], cases=400, days=6) for s in small]
    assert len(results) == 2
    assert all(len(r.lifts) == 1 for r in results)
    assert "ROBUSTNESS" in render(results, seeds=1)
