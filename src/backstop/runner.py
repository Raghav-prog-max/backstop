"""Batch runner — the whole pipeline over a synthetic batch, day by day.

    python -m backstop.runner --cases 5000 --days 14

Holdout cases are admitted to the world and stepped for self-heal, but never
diagnosed, never planned and never contacted. That is the point of them.
"""

from __future__ import annotations

import argparse
import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from .diagnosis.cohort import CohortModel
from .diagnosis.engine import DiagnosisEngine
from .domain.case import Case
from .domain.events import CaseEvent, EventKind
from .domain.types import Arm, CaseState, Disposition
from .execution.outbox import Outbox
from .ledger.store import InMemoryLedger
from .measurement.assignment import assign
from .measurement.report import Restraint, render
from .planner.actions import ActionKind
from .planner.planner import Planner
from .policy.config import PolicyConfig
from .policy.engine import PolicyEngine
from .policy.rules import CONTACT_ACTIONS, RuleContext
from .reporting.whynot import extract, write_html
from .sim.generator import generate
from .sim.world import World

CONTACT_KINDS = CONTACT_ACTIONS


@dataclass(slots=True)
class BatchResult:
    cases: list[Case]
    ledger: InMemoryLedger
    restraint: Restraint
    action_cost: int
    config_version: str
    dispatched: int
    deduped: int
    cohorts_learned: int


def run(n_cases: int, horizon_days: int, holdout: float, seed: int) -> BatchResult:
    start = datetime(2026, 9, 1, 9, 0)
    rng = random.Random(seed)

    ledger = InMemoryLedger()
    cohort = CohortModel()
    diagnoser = DiagnosisEngine(cohort)
    policy = PolicyEngine(PolicyConfig())
    planner = Planner(epsilon=0.1, rng=random.Random(seed + 1))
    world = World(horizon_days=horizon_days, seed=seed + 2)
    outbox = Outbox(ledger, world)

    cases = generate(n_cases, start=start, seed=seed)
    restraint = Restraint()
    action_cost = 0
    diagnoses = {}
    last_retry: dict[str, datetime] = {}
    attempts: dict[str, int] = {}

    for case in cases:
        case.arm = assign(case.case_id, holdout)
        ledger.append(
            CaseEvent(case.case_id, EventKind.DETECTED, case.created_at,
                      payload={"amount_paise": case.amount_paise, "arm": case.arm.value})
        )
        dx = diagnoser.diagnose(case)
        case.cause = dx.cause_class
        case.recoverability = dx.recoverability
        diagnoses[case.case_id] = dx
        world.admit(case, dx.cause_class)

        if case.arm is Arm.HOLDOUT:
            continue  # never diagnosed into the pipeline, never touched

        case.transition(CaseState.DIAGNOSED)
        ledger.append(
            CaseEvent(case.case_id, EventKind.DIAGNOSED, case.created_at,
                      payload={"cause": dx.cause_class.value, "tier": dx.tier,
                               "recoverability": round(dx.recoverability, 4),
                               "posterior_n": dx.posterior_n},
                      actor="diagnosis")
        )

    for day in range(horizon_days):
        now = start + timedelta(days=day, hours=10)

        for case in cases:
            if case.is_terminal:
                continue
            # A case cannot be worked before it was detected. Without this the ledger
            # replays actions dated earlier than the signal that caused them.
            if now < case.created_at:
                continue

            # Self-heal applies to both arms. It is what gross recovery mistakes for work.
            if world.self_heals_today(case):
                _recover(case, ledger, now, "self-healed", cohort, diagnoses)
                continue

            if case.arm is Arm.HOLDOUT:
                continue

            dx = diagnoses[case.case_id]
            action = planner.next_action(case, dx, now)

            expected = int(case.amount_paise * dx.recoverability * policy.config.merchant_margin)
            ctx = RuleContext(
                now=now,
                config=policy.config,
                expected_recovery_paise=expected,
                last_retry_at=last_retry.get(case.case_id),
                on_dnd_registry=rng.random() < 0.04,
                mandate_notice_sent_at=case.created_at,
            )
            decision = policy.evaluate(case, action, ctx)
            ledger.append(
                CaseEvent(case.case_id, EventKind.POLICY_DECIDED, now,
                          payload={"action": str(action),
                                   "disposition": decision.disposition.value,
                                   "reason": decision.reason},
                          rule_ids=decision.rule_ids, actor="policy")
            )

            if decision.disposition is not Disposition.ALLOW:
                for r in decision.fired:
                    restraint.denied_by_rule[r.rule_id] = (
                        restraint.denied_by_rule.get(r.rule_id, 0) + 1
                    )

            if decision.disposition in (Disposition.SUPPRESS, Disposition.HARD_STOP):
                if case.state is CaseState.DIAGNOSED:
                    case.transition(CaseState.SUPPRESSED, reason=decision.reason,
                                    rule_id=decision.deciding_rule)
                else:
                    case.state = CaseState.SUPPRESSED
                    case.stopping_rule = decision.deciding_rule
                    case.terminal_reason = decision.reason
                restraint.suppressed += 1
                _outcome(ledger, case, now, "suppressed", decision.reason)
                continue

            if decision.disposition in (Disposition.DENY, Disposition.DEFER):
                continue  # requeued; try again tomorrow

            if action.kind is ActionKind.WAIT or action.fire_at > now:
                continue

            if case.state is CaseState.DIAGNOSED:
                case.transition(CaseState.PLANNED)
            if case.state is CaseState.PLANNED:
                case.transition(CaseState.ATTEMPTING)

            if action.kind is ActionKind.ESCALATE_HUMAN:
                case.state = CaseState.ESCALATED
                case.terminal_reason = "handed to human queue"
                restraint.escalated += 1
                action_cost += policy.config.cost_of(action.kind.value)
                _outcome(ledger, case, now, "escalated", "human queue")
                continue

            attempts[case.case_id] = attempts.get(case.case_id, 0) + 1
            result = outbox.queue_and_dispatch(case, action, attempts[case.case_id], now)
            action_cost += policy.config.cost_of(action.kind.value)

            if action.kind is ActionKind.RETRY_PAYMENT:
                case.retries_used += 1
                last_retry[case.case_id] = now
            if action.kind in CONTACT_KINDS and action.channel:
                case.record_contact(action.channel)
                restraint.contacts_sent += 1

            if result and result.ok and result.recovered_paise:
                _recover(case, ledger, now, str(action), cohort, diagnoses)
            elif case.opted_out:
                restraint.opt_outs += 1
                case.state = CaseState.ABANDONED
                case.terminal_reason = "customer opted out"
                case.stopping_rule = "PR-07"
                _outcome(ledger, case, now, "abandoned", "customer opted out")
            else:
                case.transition(CaseState.PLANNED)  # wait -> next attempt

    for case in cases:
        if not case.is_terminal:
            if case.state is CaseState.ATTEMPTING:
                case.transition(CaseState.ABANDONED, reason="horizon reached")
            else:
                case.state = CaseState.ABANDONED
                case.terminal_reason = "horizon reached"
            if case.arm is Arm.TREATED:
                _observe(cohort, case, diagnoses, recovered=False)

    return BatchResult(
        cases=cases,
        ledger=ledger,
        restraint=restraint,
        action_cost=action_cost,
        config_version=policy.config.version,
        dispatched=outbox.dispatched,
        deduped=outbox.deduped,
        cohorts_learned=cohort.cohorts_seen(),
    )


