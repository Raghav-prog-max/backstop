"""Batch runner — the whole pipeline over a synthetic batch, day by day.

    python -m backstop.runner --cases 5000 --days 14

Holdout cases are admitted to the world and stepped for self-heal, but never
diagnosed, never planned and never contacted. That is the point of them.
"""

from __future__ import annotations

import argparse
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .diagnosis.cohort import CohortModel
from .diagnosis.engine import DiagnosisEngine, LLMDiagnoser
from .diagnosis.llm import ClaudeDiagnoser
from .diagnosis.taxonomy import CODE_TO_CAUSE
from .domain.case import Case
from .domain.events import CaseEvent, EventKind
from .domain.types import Arm, CaseState, CauseClass, Disposition
from .execution.outbox import Outbox
from .ledger.sqlite import SqliteLedger
from .ledger.store import InMemoryLedger, LedgerStore
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
# Gateway codes T1 can read. Anything else with free text attached is the T3 residual.
_KNOWN_CODES = frozenset(CODE_TO_CAUSE)


@dataclass(slots=True)
class LLMStats:
    """What T3 cost and what it resolved. Zero everywhere when the model is off."""

    enabled: bool = False
    model: str = ""
    calls: int = 0
    resolved: int = 0      # UNKNOWN -> a named cause, with grounded evidence
    refusals: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    residual_cases: int = 0  # cases that reached T3 (or would have, if it were on)


@dataclass(slots=True)
class BatchResult:
    cases: list[Case]
    ledger: LedgerStore
    restraint: Restraint
    action_cost: int
    config_version: str
    dispatched: int
    deduped: int
    cohorts_learned: int
    llm: LLMStats = field(default_factory=LLMStats)


def run(
    n_cases: int,
    horizon_days: int,
    holdout: float,
    seed: int,
    *,
    ledger: LedgerStore | None = None,
    llm: LLMDiagnoser | None = None,
) -> BatchResult:
    start = datetime(2026, 9, 1, 9, 0)
    rng = random.Random(seed)

    ledger = ledger if ledger is not None else InMemoryLedger()
    cohort = CohortModel()
    diagnoser = DiagnosisEngine(cohort, llm=llm)
    llm_stats = LLMStats(enabled=llm is not None,
                         model=getattr(llm, "model", type(llm).__name__ if llm else ""))
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
        dx = diagnoser.diagnose(case, case.free_text)
        case.cause = dx.cause_class
        case.recoverability = dx.recoverability
        case.tier = dx.tier
        diagnoses[case.case_id] = dx
        world.admit(case, dx.cause_class)

        # The residual: T1 could not read the code and the case carries free text.
        # This is the only population the model is ever shown.
        if case.free_text and case.failure_code not in _KNOWN_CODES:
            llm_stats.residual_cases += 1
            if dx.tier == "T3" and dx.cause_class is not CauseClass.UNKNOWN:
                llm_stats.resolved += 1

        if case.arm is Arm.HOLDOUT:
            continue  # never diagnosed into the pipeline, never touched

        # The planner never proposes a retry the network ruled out, so PR-04's advice
        # branch is defence in depth and would count nothing. The saving is real here.
        if dx.advice is not None and not dx.advice.retryable:
            restraint.retry_ruled_out_by_network += 1
            if dx.advice.penalised_if_retried:
                restraint.fee_events_avoided += 1

        case.transition(CaseState.DIAGNOSED)
        payload = {"cause": dx.cause_class.value, "tier": dx.tier,
                   "recoverability": round(dx.recoverability, 4),
                   "posterior_n": dx.posterior_n}
        if dx.tier == "T3":
            # A model diagnosis lands in the ledger with the spans that justify it,
            # so the replay shows what the model read — not just what it concluded.
            payload["model"] = llm_stats.model
            payload["evidence"] = [e.raw_value for e in dx.evidence if e.tier == "T3"]
        ledger.append(
            CaseEvent(case.case_id, EventKind.DIAGNOSED, case.created_at,
                      payload=payload, actor="diagnosis")
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
                # The batch horizon is shorter than the 30-day network window, so
                # retries on this case are exactly the retries in that window.
                network=case.network,
                retries_in_network_window=case.retries_used,
                advice=dx.advice,
                last_decline_at=last_retry.get(case.case_id, case.created_at),
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

    if isinstance(llm, ClaudeDiagnoser):
        llm_stats.calls = llm.calls
        llm_stats.refusals = llm.refusals
        llm_stats.input_tokens = llm.input_tokens
        llm_stats.output_tokens = llm.output_tokens

    return BatchResult(
        cases=cases,
        ledger=ledger,
        restraint=restraint,
        action_cost=action_cost,
        config_version=policy.config.version,
        dispatched=outbox.dispatched,
        deduped=outbox.deduped,
        cohorts_learned=cohort.cohorts_seen(),
        llm=llm_stats,
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
    ap.add_argument("--ledger", choices=("memory", "sqlite"), default="memory",
                    help="where the append-only ledger lives for this run")
    ap.add_argument("--db", metavar="PATH", default="backstop.db",
                    help="SQLite file for --ledger sqlite (default: backstop.db)")
    ap.add_argument("--llm", choices=("auto", "claude", "none"), default="auto",
                    help="T3 diagnoser for the free-text residual. auto = Claude when "
                         "ANTHROPIC_API_KEY is set, otherwise none (the default NoLLM)")
    ap.add_argument("--llm-max", type=int, default=400,
                    help="hard cap on model calls per batch; beyond it T3 answers UNKNOWN")
    args = ap.parse_args()

    ledger: LedgerStore
    if args.ledger == "sqlite":
        ledger = SqliteLedger(args.db)
    else:
        ledger = InMemoryLedger()

    llm: LLMDiagnoser | None = None
    if args.llm in ("auto", "claude"):
        llm = ClaudeDiagnoser.from_env(max_calls=args.llm_max)
        if llm is None and args.llm == "claude":
            ap.error("--llm claude needs ANTHROPIC_API_KEY and `pip install -e \".[llm]\"`")

    result = run(args.cases, args.days, args.holdout, args.seed, ledger=ledger, llm=llm)

    print(render(result.cases, result.restraint, result.action_cost, llm=result.llm))
    print(f"ledger events: {len(result.ledger):,}   "
          f"dispatched: {result.dispatched:,}   deduped: {result.deduped:,}   "
          f"cohorts learned: {result.cohorts_learned:,}"
          + (f"   ledger: {args.db}" if args.ledger == "sqlite" else ""))

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
