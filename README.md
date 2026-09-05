# Backstop

**Razorpay AI Buildathon — Track 03, AI Revenue Recovery**

An agent that detects revenue at risk, diagnoses why it is leaking, and executes
bounded recovery workflows under a compliance budget it is not allowed to overspend.

**Architecture: [ARCHITECTURE.md](ARCHITECTURE.md)** — the canonical document, with
every section linked to the code that implements it.
A [formatted standalone version](https://claude.ai/code/artifact/818474d5-fab3-426f-8b10-45b268c00c1e)
is also published.

---

## Run it

No infrastructure required — no database, no queue, no API keys. The core pipeline is
stdlib-only; only the test runner is a dependency.

```bash
python -m pip install -e ".[dev]"
```

```bash
python -m backstop.runner --cases 6000 --days 14
```

Add `--html why_not.html` to also write the **Why not** view — a self-contained page
(no server, no CDN, no build step) listing every case the policy engine declined to
work, and the exact rule that stopped it:

```bash
python -m backstop.runner --cases 6000 --days 14 --html why_not.html
```

```bash
python -m pytest -q
```

### Persist the ledger (optional)

The default ledger is in memory. `--ledger sqlite` writes the same append-only
`case_event` table to a file instead, so the audit trail outlives the process and can
be queried with plain `sqlite3`:

```bash
python -m backstop.runner --cases 6000 --days 14 --ledger sqlite --db backstop.db
```

### Turn the model on (optional)

T3 — the only tier that calls a model — runs on the **free-text residual**: cases whose
gateway code maps to nothing (`payment_failed`, an unmapped issuer response) but which
carry a customer reply, a support note or a forwarded bank SMS. About 7% of the
synthetic batch. Everything else is diagnosed by code tables and the cohort posterior,
and no model call is ever made for it.

```bash
python -m pip install -e ".[llm]"
export ANTHROPIC_API_KEY=sk-ant-...
python -m backstop.runner --cases 6000 --days 14 --llm claude
```

`--llm auto` (the default) uses Claude when a key is present and `NoLLM` otherwise —
either way the report's `MODEL` block says which, and the `by diagnosis tier` slice
shows T3's lift measured against the same holdout as everything else. `--llm-max`
caps calls per batch. A model answer whose quoted evidence is not actually in the
text is discarded as `UNKNOWN`, whatever its confidence field claims
(`diagnosis/llm.py`, `tests/test_t3.py`).

### The console (optional)

A React operator console over a FastAPI surface — batch report, Why-not view and
ledger replay, driven live rather than exported. Two processes:

```bash
python -m pip install -e ".[serve]" && python -m backstop.api
```

```bash
cd console && npm install && npm run dev
```

Then open <http://localhost:5180>. The CLI above stays stdlib-only and needs none of
this; the console is an additional surface, not a replacement.

Prefer not to install? Both work from a clean checkout with the source path set:

```bash
PYTHONPATH=src python -m backstop.runner --cases 6000 --days 14
```

## The reframe

The obvious build for this track is an LLM that writes better dunning emails. None of
the four acceptance criteria are about generation quality — all four are about
restraint and proof.

> Every at-risk rupee is a sequential decision under a compliance budget.
> The model proposes; a deterministic policy engine disposes.

So the LLM is one narrow, schema-constrained component on the residual cases that
structured signals cannot classify (`diagnosis/llm.py`, tier T3 — `claude-opus-5` with
a closed cause enum and mandatory verbatim evidence spans; `NoLLM` when no key is set).
It names a cause. It never picks an action. Everything that decides whether to spend a
contact, a retry, or a phone call is ordinary versioned, unit-tested code in `policy/`.

## Where the acceptance criteria live

| Stated bar | Code | Evidence |
|---|---|---|
| Measurable money recovered across batch | `measurement/report.py` | Batch report headline: at risk in, gross, **incremental ± CI**, net |
| Compliant escalation protocols | `policy/rules.py` | PR-01…PR-08, each recorded per decision |
| Stopping rules and audit trails | `policy/` + `ledger/` | `denied / deferred by rule` block; full replay from `case_event` |
| Evidence beyond cherry-picked successes | `measurement/report.py` | Lift decomposed by cause, amount band and issuer, with per-cohort n |

## Layout

```
src/backstop/
  domain/       types, the append-only event record, the case state machine
  ledger/       LedgerStore protocol + in-memory and SQLite implementations
  diagnosis/    T0 network advice -> T1 code taxonomy -> T2 cohort posterior -> T3 LLM
  policy/       PR-01..PR-08, versioned config, the engine that disposes
  planner/      action space and the scored policy that picks action + timing
  execution/    transactional outbox (exactly-once dispatch)
  measurement/  holdout assignment, lift with CI, cohort decomposition
  reporting/    the "Why not" view — a self-contained HTML audit surface
  api/          FastAPI surface the console reads (optional [serve] extra)
  sim/          synthetic generator and the dry-run world
  runner.py     batch runner
console/        React + Vite operator console (optional)
```

## The "Why not" view

The screen that answers *stopping rules and audit trails*. It is close to free to build,
because the policy engine records every evaluation whether or not it allowed anything
— nothing in it is instrumentation added after the fact, it is a read over the ledger.

- **Rule rollup** — each of PR-01…PR-08 with decisions made, cases ended, and contacts
  prevented. Click one to filter. Note that `deny` and `defer` rules stop *actions*,
  not cases, so their "cases ended" is legitimately zero; only `suppress` and
  `hard stop` terminate a case.
- **Stopped cases** — filterable by rule, disposition, outcome and free text. A case
  appears if it ended suppressed **or** if any action proposed for it was denied,
  deferred or hard-stopped, including cases that recovered anyway. Showing only the
  suppressed ones would hide the restraint applied to successful cases.
- **Ledger replay** — select any row for that case's full event history in order:
  signal, diagnosis, every policy decision with the rules that fired, every dispatch,
  the outcome.

A representative replay, straight out of the view:

```
2026-09-02 08:29  detected        amount_paise=2369936 arm=treated
2026-09-02 08:29  diagnosed       cause=do_not_honour tier=T1 recoverability=0.3
2026-09-02 19:00  policy_decided  retry_payment -> defer  PR-05: pre-debit notice
2026-09-03 19:00  policy_decided  retry_payment -> defer  PR-05: pre-debit notice
2026-09-04 19:00  policy_decided  retry_payment -> allow
2026-09-04 19:00  action_result   ok=False  no response
2026-09-05 19:00  action_result   ok=False  no response
2026-09-06 19:00  action_result   ok=True   recovered_paise=2369936
```

Exports are capped (`--html-rows`, default 2000; `--html-timelines`, default 250) and
the view states its own caps on screen rather than silently truncating.

## Design notes worth knowing before reading the code

**The ledger is the spine.** One append-only event table, one derived case projection.
Audit trail and replay fall out of it rather than being bolted on. If the projection
is ever wrong it is rebuilt, not patched.

**Suppression is reached from `diagnosed`, not from `attempting`.** A suppressed case
is a decision the system made, not an attempt that failed, and the state machine
enforces that — mixing the two would corrupt the reporting. See
`test_ledger_and_state.py`.

**Four dispositions, deliberately distinct.** `deny` (this action, this channel),
`defer` (right action, wrong time — requeued, never dropped), `suppress` (case not
worth working), `hard stop` (cease all contact). Collapsing them is how compliance
bugs hide.

**The network usually knows better than we do.** A decline response often carries a
Mastercard Merchant Advice Code or a Visa decline category, and that instruction
outranks the taxonomy and the cohort model: MAC 03 means never retry (and reattempting
is a per-attempt fee), MAC 01 means fetch a new credential rather than retry, and MAC
24-30 give the retry time directly. An unrecognised code yields *no advice* rather than
a guess. See `diagnosis/advice.py`.

**Timing beats copy.** Retrying an `insufficient_funds` decline the day after salary
credit is a larger lever than any message rewrite, so `wait(until)` is a first-class
action and it is the default one.

**Exploration is load-bearing.** The planner's ε-greedy is not decoration — it
generates the variance the measurement layer needs to attribute anything. A purely
greedy planner produces a system that cannot explain its own results.

**Duplicate contact is a compliance incident, not a bug.** Hence the outbox: intent
committed to the ledger before dispatch, keyed by a stable idempotency key.

**A promise is not a recovery.** B2B receivables (`invoice_overdue`, ~16% of the batch)
swap the retry ladder for reminder / promise-to-pay / human. A buyer's commitment to a
date puts the case on PR-08 hold; on that date the ledger records exactly one of
`promise kept` (a recovery) or `promise broken` (the planner's cue to bring in a
human). The report shows obtained, kept and broken side by side. A disputed invoice
(`invoice_query`) is never chased — it goes straight to a human. See
`tests/test_receivables.py`.

**Escalation is terminal for the agent, not for the customer.** An escalated case keeps
its chance to pay on its own, as its holdout twin would. Otherwise the treated arm would
be charged the lost self-heal for handing a case to a human — receivables showed a flat
lift until this was corrected, and the fix is a measurement rule, not a tuning knob.

## On the numbers

The demo runs on a **synthetic generator**, declared as such here, in
`sim/generator.py`, and on the report screen itself. The mixes are hand-calibrated to
plausible ranges — they are not measured production rates.

Two statistical choices that matter:

- The interval on the difference of proportions is **Agresti–Caffo**, not Wald. A
  plain Wald interval collapses to ±0 when a recovery rate hits 0 or 1, which reports
  a one-versus-one cohort as significant — the exact overstatement this layer exists
  to prevent.
- Significance is additionally gated on `MIN_N_PER_ARM`. A cohort of six that happens
  to recover is not a finding, and the report marks it `(thin)`.

## Before submitting

- [x] **Regulatory constants verified** (2026-09-05) — `cfg-2026.09.1`. Sources are
      cited in `policy/config.py` and each value is pinned by a test in
      `tests/test_regulatory.py`. Re-check before submission; these move.
- [x] `SqliteLedger` available in the runner (`--ledger sqlite`). Postgres stays a seam.
- [x] T3 wired to Claude (`--llm claude`), grounded-evidence contract tested without network.
- [x] B2B receivables with promise-to-pay tracking; batches reproducible seed-for-seed
      (case ids come from the seed, so holdout arms no longer reshuffle between runs).
- [ ] Run one batch with `--llm claude` and keep the report — the `MODEL` block and the
      `by diagnosis tier` slice are the evidence that the model earns its place.
- [ ] Record the 5-minute pitch video against a live batch run and the Why-not view.
