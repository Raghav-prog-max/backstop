"""FastAPI surface over the batch pipeline.

SEAM: batches live in memory here. In production the ledger is Postgres and these
endpoints read the projection rather than a dict — the shapes below are the contract
either way, so the frontend does not change when the store does.

    python -m backstop.api            # http://127.0.0.1:8010
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from ..diagnosis.llm import ClaudeDiagnoser
from ..domain.case import Case
from ..domain.types import Arm, CaseState, rupees
from ..measurement.report import compute_lift, decompose
from ..reporting.whynot import RULE_NAMES, extract
from ..runner import BatchResult, run

app = FastAPI(title="Backstop", version="0.1.0")

# The console is served from a different origin in development.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5180", "http://127.0.0.1:5180"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@dataclass(slots=True)
class StoredBatch:
    batch_id: str
    params: dict[str, Any]
    result: BatchResult
    created_at: datetime = field(default_factory=datetime.now)


_BATCHES: dict[str, StoredBatch] = {}
_ORDER: list[str] = []
_LOCK = Lock()
_MAX_BATCHES = 8  # keep memory bounded; this is a demo surface, not a warehouse


def _store(batch: StoredBatch) -> None:
    with _LOCK:
        _BATCHES[batch.batch_id] = batch
        _ORDER.append(batch.batch_id)
        while len(_ORDER) > _MAX_BATCHES:
            _BATCHES.pop(_ORDER.pop(0), None)


def _get(batch_id: str) -> StoredBatch:
    batch = _BATCHES.get(batch_id)
    if batch is None:
        raise HTTPException(404, f"no batch {batch_id}; run one first")
    return batch


def _lift_json(lift) -> dict[str, Any]:
    return {
        "treated_n": lift.treated.n,
        "treated_rate": round(lift.treated.rate, 4),
        "holdout_n": lift.holdout.n,
        "holdout_rate": round(lift.holdout.rate, 4),
        "lift_pp": round(lift.lift_pp, 2),
        "ci_pp": round(lift.ci_pp, 2),
        "significant": lift.significant,
        "incremental_paise": lift.incremental_paise,
    }


@app.post("/api/batches")
def create_batch(
    cases: int = Query(4000, ge=100, le=40_000),
    days: int = Query(14, ge=1, le=60),
    holdout: float = Query(0.10, ge=0.02, le=0.5),
    seed: int = Query(42),
    llm: str = Query("auto", pattern="^(auto|claude|none)$"),
    llm_max: int = Query(400, ge=0, le=5000),
) -> dict[str, Any]:
    """Run a batch synchronously and return its summary.

    Synchronous on purpose: a 6,000-case batch takes about a second, and a job queue
    would be infrastructure the reviewer has to run for no benefit. With `llm=claude`
    it is as slow as the residual is large — the console shows the call count.
    """
    diagnoser = None
    if llm in ("auto", "claude"):
        diagnoser = ClaudeDiagnoser.from_env(max_calls=llm_max)
        if diagnoser is None and llm == "claude":
            raise HTTPException(
                400, "llm=claude needs ANTHROPIC_API_KEY on the API process and the "
                     "[llm] extra installed")
    result = run(cases, days, holdout, seed, llm=diagnoser)
    batch_id = f"b{len(_ORDER) + 1:03d}-{seed}-{cases}"
    stored = StoredBatch(batch_id, {"cases": cases, "days": days,
                                    "holdout": holdout, "seed": seed,
                                    "llm": result.llm.model or "none"}, result)
    _store(stored)
    return summary(batch_id)


@app.get("/api/batches")
def list_batches() -> list[dict[str, Any]]:
    return [
        {
            "batch_id": b.batch_id,
            "params": b.params,
            "created_at": b.created_at.isoformat(timespec="seconds"),
            "cases": len(b.result.cases),
        }
        for b in (_BATCHES[i] for i in reversed(_ORDER))
    ]


@app.get("/api/batches/{batch_id}")
def summary(batch_id: str) -> dict[str, Any]:
    b = _get(batch_id)
    cases = b.result.cases
    overall = compute_lift(cases)
    r = b.result.restraint
    treated = [c for c in cases if c.arm is Arm.TREATED]
    at_risk = sum(c.amount_paise for c in cases)

    return {
        "batch_id": b.batch_id,
        "params": b.params,
        "created_at": b.created_at.isoformat(timespec="seconds"),
        "config_version": b.result.config_version,
        "synthetic": True,
        "headline": {
            "cases": len(cases),
            "amount_at_risk_paise": at_risk,
            "amount_at_risk": rupees(at_risk),
            "gross_paise": overall.treated.amount_recovered,
            "gross": rupees(overall.treated.amount_recovered),
            "incremental_paise": overall.incremental_paise,
            "incremental": rupees(overall.incremental_paise),
            "action_cost_paise": b.result.action_cost,
            "action_cost": rupees(b.result.action_cost),
            "net_paise": overall.incremental_paise - b.result.action_cost,
            "net": rupees(overall.incremental_paise - b.result.action_cost),
        },
        "lift": _lift_json(overall),
        "decomposition": {
            name: {k: _lift_json(v) for k, v in decompose(cases, fn).items()}
            for name, fn in (
                ("case_type", lambda c: c.case_type.value),
                ("cause", lambda c: c.cause.value),
                ("amount_band", lambda c: c.amount_band()),
                ("issuer", lambda c: c.issuer),
                ("tier", lambda c: c.tier or "n/a"),
            )
        },
        "llm": {
            "enabled": b.result.llm.enabled,
            "model": b.result.llm.model,
            "residual_cases": b.result.llm.residual_cases,
            "calls": b.result.llm.calls,
            "resolved": b.result.llm.resolved,
            "refusals": b.result.llm.refusals,
            "input_tokens": b.result.llm.input_tokens,
            "output_tokens": b.result.llm.output_tokens,
        },
        "restraint": {
            "suppressed": r.suppressed,
            "escalated": r.escalated,
            "contacts_sent": r.contacts_sent,
            "contacts_per_recovery": round(
                r.contacts_per_recovery(overall.treated.recovered), 3),
            "opt_outs": r.opt_outs,
            "retry_ruled_out_by_network": r.retry_ruled_out_by_network,
            "fee_events_avoided": r.fee_events_avoided,
            "promises_made": r.promises_made,
            "promises_kept": r.promises_kept,
            "promises_broken": r.promises_broken,
            "denied_by_rule": dict(sorted(r.denied_by_rule.items())),
            "amount_withheld_paise": sum(
                c.amount_paise for c in cases if c.state is CaseState.SUPPRESSED),
        },
        "ledger_events": len(b.result.ledger),
        "treated": len(treated),
    }


@app.get("/api/batches/{batch_id}/rules")
def rules(batch_id: str) -> list[dict[str, Any]]:
    b = _get(batch_id)
    data = extract(b.result.cases, b.result.ledger, max_rows=1, max_timelines=0)
    return [
        {
            "rule_id": r.rule_id,
            "name": r.name,
            "decisions": r.decisions,
            "cases_stopped": r.cases_stopped,
            "contacts_prevented": r.contacts_prevented,
            "amount_withheld_paise": r.amount_withheld,
            "dispositions": r.dispositions,
        }
        for r in data.rules
    ]


@app.get("/api/batches/{batch_id}/cases")
def stopped_cases(
    batch_id: str,
    rule: str | None = None,
    disposition: str | None = None,
    state: str | None = None,
    q: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Cases the policy engine stopped at least once — the Why-not feed."""
    b = _get(batch_id)
    data = extract(b.result.cases, b.result.ledger, max_rows=20_000, max_timelines=0)

    rows = data.rows
    if rule:
        rows = [r for r in rows if rule in r["rules"] or r["stopping_rule"] == rule]
    if disposition:
        rows = [
            r for r in rows
            if any(d["disposition"] == disposition for d in r["decisions"])
            or (disposition == "suppress" and r["state"] == "suppressed")
        ]
    if state:
        rows = [r for r in rows if r["state"] == state]
    if q:
        needle = q.lower()
        rows = [
            r for r in rows
            if needle in " ".join(
                [r["id"], r["cause"], r["issuer"], r["reason"], r["type"],
                 r["stopping_rule"], " ".join(r["rules"])]).lower()
        ]

    page = rows[offset: offset + limit]
    for row in page:
        row.pop("timeline", None)
        row.pop("decisions", None)
    return {
        "total": len(rows),
        "offset": offset,
        "limit": limit,
        "states": sorted({r["state"] for r in data.rows}),
        "rows": page,
    }