def _recover(case: Case, ledger, now: datetime, detail: str, cohort, diagnoses) -> None:
    case.state = CaseState.RECOVERED
    case.recovered_at = now
    case.terminal_reason = detail
    _outcome(ledger, case, now, "recovered", detail)
    if case.arm is Arm.TREATED:
        _observe(cohort, case, diagnoses, recovered=True)


def _observe(cohort, case: Case, diagnoses, *, recovered: bool) -> None:
    key = cohort.key(case.issuer, case.instrument, case.amount_band(), case.created_at.hour)
    cohort.observe(key, recovered)


def _outcome(ledger, case: Case, now: datetime, state: str, detail: str) -> None:
    ledger.append(
        CaseEvent(case.case_id, EventKind.OUTCOME, now,
                  payload={"state": state, "detail": detail,
                           "stopping_rule": case.stopping_rule},
                  actor="system")
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Backstop batch runner (dry-run world)")
    ap.add_argument("--cases", type=int, default=5000)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--holdout", type=float, default=0.10)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--html", metavar="PATH", default=None,
                    help="also write the 'Why not' view to this HTML file")
    ap.add_argument("--html-rows", type=int, default=2_000,
                    help="max stopped cases embedded in the HTML export")
    ap.add_argument("--html-timelines", type=int, default=250,
                    help="max cases carrying a full ledger replay in the export")
    args = ap.parse_args()

    result = run(args.cases, args.days, args.holdout, args.seed)

    print(render(result.cases, result.restraint, result.action_cost))
    print(f"ledger events: {len(result.ledger):,}   "
          f"dispatched: {result.dispatched:,}   deduped: {result.deduped:,}   "
          f"cohorts learned: {result.cohorts_learned:,}")

    if args.html:
        data = extract(result.cases, result.ledger,
                       max_rows=args.html_rows, max_timelines=args.html_timelines)
        write_html(data, args.html, config_version=result.config_version)
        print(f"\nwhy-not view: {args.html}   "
              f"({data.rows_total:,} stopped cases, "
              f"{data.timelines_shown:,} with full replay)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
