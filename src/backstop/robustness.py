"""Does the result survive a world it was not tuned in?

    python -m backstop.robustness

A lift measured once, in the simulator whose constants the same author calibrated,
is a number about this repository rather than about the agent. Two things could be
carrying it: a lucky seed, or a world that happens to suit the strategy. This runs
the batch across several seeds in several deliberately wrong worlds and reports what
holds.

The worlds are not noise. Each one turns off or inverts something the agent relies
on: customers who mostly fix themselves (so there is little left to attribute), an
agent whose actions barely work, templates that are all identical (so the planner's
template learning has nothing real to find), buyers who break their promises, and
customers who tire and leave twice as fast. If the lift only exists in the
calibrated world, these say so.

The planner learns within a batch, so each run also starts from a blank table. A
table carried in from the *calibrated* world is tested separately: that is the
question of whether yesterday's lesson survives the world changing underneath it,
which is the realistic failure mode in production.
"""

from __future__ import annotations

import argparse
import statistics
from dataclasses import dataclass

from .measurement.report import compute_lift
from .planner.learning import ActionModel
from .runner import run
from .sim.world import WorldConfig


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    world: WorldConfig
    note: str


def scenarios() -> list[Scenario]:
    base = WorldConfig()
    return [
        Scenario("calibrated", base, "the tuned world the numbers were quoted in"),
        Scenario("self-heal x1.6", base.scaled(self_heal=1.6),
                 "most cases fix themselves; little left to attribute"),
        Scenario("self-heal x0.5", base.scaled(self_heal=0.5),
                 "almost nothing self-heals; the holdout recovers rarely"),
        Scenario("actions x0.5", base.scaled(action_power=0.5),
                 "everything the agent does works half as well"),
        Scenario("flat templates", base.scaled(templates=0.0),
                 "every template identical; nothing for the planner to learn"),
        Scenario("promises x0.5", base.scaled(promise_keep=0.5),
                 "buyers name dates and break them"),
        Scenario("fragile customers", base.scaled(fatigue=2.0, opt_out=2.0),
                 "contact fatigue and opt-outs twice as fast"),
        Scenario("hostile", base.scaled(self_heal=1.4, action_power=0.6,
                                        promise_keep=0.6, fatigue=1.5, opt_out=1.5),
                 "several at once, all unfavourable"),
    ]


@dataclass(slots=True)
class Result:
    scenario: str
    note: str
    lifts: list[float]
    significant: int
    contacts_per_recovery: list[float]
    net_paise: list[int]

    @property
    def mean_lift(self) -> float:
        return statistics.fmean(self.lifts)

    @property
    def worst_lift(self) -> float:
        return min(self.lifts)

    @property
    def holds(self) -> bool:
        """Every seed significantly positive. Anything less is reported, not hidden."""
        return self.significant == len(self.lifts) and self.worst_lift > 0


def evaluate(scenario: Scenario, seeds: list[int], cases: int, days: int,
             pretrained: ActionModel | None = None) -> Result:
    lifts, sig, cpr, nets = [], 0, [], []
    for seed in seeds:
        # A copy per seed: the question is what one batch does, not what a table
        # accumulated across the whole sweep does.
        table = ActionModel.from_json(pretrained.to_json()) if pretrained else None
        r = run(cases, days, 0.10, seed, actions=table, world_config=scenario.world)
        lift = compute_lift(r.cases)
        recovered = lift.treated.recovered or 1
        lifts.append(lift.lift_pp)
        sig += 1 if lift.significant and lift.lift_pp > 0 else 0
        cpr.append(r.restraint.contacts_sent / recovered)
        nets.append(lift.incremental_paise - r.action_cost)
    return Result(scenario.name, scenario.note, lifts, sig, cpr, nets)


def train(seeds: list[int], cases: int, days: int) -> ActionModel:
    """A table learned in the calibrated world, to carry into the others."""
    model = ActionModel()
    for seed in seeds:
        run(cases, days, 0.10, seed, actions=model)
    return model


