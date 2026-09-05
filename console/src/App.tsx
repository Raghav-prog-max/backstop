import { useCallback, useEffect, useState } from "react";
import {
  api,
  rupees,
  type CasePage,
  type CaseRow,
  type Lift,
  type LlmMode,
  type LlmStats,
  type Replay,
  type RuleRollup,
  type Summary,
} from "./api";

type Tab = "report" | "whynot";

const DEFAULTS = { cases: 6000, days: 14, holdout: 0.1, seed: 42, llm: "auto" as LlmMode };

export default function App() {
  const [params, setParams] = useState(DEFAULTS);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [rules, setRules] = useState<RuleRollup[]>([]);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("report");

  const runBatch = useCallback(async (p: typeof DEFAULTS) => {
    setRunning(true);
    setError(null);
    try {
      const s = await api.runBatch(p);
      setSummary(s);
      setRules(await api.rules(s.batch_id));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRunning(false);
    }
  }, []);

  // Open on a real batch rather than an empty shell — the console should show what
  // it does before anyone touches a control.
  useEffect(() => {
    void runBatch(DEFAULTS);
  }, [runBatch]);

  return (
    <>
      <header className="top">
        <div className="top-in">
          <h1 className="brand">
            <span>Razorpay Buildathon · Track 03</span>
            Backstop console
          </h1>
          <div className="spacer" />
          <label className="field">
            cases
            <input
              type="number"
              min={100}
              max={40000}
              step={500}
              value={params.cases}
              onChange={(e) =>
                setParams({ ...params, cases: Number(e.target.value) })
              }
            />
          </label>
          <label className="field">
            days
            <input
              type="number"
              min={1}
              max={60}
              value={params.days}
              onChange={(e) => setParams({ ...params, days: Number(e.target.value) })}
            />
          </label>
          <label className="field">
            holdout
            <input
              type="number"
              min={0.02}
              max={0.5}
              step={0.01}
              value={params.holdout}
              onChange={(e) =>
                setParams({ ...params, holdout: Number(e.target.value) })
              }
            />
          </label>
          <label className="field">
            seed
            <input
              type="number"
              value={params.seed}
              onChange={(e) => setParams({ ...params, seed: Number(e.target.value) })}
            />
          </label>
          <label className="field" title="T3 diagnoser for the free-text residual. auto = Claude when the API process has ANTHROPIC_API_KEY, otherwise NoLLM.">
            model
            <select
              value={params.llm}
              onChange={(e) => setParams({ ...params, llm: e.target.value as LlmMode })}
            >
              <option value="auto">auto</option>
              <option value="claude">claude</option>
              <option value="none">none</option>
            </select>
          </label>
          <button
            className="primary"
            disabled={running}
            onClick={() => void runBatch(params)}
          >
            {running ? "Running…" : "Run batch"}
          </button>
        </div>
      </header>

      <p className="banner">
        Synthetic data — hand-calibrated, not measured production rates
        {summary ? ` · policy ${summary.config_version}` : ""}
      </p>

      <main>
        {error && (
          <div className="err">
            <strong>Could not reach the API.</strong> {error}
            <br />
            <span className="muted">
              Start it with <code>python -m backstop.api</code> on port 8010, then run
              a batch again.
            </span>
          </div>
        )}

        {!summary && !error && <p className="empty">Running the first batch…</p>}

        {summary && (
          <>
            <nav className="tabs" role="tablist">
              <button
                role="tab"
                aria-selected={tab === "report"}
                onClick={() => setTab("report")}
              >
                Batch report
              </button>
              <button
                role="tab"
                aria-selected={tab === "whynot"}
                onClick={() => setTab("whynot")}
              >
                Why not
              </button>
            </nav>

            {tab === "report" ? (
              <BatchReport summary={summary} />
            ) : (
              <WhyNot summary={summary} rules={rules} />
            )}
          </>
        )}
      </main>
    </>
  );
}

/* ---------------------------------------------------------------- report -- */

/** T3 is the only tier that calls a model, and it only ever sees the free-text
 *  residual. Shown whether or not the model was on, so an absent model reads as
 *  "off" rather than as a missing panel. */
