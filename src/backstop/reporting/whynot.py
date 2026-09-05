"""The "Why not" view — every case the system deliberately did not work, and the
exact rule that stopped it, with a full replay from the ledger.

This is the screen that answers the track's "stopping rules and audit trails"
criterion. It is close to free to build because the policy engine records every
evaluation whether or not it allowed anything: nothing here is instrumentation added
after the fact, it is a read over the ledger.

Emits one self-contained HTML file — no server, no CDN, no build step — so it opens
from a filesystem path during the pitch video.
"""

from __future__ import annotations

import html
import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Iterable

from ..domain.case import Case
from ..domain.events import CaseEvent, EventKind
from ..domain.types import Arm, CaseState, Paise, rupees
from ..ledger.store import LedgerStore

# Human-readable rule names, so the view does not make a reviewer look up an ID.
RULE_NAMES: dict[str, str] = {
    "PR-01": "Consent / DND registry",
    "PR-02": "Quiet hours",
    "PR-03": "Contact frequency cap",
    "PR-04": "Retry ceiling",
    "PR-05": "Mandate pre-debit notice",
    "PR-06": "Economic floor",
    "PR-07": "Hard stop",
    "PR-08": "Promise-to-pay hold",
}

STOPPING_DISPOSITIONS = frozenset({"deny", "defer", "suppress", "hard_stop"})
CONTACT_PREFIXES = ("send_message", "voice_call", "request_reauth_link",
                    "request_promise_to_pay", "offer_installment")


@dataclass(slots=True)
class RuleRollup:
    rule_id: str
    name: str
    cases_stopped: int = 0
    decisions: int = 0
    contacts_prevented: int = 0
    amount_withheld: Paise = 0
    dispositions: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class WhyNotData:
    rules: list[RuleRollup]
    rows: list[dict[str, Any]]
    totals: dict[str, Any]
    rows_shown: int
    rows_total: int
    timelines_shown: int


def _is_contact(action: str) -> bool:
    return action.startswith(CONTACT_PREFIXES)


def extract(
    cases: Iterable[Case],
    ledger: LedgerStore,
    *,
    max_rows: int = 2_000,
    max_timelines: int = 250,
) -> WhyNotData:
    """Read the ledger for every case the policy engine stopped at least once.

    A case appears here if it ended suppressed, or if any action proposed for it was
    denied, deferred or hard-stopped — including cases that recovered anyway. Showing
    only the suppressed ones would hide the restraint applied to successful cases.
    """
    by_case = {c.case_id: c for c in cases}
    rollups: dict[str, RuleRollup] = {
        rid: RuleRollup(rid, name) for rid, name in RULE_NAMES.items()
    }
    decisions_by_case: dict[str, list[dict[str, Any]]] = defaultdict(list)
    contacts_prevented_total = 0
    decisions_logged = 0

    for event in ledger.all_events():
        if event.kind is not EventKind.POLICY_DECIDED:
            continue
        decisions_logged += 1
        disposition = event.payload.get("disposition", "")
        if disposition not in STOPPING_DISPOSITIONS:
            continue

        action = event.payload.get("action", "")
        fired = [r for r in event.rule_ids if r in rollups]
        prevented = _is_contact(action) and disposition in ("deny", "hard_stop")
        if prevented:
            contacts_prevented_total += 1

        for rule_id in fired:
            roll = rollups[rule_id]
            roll.decisions += 1
            roll.dispositions[disposition] = roll.dispositions.get(disposition, 0) + 1
            if prevented:
                roll.contacts_prevented += 1

        decisions_by_case[event.case_id].append(
            {
                "ts": event.occurred_at.isoformat(sep=" ", timespec="minutes"),
                "action": action,
                "disposition": disposition,
                "reason": event.payload.get("reason", ""),
                "rules": list(event.rule_ids),
            }
        )

    # Attribute a stopped case to the rule that actually ended it.
    for case in by_case.values():
        if case.state is CaseState.SUPPRESSED and case.stopping_rule in rollups:
            roll = rollups[case.stopping_rule]
            roll.cases_stopped += 1
            roll.amount_withheld += case.amount_paise

    candidates = [
        by_case[cid] for cid in decisions_by_case if cid in by_case
    ] + [
        c for c in by_case.values()
        if c.state is CaseState.SUPPRESSED and c.case_id not in decisions_by_case
    ]
    candidates = list({c.case_id: c for c in candidates}.values())
    candidates.sort(key=lambda c: c.amount_paise, reverse=True)

    rows_total = len(candidates)
    shown = candidates[:max_rows]

    rows: list[dict[str, Any]] = []
    for i, case in enumerate(shown):
        decisions = decisions_by_case.get(case.case_id, [])
        rows.append(
            {
                "id": case.case_id,
                "short": case.case_id[:8],
                "amount": case.amount_paise,
                "amount_fmt": rupees(case.amount_paise),
                "cause": case.cause.value,
                "issuer": case.issuer,
                "type": case.case_type.value,
                "state": case.state.value,
                "arm": case.arm.value,
                "stopping_rule": case.stopping_rule or "",
                "reason": case.terminal_reason or "",
                "contacts": case.contacts_total,
                "retries": case.retries_used,
                "n_stops": len(decisions),
                "rules": sorted({r for d in decisions for r in d["rules"] if r in rollups}),
                "decisions": decisions,
                "timeline": (
                    _timeline(ledger.events_for(case.case_id)) if i < max_timelines else None
                ),
            }
        )

    suppressed = [c for c in by_case.values() if c.state is CaseState.SUPPRESSED]
    treated = [c for c in by_case.values() if c.arm is Arm.TREATED]
    totals = {
        "cases": len(by_case),
        "treated": len(treated),
        "suppressed": len(suppressed),
        "amount_withheld": sum(c.amount_paise for c in suppressed),
        "amount_withheld_fmt": rupees(sum(c.amount_paise for c in suppressed)),
        "contacts_prevented": contacts_prevented_total,
        "decisions_logged": decisions_logged,
        "generated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
    }

    return WhyNotData(
        rules=[r for r in rollups.values() if r.decisions or r.cases_stopped],
        rows=rows,
        totals=totals,
        rows_shown=len(rows),
        rows_total=rows_total,
        timelines_shown=min(max_timelines, len(rows)),
    )