def render(results: list[Result], seeds: int, transfer: list[Result] | None = None) -> str:
    out = ["=" * 78,
           f"ROBUSTNESS      {seeds} seeds per world (synthetic data, as everywhere)",
           "=" * 78, "",
           f"  {'world':<20}{'mean lift':>11}{'worst':>8}{'sig':>6}{'c/rec':>8}  what changed",
           ""]
    for r in results:
        verdict = f"{r.significant}/{len(r.lifts)}"
        out.append(f"  {r.scenario:<20}{r.mean_lift:>+10.1f}{r.worst_lift:>+8.1f}"
                   f"{verdict:>6}{statistics.fmean(r.contacts_per_recovery):>8.2f}  {r.note}")
    out.append("")

    held = [r for r in results if r.holds]
    out.append(f"  significantly positive in every seed of {len(held)}/{len(results)} worlds")
    weak = [r for r in results if not r.holds]
    if weak:
        out.append("  did not hold: " + ", ".join(r.scenario for r in weak))
    base = next((r for r in results if r.scenario == "calibrated"), None)
    if base:
        spread = max(r.mean_lift for r in results) - min(r.mean_lift for r in results)
        out.append(f"  calibrated world {base.mean_lift:+.1f} pp; across worlds the mean "
                   f"ranges over {spread:.1f} pp")

    if transfer:
        out += ["", "  CARRYING A LEARNED TABLE INTO A WORLD IT WAS NOT LEARNED IN",
                f"    {'world':<20}{'cold':>8}{'warmed':>9}{'delta':>8}"]
        cold = {r.scenario: r for r in results}
        for r in transfer:
            c = cold[r.scenario].mean_lift
            out.append(f"    {r.scenario:<20}{c:>+8.1f}{r.mean_lift:>+9.1f}"
                       f"{r.mean_lift - c:>+8.1f}")
        deltas = {r.scenario: r.mean_lift - cold[r.scenario].mean_lift for r in transfer}
        worst = min(deltas, key=deltas.get)
        best = max(deltas, key=deltas.get)
        if deltas[worst] >= 0:
            out.append(f"    Carrying it never cost anything here: deltas ran "
                       f"{deltas[worst]:+.1f} to {deltas[best]:+.1f} pp, worst in "
                       f"'{worst}'.")
            out.append("    Which world gains most moves between runs, so read this as "
                       "'a stale table did no")
            out.append("    harm in these worlds', not as a per-world benefit. The "
                       "cheap conclusion is the safe")
            out.append("    one: carry the table, keep the holdout, and let it be "
                       "measured rather than assumed.")
        else:
            out.append(f"    It is not free: '{worst}' is {deltas[worst]:+.1f} pp worse "
                       f"warmed than cold.")
            out.append("    A table learned in one world can mislead in another, which is "
                       "an argument for")
            out.append("    retraining on recent batches rather than carrying one "
                       "indefinitely.")
    out.append("")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="Re-run the batch in worlds it was not tuned in")
    ap.add_argument("--cases", type=int, default=4000)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero unless the lift is significantly positive in "
                         "every seed of every world. Seeds are fixed, so this is a "
                         "regression test rather than a coin toss, and CI runs it")
    ap.add_argument("--transfer", action="store_true",
                    help="also carry a table learned in the calibrated world into each "
                         "of the others")
    args = ap.parse_args()

    seeds = [42 + i * 17 for i in range(args.seeds)]
    worlds = scenarios()
    results = [evaluate(s, seeds, args.cases, args.days) for s in worlds]

    transfer = None
    if args.transfer:
        table = train([901, 902], args.cases, args.days)
        transfer = [evaluate(s, seeds, args.cases, args.days, pretrained=table)
                    for s in worlds]

    print(render(results, len(seeds), transfer))

    failed = [r.scenario for r in results if not r.holds]
    if args.strict and failed:
        print(f"FAILED: the lift did not hold in {', '.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