@app.get("/api/batches/{batch_id}/cases/{case_id}")
def case_replay(batch_id: str, case_id: str) -> dict[str, Any]:
    """Everything the ledger holds for one case, in order. The audit trail."""
    b = _get(batch_id)
    case: Case | None = next(
        (c for c in b.result.cases if c.case_id == case_id), None)
    if case is None:
        raise HTTPException(404, f"no case {case_id} in {batch_id}")

    events = sorted(b.result.ledger.events_for(case_id), key=lambda e: e.occurred_at)
    return {
        "case": {
            "id": case.case_id,
            "type": case.case_type.value,
            "amount_paise": case.amount_paise,
            "amount": rupees(case.amount_paise),
            "issuer": case.issuer,
            "network": case.network,
            "advice_code": case.advice_code,
            "instrument": case.instrument,
            "failure_code": case.failure_code,
            "cause": case.cause.value,
            "tier": case.tier,
            "free_text": case.free_text,
            "due_at": case.due_at.isoformat(sep=" ", timespec="minutes") if case.due_at else None,
            "promise_status": case.promise_status,
            "promise_until": (case.promise_until.isoformat(sep=" ", timespec="minutes")
                              if case.promise_until else None),
            "promises_broken": case.promises_broken,
            "state": case.state.value,
            "arm": case.arm.value,
            "stopping_rule": case.stopping_rule,
            "reason": case.terminal_reason,
            "contacts": case.contacts_total,
            "retries": case.retries_used,
        },
        "events": [
            {
                "ts": e.occurred_at.isoformat(sep=" ", timespec="minutes"),
                "kind": e.kind.value,
                "actor": e.actor,
                "rules": list(e.rule_ids),
                "payload": {k: str(v) for k, v in e.payload.items()},
            }
            for e in events
        ],
    }


@app.get("/api/rules")
def rule_catalogue() -> dict[str, str]:
    return dict(RULE_NAMES)


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "batches": len(_ORDER)}