def _timeline(events: list[CaseEvent]) -> list[dict[str, Any]]:
    # A replay is chronological. Sorting is stable, so events sharing a timestamp keep
    # the order the ledger recorded them in.
    events = sorted(events, key=lambda e: e.occurred_at)
    return [
        {
            "ts": e.occurred_at.isoformat(sep=" ", timespec="minutes"),
            "kind": e.kind.value,
            "actor": e.actor,
            "rules": [r for r in e.rule_ids],
            "payload": {k: str(v) for k, v in e.payload.items()},
        }
        for e in events
    ]


def write_html(data: WhyNotData, path: str, *, config_version: str) -> str:
    payload = json.dumps(
        {
            "rules": [asdict(r) for r in data.rules],
            "rows": data.rows,
            "totals": data.totals,
            "rows_shown": data.rows_shown,
            "rows_total": data.rows_total,
            "timelines_shown": data.timelines_shown,
            "config_version": config_version,
        },
        separators=(",", ":"),
        default=str,
    ).replace("</", "<\\/")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(_TEMPLATE.replace("__DATA__", payload))
    return path


_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Backstop - Why not</title>
<style>
:root{
  --ground:#F3F6F4; --surface:#FFFFFF; --surface-2:#E8EDEA;
  --ink:#121A18; --ink-mid:#3F4C49; --ink-soft:#6B7873;
  --rule:#D6DEDA; --rule-strong:#B4C0BB;
  --accent:#1B6864; --accent-ink:#12514E; --accent-wash:#DCEAE7;
  --deny:#8E322D; --deny-wash:#F5E4E2;
  --defer:#9E4E19; --defer-wash:#F5E9DC;
  --stop:#6B2320; --stop-wash:#EFDCDA;
}
@media (prefers-color-scheme:dark){:root{
  --ground:#0D1312; --surface:#141C1A; --surface-2:#1B2523;
  --ink:#E3EAE7; --ink-mid:#B3C0BC; --ink-soft:#8A9994;
  --rule:#26312E; --rule-strong:#3A4844;
  --accent:#54B7AE; --accent-ink:#7ACFC6; --accent-wash:#16302E;
  --deny:#D4756D; --deny-wash:#2C1B1A;
  --defer:#D0854A; --defer-wash:#2C2118;
  --stop:#E08A82; --stop-wash:#331D1B;
}}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);
  font:14px/1.55 "IBM Plex Sans",-apple-system,"Segoe UI",system-ui,sans-serif}
.mono{font-family:"IBM Plex Mono",ui-monospace,"Cascadia Mono",Consolas,monospace}
header{border-bottom:1px solid var(--rule-strong);padding:22px 28px 18px;background:var(--surface)}
.eyebrow{font-family:"IBM Plex Mono",monospace;font-size:11px;letter-spacing:.13em;
  text-transform:uppercase;color:var(--accent-ink);margin:0 0 8px}