function ModelPanel({ llm }: { llm: LlmStats }) {
  return (
    <div className="panel" style={{ marginTop: 18 }}>
      <h2>Model (T3 — free-text residual only)</h2>
      {llm.enabled ? (
        <>
          <Row k="Model" v={llm.model} />
          <Row k="Residual cases" v={llm.residual_cases.toLocaleString()} />
          <Row k="Model calls" v={llm.calls.toLocaleString()} />
          <Row k="Resolved to a cause" v={llm.resolved.toLocaleString()} />
          <Row k="Refusals" v={llm.refusals.toLocaleString()} />
          <Row
            k="Tokens in / out"
            v={`${llm.input_tokens.toLocaleString()} / ${llm.output_tokens.toLocaleString()}`}
          />
        </>
      ) : (
        <>
          <Row k="Status" v="off — NoLLM, residual diagnosed unknown" />
          <Row k="Residual cases" v={llm.residual_cases.toLocaleString()} />
        </>
      )}
      <p className="muted" style={{ fontSize: 13, marginBottom: 0 }}>
        The model names a cause and never picks an action. Its lift is in the
        <em> by tier</em> table below, measured against the same holdout as everything else.
      </p>
    </div>
  );
}

function BatchReport({ summary }: { summary: Summary }) {
  const h = summary.headline;
  const r = summary.restraint;
  return (
    <>
      <dl className="kpis">
        <Kpi label="Amount at risk" value={h.amount_at_risk} />
        <Kpi label="Recovered gross" value={h.gross} />
        <Kpi label="Recovered incremental" value={h.incremental} accent
          sub="attributable to the agent" />
        <Kpi label="Action cost" value={h.action_cost} small />
        <Kpi label="Net" value={h.net} accent small />
      </dl>

      <div className="grid2">
        <div className="panel">
          <h2>Lift vs untouched holdout</h2>
          <LiftBars lift={summary.lift} />
          <p className="muted" style={{ fontSize: 13, marginBottom: 0 }}>
            Gross recovery credits the agent with everything the treated arm recovered.
            Only the gap over the holdout is attributable — here{" "}
            <strong>{rupees(summary.lift.incremental_paise)}</strong> of{" "}
            {h.gross}.
          </p>
        </div>

        <div className="panel">
          <h2>Restraint</h2>
          <Row k="Cases suppressed" v={r.suppressed.toLocaleString()} />
          <Row k="Money not chased" v={rupees(r.amount_withheld_paise)} />
          <Row k="Contacts sent" v={r.contacts_sent.toLocaleString()} />
          <Row k="Contacts per recovery" v={r.contacts_per_recovery.toFixed(2)} />
          <Row k="Escalated to human" v={r.escalated.toLocaleString()} />
          <Row k="Opt-outs caused" v={r.opt_outs.toLocaleString()} />
          <Row
            k="Retry ruled out by network"
            v={r.retry_ruled_out_by_network.toLocaleString()}
          />
          <Row k="…of which fee events" v={r.fee_events_avoided.toLocaleString()} />
          <Row k="Ledger events" v={summary.ledger_events.toLocaleString()} />
        </div>
      </div>

      <ModelPanel llm={summary.llm} />

      {Object.entries(summary.decomposition).map(([dim, buckets]) => (
        <div className="panel" key={dim} style={{ marginTop: 18 }}>
          <h2>Lift by {dim.replace("_", " ")}</h2>
          <div className="scroll">
            <table>
              <thead>
                <tr>
                  <th>{dim.replace("_", " ")}</th>
                  <th className="num">lift pp</th>
                  <th className="num">± 95%</th>
                  <th className="num">n treated</th>
                  <th className="num">n holdout</th>
                  <th>verdict</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(buckets).map(([name, lf]) => (
                  <tr key={name}>
                    <td className="mono">{name}</td>
                    <td className="num">
                      {lf.lift_pp > 0 ? "+" : ""}
                      {lf.lift_pp.toFixed(1)}
                    </td>
                    <td className="num">{lf.ci_pp.toFixed(1)}</td>
                    <td className="num">{lf.treated_n.toLocaleString()}</td>
                    <td className="num">{lf.holdout_n.toLocaleString()}</td>
                    <td>
                      {lf.significant ? (
                        <span className="pill p-suppress">significant</span>
                      ) : (
                        <span className="pill p-defer">thin</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ))}
    </>
  );
}

function LiftBars({ lift }: { lift: Lift }) {
  const scale = Math.max(lift.treated_rate, lift.holdout_rate, 0.01);
  const pct = (x: number) => `${(x * 100).toFixed(1)}%`;
  return (
    <>
      <div className="armrow">
        <span>treated</span>
        <span className="bar">
          <i style={{ width: `${(lift.treated_rate / scale) * 100}%` }} />
        </span>
        <span>
          {pct(lift.treated_rate)} · n={lift.treated_n.toLocaleString()}
        </span>
      </div>
      <div className="armrow">
        <span>holdout</span>
        <span className="bar">
          <i className="muted" style={{ width: `${(lift.holdout_rate / scale) * 100}%` }} />
        </span>
        <span>
          {pct(lift.holdout_rate)} · n={lift.holdout_n.toLocaleString()}
        </span>
      </div>
      <p className="liftline">
        lift {lift.lift_pp > 0 ? "+" : ""}
        {lift.lift_pp.toFixed(1)} pp ± {lift.ci_pp.toFixed(1)}{" "}
        {lift.significant ? (
          "(significant at 95%)"
        ) : (
          <span className="flag">(not significant)</span>
        )}
      </p>
    </>
  );
}

function Kpi(props: {
  label: string;
  value: string;
  accent?: boolean;
  small?: boolean;
  sub?: string;
}) {
  return (
    <div className="kpi">
      <dt>{props.label}</dt>
      <dd className={`${props.accent ? "accent" : ""} ${props.small ? "small" : ""}`}>
        {props.value}
      </dd>
      {props.sub && <div className="sub">{props.sub}</div>}
    </div>
  );
}

function Row({ k, v }: { k: string; v: string }) {
  return (
    <div
      style={{
        display: "flex",
        justifyContent: "space-between",
        gap: 12,
        padding: "6px 0",
        borderBottom: "1px solid var(--rule)",
        fontSize: 13,
      }}
    >
      <span className="muted">{k}</span>
      <span className="mono" style={{ fontVariantNumeric: "tabular-nums" }}>
        {v}
      </span>
    </div>
  );
}

/* ---------------------------------------------------------------- why-not -- */

function WhyNot({ summary, rules }: { summary: Summary; rules: RuleRollup[] }) {
  const [rule, setRule] = useState("");
  const [disposition, setDisposition] = useState("");
  const [state, setState] = useState("");
  const [q, setQ] = useState("");
  const [page, setPage] = useState<CasePage | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [replay, setReplay] = useState<Replay | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let live = true;
    setLoading(true);
    api
      .cases(summary.batch_id, { rule, disposition, state, q, limit: 150 })
      .then((p) => live && setPage(p))
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, [summary.batch_id, rule, disposition, state, q]);

  useEffect(() => {
    if (!selected) return setReplay(null);
    let live = true;
    api.replay(summary.batch_id, selected).then((r) => live && setReplay(r));
    return () => {
      live = false;
    };
  }, [summary.batch_id, selected]);

  return (
    <div className="grid2">
      <div style={{ minWidth: 0 }}>
        <div className="controls">
          <input
            type="search"
            placeholder="Search case id, cause, issuer, reason…"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
          <select value={disposition} onChange={(e) => setDisposition(e.target.value)}>
            <option value="">All dispositions</option>
            <option value="suppress">suppress</option>
            <option value="deny">deny</option>
            <option value="defer">defer</option>
            <option value="hard_stop">hard stop</option>
          </select>
          <select value={state} onChange={(e) => setState(e.target.value)}>
            <option value="">All outcomes</option>
            {(page?.states ?? []).map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
          <button
            onClick={() => {
              setRule("");
              setDisposition("");
              setState("");
              setQ("");
            }}
          >
            Clear
          </button>
          <span className="count">
            {loading
              ? "loading…"
              : page
                ? `${page.rows.length.toLocaleString()} of ${page.total.toLocaleString()} stopped cases`
                : ""}
          </span>
        </div>

        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th>Case</th>
                <th className="num">Amount</th>
                <th>Cause</th>
                <th>Issuer</th>
                <th>Outcome</th>
                <th>Stopped by</th>
                <th>Rules fired</th>
                <th className="num">Stops</th>
              </tr>
            </thead>
            <tbody>
              {(page?.rows ?? []).map((row) => (
                <CaseRowView
                  key={row.id}
                  row={row}
                  selected={row.id === selected}
                  onSelect={() => setSelected(row.id === selected ? null : row.id)}
                />
              ))}
            </tbody>
          </table>
          {page && page.rows.length === 0 && (
            <p className="empty">No cases match these filters.</p>
          )}
        </div>

        {replay && <ReplayPanel replay={replay} />}
      </div>

      <div className="panel">
        <h2>Stopped by rule</h2>
        {rules.map((r) => (
          <button
            key={r.rule_id}
            className="rulebtn"
            aria-pressed={rule === r.rule_id}
            onClick={() => setRule(rule === r.rule_id ? "" : r.rule_id)}
          >
            <span className="rid">{r.rule_id}</span>
            <span className="rn">{r.name}</span>
            <span className="rs">
              {r.decisions.toLocaleString()} decisions ·{" "}
              {r.cases_stopped.toLocaleString()} cases ended ·{" "}
              {r.contacts_prevented.toLocaleString()} contacts stopped
            </span>
          </button>
        ))}
        <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>
          <code>deny</code> and <code>defer</code> rules stop actions, not cases, so
          their “cases ended” is legitimately zero. Only <code>suppress</code> and{" "}
          <code>hard stop</code> terminate a case.
        </p>
      </div>
    </div>
  );
}

function CaseRowView({
  row,
  selected,
  onSelect,
}: {
  row: CaseRow;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <tr className={`click ${selected ? "sel" : ""}`} onClick={onSelect}>
      <td className="mono">{row.short}</td>
      <td className="num">{row.amount_fmt}</td>
      <td>{row.cause}</td>
      <td>{row.issuer}</td>
      <td>
        <span className={`pill p-${row.state}`}>{row.state}</span>
      </td>
      <td>
        {row.stopping_rule ? (
          <>
            <span className="mono">{row.stopping_rule}</span>
            <div className="muted" style={{ fontSize: 11.5 }}>
              {row.reason}
            </div>
          </>
        ) : (
          <span className="muted">—</span>
        )}
      </td>
      <td>
        {row.rules.map((r) => (
          <span className="pill" key={r}>
            {r}
          </span>
        ))}
      </td>
      <td className="num">{row.n_stops}</td>
    </tr>
  );
}

function ReplayPanel({ replay }: { replay: Replay }) {
  const c = replay.case;
  return (
    <div className="panel replay">
      <h3>
        Ledger replay · {replay.events.length} events · {c.id}
      </h3>
      <p className="muted mono" style={{ fontSize: 11.5, marginTop: 0 }}>
        {c.amount} · {c.issuer} {c.network ? `(${c.network})` : ""} · {c.failure_code}
        {c.advice_code ? ` · advice ${c.advice_code}` : " · no network advice"} ·{" "}
        {c.cause} via {c.tier || "n/a"} · {c.retries} retries · {c.contacts} contacts
      </p>
      {c.free_text && (
        <p className="muted" style={{ fontSize: 12.5, marginTop: 0 }}>
          <b>free text</b> (what T3 was shown): “{c.free_text}”
        </p>
      )}
      {replay.events.map((e, i) => (
        <div className="ev" key={i}>
          <span className="t">{e.ts}</span>
          <span className="k">{e.kind}</span>
          <span className="d">
            {Object.entries(e.payload).map(([k, v]) => (
              <span key={k}>
                <b>{k}</b>={v}&nbsp;&nbsp;
              </span>
            ))}
            {e.rules.length > 0 && (
              <span className="rules">rules: {e.rules.join(", ")}</span>
            )}
          </span>
        </div>
      ))}
    </div>
  );
}
