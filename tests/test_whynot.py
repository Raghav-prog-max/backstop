"""The Why-not view is an audit artefact, so its invariants are tested, not eyeballed."""

from __future__ import annotations

import json
import re

from backstop.domain.events import EventKind
from backstop.domain.types import CaseState, Disposition
from backstop.reporting.whynot import RULE_NAMES, extract, write_html
from backstop.runner import run


def small_batch():
    return run(n_cases=400, horizon_days=8, holdout=0.15, seed=11)


def test_no_action_is_dated_before_the_case_was_detected():
    """A ledger that replays work before the signal that caused it is not an audit trail."""
    result = small_batch()
    detected_at = {
        e.case_id: e.occurred_at
        for e in result.ledger.all_events()
        if e.kind is EventKind.DETECTED
    }
    offenders = [
        (e.case_id, e.kind.value, e.occurred_at)
        for e in result.ledger.all_events()
        if e.occurred_at < detected_at[e.case_id]
    ]
    assert not offenders, offenders[:5]


def test_replay_is_chronological():
    result = small_batch()
    data = extract(result.cases, result.ledger, max_timelines=40)
    timelines = [r["timeline"] for r in data.rows if r["timeline"]]
    assert timelines, "expected at least one case to carry a replay"
    for tl in timelines:
        stamps = [e["ts"] for e in tl]
        assert stamps == sorted(stamps)


def test_suppression_is_attributed_to_the_deciding_rule():
    """Not merely the first rule that fired — naming the wrong rule is worse than silence."""
    result = small_batch()
    suppressed = [c for c in result.cases if c.state is CaseState.SUPPRESSED]
    assert suppressed
    for case in suppressed:
        assert case.stopping_rule in RULE_NAMES
        # The recorded reason is written by the deciding rule, so they must agree.
        assert case.terminal_reason.startswith(case.stopping_rule)


def test_every_stopped_case_names_a_rule_that_stopped_it():
    result = small_batch()
    data = extract(result.cases, result.ledger)
    assert data.rows
    for row in data.rows:
        assert row["rules"] or row["stopping_rule"], row["id"]


def test_rollup_counts_reconcile_with_the_ledger():
    result = small_batch()
    data = extract(result.cases, result.ledger)

    from collections import Counter
    expected: Counter[str] = Counter()
    for e in result.ledger.all_events():
        if e.kind is not EventKind.POLICY_DECIDED:
            continue
        if e.payload.get("disposition") == Disposition.ALLOW.value:
            continue
        for rid in e.rule_ids:
            if rid in RULE_NAMES:
                expected[rid] += 1

    got = {r.rule_id: r.decisions for r in data.rules}
    assert got == {k: v for k, v in expected.items()}


def test_export_is_self_contained_and_embeds_valid_json(tmp_path):
    result = small_batch()
    data = extract(result.cases, result.ledger, max_rows=50, max_timelines=10)
    out = tmp_path / "why_not.html"
    write_html(data, str(out), config_version="cfg-test")

    html = out.read_text(encoding="utf-8")
    # No network dependency: the view must open from a filesystem path during the pitch.
    assert "http://" not in html.replace("http://localhost", "")
    assert "<script src" not in html

    payload = re.search(r"const DATA = (\{.*?\});\n", html, re.S).group(1)
    parsed = json.loads(payload)
    assert parsed["config_version"] == "cfg-test"
    assert len(parsed["rows"]) == data.rows_shown <= 50
    assert sum(1 for r in parsed["rows"] if r["timeline"]) <= 10


def test_row_cap_is_reported_honestly():
    result = small_batch()
    data = extract(result.cases, result.ledger, max_rows=5)
    assert data.rows_shown == 5
    assert data.rows_total > 5  # the view tells the reader it was capped