h1{font-size:1.5rem;margin:0 0 6px;letter-spacing:-.01em;font-weight:600}
.sub{color:var(--ink-soft);margin:0;max-width:78ch}
.warn{display:inline-block;margin-top:12px;font-family:"IBM Plex Mono",monospace;
  font-size:11px;letter-spacing:.05em;color:var(--defer);
  border:1px solid var(--defer);border-radius:2px;padding:2px 8px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));
  gap:1px;background:var(--rule);border-bottom:1px solid var(--rule-strong)}
.kpi{background:var(--surface);padding:16px 20px}
.kpi dt{font-family:"IBM Plex Mono",monospace;font-size:10px;letter-spacing:.1em;
  text-transform:uppercase;color:var(--ink-soft);margin:0 0 6px}
.kpi dd{margin:0;font-size:1.45rem;font-weight:600;letter-spacing:-.02em;
  font-variant-numeric:tabular-nums}
.kpi dd.accent{color:var(--accent-ink)}
main{display:grid;grid-template-columns:290px minmax(0,1fr);gap:0;align-items:start}
@media (max-width:900px){main{grid-template-columns:1fr}}
aside{border-right:1px solid var(--rule);padding:20px;position:sticky;top:0}
@media (max-width:900px){aside{position:static;border-right:0;border-bottom:1px solid var(--rule)}}
aside h2,section h2{font-family:"IBM Plex Mono",monospace;font-size:10.5px;
  letter-spacing:.12em;text-transform:uppercase;color:var(--ink-soft);
  margin:0 0 12px;font-weight:500}
.rulebtn{display:block;width:100%;text-align:left;background:var(--surface);
  border:1px solid var(--rule);border-radius:3px;padding:10px 12px;margin-bottom:7px;
  cursor:pointer;color:inherit;font:inherit}
.rulebtn:hover{border-color:var(--rule-strong)}
.rulebtn[aria-pressed="true"]{border-color:var(--accent);background:var(--accent-wash)}
.rulebtn:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.rulebtn .rid{font-family:"IBM Plex Mono",monospace;font-size:11.5px;color:var(--accent-ink)}
.rulebtn .rn{display:block;font-size:12.5px;color:var(--ink-mid);margin-top:1px}
.rulebtn .rstats{display:block;font-family:"IBM Plex Mono",monospace;font-size:10.5px;
  color:var(--ink-soft);margin-top:5px;font-variant-numeric:tabular-nums}
section{padding:20px 24px 60px;min-width:0}
.controls{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:14px}
input[type=search],select{font:inherit;padding:7px 10px;border:1px solid var(--rule);
  border-radius:3px;background:var(--surface);color:inherit}
input[type=search]{flex:1;min-width:200px}
input:focus-visible,select:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
button.clear{background:none;border:1px solid var(--rule);border-radius:3px;
  padding:7px 12px;cursor:pointer;color:var(--ink-mid);font:inherit}
.count{font-family:"IBM Plex Mono",monospace;font-size:11.5px;color:var(--ink-soft)}
.tablewrap{overflow-x:auto;border:1px solid var(--rule);border-radius:4px;background:var(--surface)}
table{border-collapse:collapse;width:100%;font-size:13px;min-width:820px}
th,td{text-align:left;padding:9px 13px;border-bottom:1px solid var(--rule);vertical-align:top}
th{font-family:"IBM Plex Mono",monospace;font-size:10px;letter-spacing:.09em;
  text-transform:uppercase;color:var(--ink-soft);font-weight:500;
  background:var(--surface-2);position:sticky;top:0;white-space:nowrap}
td.num{text-align:right;font-variant-numeric:tabular-nums;
  font-family:"IBM Plex Mono",monospace;font-size:12px;white-space:nowrap}
tr.row{cursor:pointer}
tr.row:hover td{background:var(--surface-2)}
tr.row[aria-expanded="true"] td{background:var(--accent-wash)}
.pill{font-family:"IBM Plex Mono",monospace;font-size:10px;letter-spacing:.05em;
  text-transform:uppercase;border:1px solid currentColor;border-radius:2px;
  padding:1px 5px;white-space:nowrap;display:inline-block}
.p-deny{color:var(--deny)} .p-defer{color:var(--defer)}
.p-suppress{color:var(--accent-ink)} .p-hard_stop{color:var(--stop)}
.rules-cell{display:flex;flex-wrap:wrap;gap:4px}
.replay{background:var(--surface-2)}
.replay td{padding:0}
.replay-inner{padding:16px 18px;border-left:3px solid var(--accent)}
.replay h3{font-family:"IBM Plex Mono",monospace;font-size:10.5px;letter-spacing:.11em;
  text-transform:uppercase;color:var(--ink-soft);margin:0 0 10px;font-weight:500}
