// Types mirror the FastAPI response shapes in src/backstop/api/app.py.

export type Lift = {
  treated_n: number;
  treated_rate: number;
  holdout_n: number;
  holdout_rate: number;
  lift_pp: number;
  ci_pp: number;
  significant: boolean;
  incremental_paise: number;
};

export type Summary = {
  batch_id: string;
  params: { cases: number; days: number; holdout: number; seed: number };
  created_at: string;
  config_version: string;
  synthetic: boolean;
  headline: {
    cases: number;
    amount_at_risk: string;
    gross: string;
    gross_paise: number;
    incremental: string;
    incremental_paise: number;
    action_cost: string;
    net: string;
    net_paise: number;
  };
  lift: Lift;
  decomposition: Record<string, Record<string, Lift>>;
  restraint: {
    suppressed: number;
    escalated: number;
    contacts_sent: number;
    contacts_per_recovery: number;
    opt_outs: number;
    retry_ruled_out_by_network: number;
    fee_events_avoided: number;
    denied_by_rule: Record<string, number>;
    amount_withheld_paise: number;
  };
  ledger_events: number;
  treated: number;
};

export type RuleRollup = {
  rule_id: string;
  name: string;
  decisions: number;
  cases_stopped: number;
  contacts_prevented: number;
  amount_withheld_paise: number;
};

export type CaseRow = {
  id: string;
  short: string;
  amount: number;
  amount_fmt: string;
  cause: string;
  issuer: string;
  state: string;
  stopping_rule: string;
  reason: string;
  contacts: number;
  retries: number;
  n_stops: number;
  rules: string[];
};

export type CasePage = {
  total: number;
  offset: number;
  limit: number;
  states: string[];
  rows: CaseRow[];
};

export type LedgerEvent = {
  ts: string;
  kind: string;
  actor: string;
  rules: string[];
  payload: Record<string, string>;
};

export type Replay = {
  case: {
    id: string;
    type: string;
    amount: string;
    issuer: string;
    network: string | null;
    advice_code: string | null;
    instrument: string;
    failure_code: string;
    cause: string;
    state: string;
    arm: string;
    stopping_rule: string | null;
    reason: string | null;
    contacts: number;
    retries: number;
  };
  events: LedgerEvent[];
};

async function json<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) {
    // The API puts a usable sentence in `detail`; surface it rather than a status code.
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body?.detail) detail = String(body.detail);
    } catch {
      /* response had no JSON body */
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

export const api = {
  runBatch: (p: { cases: number; days: number; holdout: number; seed: number }) =>
    json<Summary>(
      `/api/batches?cases=${p.cases}&days=${p.days}&holdout=${p.holdout}&seed=${p.seed}`,
      { method: "POST" },
    ),
  rules: (batchId: string) => json<RuleRollup[]>(`/api/batches/${batchId}/rules`),
  cases: (batchId: string, q: Record<string, string | number>) =>
    json<CasePage>(
      `/api/batches/${batchId}/cases?` +
        new URLSearchParams(
          Object.entries(q)
            .filter(([, v]) => v !== "" && v !== undefined)
            .map(([k, v]) => [k, String(v)]),
        ),
    ),
  replay: (batchId: string, caseId: string) =>
    json<Replay>(`/api/batches/${batchId}/cases/${caseId}`),
};

export const rupees = (paise: number) =>
  `Rs ${(paise / 100).toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