.ev{display:grid;grid-template-columns:132px 118px minmax(0,1fr);gap:4px 14px;
  padding:7px 0;border-bottom:1px solid var(--rule);
  font-family:"IBM Plex Mono",monospace;font-size:11.5px}
.ev:last-child{border-bottom:0}
.ev .k{color:var(--accent-ink)}
.ev .t{color:var(--ink-soft)}
.ev .d{color:var(--ink-mid);word-break:break-word}
.ev .d b{color:var(--ink);font-weight:500}
.norep{color:var(--ink-soft);font-family:"IBM Plex Mono",monospace;font-size:11.5px;
  padding:8px 0}
.empty{padding:36px;text-align:center;color:var(--ink-soft)}
footer{padding:18px 28px 40px;color:var(--ink-soft);
  font-family:"IBM Plex Mono",monospace;font-size:11px;line-height:1.7}
</style>
</head>
<body>
<header>
  <p class="eyebrow">Backstop &middot; Track 03 &middot; stopping rules &amp; audit trail</p>
  <h1>Why not</h1>
  <p class="sub">Every case the policy engine declined to work, and the exact rule that
  stopped it. Select a row to replay that case from the ledger, event by event.</p>
  <span class="warn" id="warn"></span>
</header>

<dl class="kpis" id="kpis"></dl>

<main>
  <aside>
    <h2>Stopped by rule</h2>
    <div id="rules"></div>
  </aside>
  <section>
    <h2>Stopped cases</h2>
    <div class="controls">
      <input type="search" id="q" placeholder="Search case id, cause, issuer, reason...">
      <select id="disp">
        <option value="">All dispositions</option>
        <option value="suppress">suppress</option>
        <option value="deny">deny</option>
        <option value="defer">defer</option>
        <option value="hard_stop">hard stop</option>
      </select>
      <select id="state">
        <option value="">All outcomes</option>
      </select>
      <button class="clear" id="clear">Clear</button>
      <span class="count" id="count"></span>
    </div>
    <div class="tablewrap">
      <table>
        <thead><tr>
          <th>Case</th><th>Amount</th><th>Cause</th><th>Issuer</th>
          <th>Outcome</th><th>Stopped by</th><th>Rules fired</th>
          <th class="num">Stops</th><th class="num">Contacts</th>
        </tr></thead>
        <tbody id="tbody"></tbody>
      </table>
    </div>
    <div class="empty" id="empty" hidden>No cases match these filters.</div>
  </section>
</main>

<footer id="foot"></footer>

<script>
const DATA = __DATA__;
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"]/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

let activeRule = null;

$('warn').textContent =
  'Synthetic data \\u2014 hand-calibrated, not measured production rates \\u00b7 policy '
  + DATA.config_version;

const T = DATA.totals;
$('kpis').innerHTML = [
  ['Cases suppressed', T.suppressed.toLocaleString(), true],
  ['Money deliberately not chased', T.amount_withheld_fmt, true],
  ['Contacts prevented', T.contacts_prevented.toLocaleString(), false],
  ['Policy decisions logged', T.decisions_logged.toLocaleString(), false],
  ['Treated cases in batch', T.treated.toLocaleString(), false],
].map(([k, v, accent]) =>
  `<div class="kpi"><dt>${esc(k)}</dt><dd class="${accent ? 'accent' : ''}">${esc(v)}</dd></div>`
).join('');

$('rules').innerHTML = DATA.rules.map(r => `
  <button class="rulebtn" data-rule="${esc(r.rule_id)}" aria-pressed="false">
    <span class="rid">${esc(r.rule_id)}</span>
    <span class="rn">${esc(r.name)}</span>
    <span class="rstats">${r.decisions.toLocaleString()} decisions &middot;
      ${r.cases_stopped.toLocaleString()} cases ended &middot;
      ${r.contacts_prevented.toLocaleString()} contacts stopped</span>
  </button>`).join('');

const states = [...new Set(DATA.rows.map(r => r.state))].sort();
$('state').innerHTML += states.map(s =>
  `<option value="${esc(s)}">${esc(s)}</option>`).join('');

function matches(row) {
  if (activeRule && !row.rules.includes(activeRule)
      && row.stopping_rule !== activeRule) return false;
  const disp = $('disp').value;
  if (disp && !row.decisions.some(d => d.disposition === disp)
      && !(disp === 'suppress' && row.state === 'suppressed')) return false;
  const st = $('state').value;
  if (st && row.state !== st) return false;
  const q = $('q').value.trim().toLowerCase();
  if (q) {
    const hay = [row.id, row.cause, row.issuer, row.reason, row.stopping_rule,
                 row.type, row.rules.join(' ')].join(' ').toLowerCase();
    if (!hay.includes(q)) return false;
  }
  return true;
}

function pill(d) { return `<span class="pill p-${esc(d)}">${esc(d.replace('_', ' '))}</span>`; }

function replayHTML(row) {
  if (!row.timeline) {
    return `<div class="replay-inner"><h3>Ledger replay</h3><p class="norep">
      Replay for this case was not included in this export (largest
      ${DATA.timelines_shown.toLocaleString()} cases by amount carry timelines).
      It is present in the ledger \\u2014 regenerate with a higher --html-timelines.
    </p></div>`;
  }
  const evs = row.timeline.map(e => {
    const bits = Object.entries(e.payload)
      .map(([k, v]) => `<b>${esc(k)}</b>=${esc(v)}`).join(' &nbsp; ');
    const rules = e.rules.length
      ? `<br>rules: ${e.rules.map(esc).join(', ')}` : '';
    return `<div class="ev"><span class="t">${esc(e.ts)}</span>
      <span class="k">${esc(e.kind)}</span>
      <span class="d">${bits}${rules}</span></div>`;
  }).join('');
  return `<div class="replay-inner">
    <h3>Ledger replay &middot; ${row.timeline.length} events &middot; ${esc(row.id)}</h3>
    ${evs}</div>`;
}

function render() {
  const rows = DATA.rows.filter(matches);
  $('count').textContent =
    `${rows.length.toLocaleString()} of ${DATA.rows_total.toLocaleString()} stopped cases`
    + (DATA.rows_total > DATA.rows_shown
        ? ` (export capped at ${DATA.rows_shown.toLocaleString()}, largest by amount)` : '');
  $('empty').hidden = rows.length > 0;
  $('tbody').innerHTML = rows.map((row, i) => `
    <tr class="row" data-i="${i}" aria-expanded="false">
      <td class="mono">${esc(row.short)}</td>
      <td class="num">${esc(row.amount_fmt)}</td>
      <td>${esc(row.cause)}</td>
      <td>${esc(row.issuer)}</td>
      <td>${esc(row.state)}</td>
      <td>${row.stopping_rule ? `<span class="mono">${esc(row.stopping_rule)}</span>
            <div style="color:var(--ink-soft);font-size:11.5px">${esc(row.reason)}</div>` : '&mdash;'}</td>
      <td><div class="rules-cell">${row.rules.map(r =>
            `<span class="pill">${esc(r)}</span>`).join('')}</div></td>
      <td class="num">${row.n_stops}</td>
      <td class="num">${row.contacts}</td>
    </tr>
    <tr class="replay" hidden><td colspan="9"></td></tr>`).join('');

  $('tbody').querySelectorAll('tr.row').forEach(tr => {
    tr.addEventListener('click', () => {
      const drawer = tr.nextElementSibling;
      const open = tr.getAttribute('aria-expanded') === 'true';
      tr.setAttribute('aria-expanded', String(!open));
      drawer.hidden = open;
      if (!open && !drawer.dataset.filled) {
        drawer.firstElementChild.innerHTML = replayHTML(rows[Number(tr.dataset.i)]);
        drawer.dataset.filled = '1';
      }
    });
  });
}

$('rules').addEventListener('click', e => {
  const btn = e.target.closest('.rulebtn');
  if (!btn) return;
  const rule = btn.dataset.rule;
  activeRule = activeRule === rule ? null : rule;
  $('rules').querySelectorAll('.rulebtn').forEach(b =>
    b.setAttribute('aria-pressed', String(b.dataset.rule === activeRule)));
  render();
});
['q', 'disp', 'state'].forEach(id => $(id).addEventListener('input', render));
$('clear').addEventListener('click', () => {
  activeRule = null; $('q').value = ''; $('disp').value = ''; $('state').value = '';
  $('rules').querySelectorAll('.rulebtn').forEach(b => b.setAttribute('aria-pressed', 'false'));
  render();
});

$('foot').innerHTML =
  `Generated ${esc(T.generated_at)} &middot; ${T.cases.toLocaleString()} cases in batch &middot; `
  + `${T.decisions_logged.toLocaleString()} policy decisions read from the ledger<br>`
  + `A case appears here if it ended suppressed, or if any action proposed for it was `
  + `denied, deferred or hard-stopped \\u2014 including cases that recovered anyway.`;

render();
</script>
</body>
</html>
"""
