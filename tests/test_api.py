"""The API is the console's contract. These pin the shapes the frontend reads."""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi", reason="install the [serve] extra")
pytest.importorskip("httpx2", reason="TestClient needs httpx2")

from fastapi.testclient import TestClient  # noqa: E402

from backstop.api.app import app  # noqa: E402
from backstop.reporting.whynot import RULE_NAMES  # noqa: E402


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(scope="module")
def batch(client: TestClient) -> dict:
    r = client.post("/api/batches?cases=1200&days=8&holdout=0.15&seed=5")
    assert r.status_code == 200
    return r.json()


def test_running_a_batch_returns_a_full_summary(batch):
    assert batch["synthetic"] is True
    assert "UNVERIFIED" not in batch["config_version"]
    assert batch["headline"]["cases"] == 1200
    for key in ("amount_at_risk", "gross", "incremental", "action_cost", "net"):
        assert batch["headline"][key].startswith("Rs ")


def test_incremental_is_never_reported_as_gross(batch):
    """The whole point of the measurement layer, asserted at the API boundary."""
    h = batch["headline"]
    assert h["incremental_paise"] < h["gross_paise"]


def test_lift_carries_its_interval_and_verdict(batch):
    lift = batch["lift"]
    assert lift["treated_n"] > lift["holdout_n"] > 0
    assert lift["ci_pp"] > 0
    assert isinstance(lift["significant"], bool)


def test_decomposition_covers_every_dimension(batch):
    assert set(batch["decomposition"]) == {"cause", "amount_band", "issuer"}
    for buckets in batch["decomposition"].values():
        assert buckets
        for lf in buckets.values():
            assert {"lift_pp", "ci_pp", "treated_n", "significant"} <= set(lf)


def test_restraint_reports_what_the_system_declined_to_do(batch):
    r = batch["restraint"]
    assert r["suppressed"] > 0
    assert r["amount_withheld_paise"] > 0
    assert r["retry_ruled_out_by_network"] >= r["fee_events_avoided"] >= 0
    assert set(r["denied_by_rule"]) <= set(RULE_NAMES)


def test_rules_rollup_matches_the_known_rule_set(client, batch):
    rules = client.get(f"/api/batches/{batch['batch_id']}/rules").json()
    assert rules
    for r in rules:
        assert r["rule_id"] in RULE_NAMES
        assert r["name"] == RULE_NAMES[r["rule_id"]]
        assert r["decisions"] > 0


def test_stopped_cases_paginate_and_filter(client, batch):
    bid = batch["batch_id"]
    page = client.get(f"/api/batches/{bid}/cases?limit=10").json()
    assert len(page["rows"]) == 10
    assert page["total"] > 10
    assert page["states"]

    filtered = client.get(f"/api/batches/{bid}/cases?rule=PR-06&limit=5").json()
    assert filtered["total"] <= page["total"]
    for row in filtered["rows"]:
        assert "PR-06" in row["rules"] or row["stopping_rule"] == "PR-06"


def test_case_list_omits_the_heavy_fields(client, batch):
    """Timelines and per-decision detail belong to the replay endpoint, not the list."""
    row = client.get(f"/api/batches/{batch['batch_id']}/cases?limit=1").json()["rows"][0]
    assert "timeline" not in row and "decisions" not in row


def test_replay_is_chronological_and_starts_at_detection(client, batch):
    bid = batch["batch_id"]
    case_id = client.get(f"/api/batches/{bid}/cases?limit=1").json()["rows"][0]["id"]
    rep = client.get(f"/api/batches/{bid}/cases/{case_id}").json()

    assert rep["case"]["id"] == case_id
    stamps = [e["ts"] for e in rep["events"]]
    assert stamps == sorted(stamps)
    assert rep["events"][0]["kind"] == "detected"


def test_replay_exposes_the_network_advice_the_decision_used(client, batch):
    bid = batch["batch_id"]
    rows = client.get(f"/api/batches/{bid}/cases?limit=200").json()["rows"]
    seen = [client.get(f"/api/batches/{bid}/cases/{r['id']}").json()["case"]
            for r in rows[:40]]
    assert all(c["network"] in {"visa", "mastercard", None} for c in seen)
    # At least some cases in any real batch carry an advice code.
    assert any(c["advice_code"] for c in seen)


def test_unknown_ids_are_404_with_a_usable_message(client, batch):
    missing = client.get("/api/batches/does-not-exist")
    assert missing.status_code == 404
    assert "run one first" in missing.json()["detail"]

    bad_case = client.get(f"/api/batches/{batch['batch_id']}/cases/nope")
    assert bad_case.status_code == 404


def test_batch_params_are_validated_not_trusted(client):
    assert client.post("/api/batches?cases=5").status_code == 422
    assert client.post("/api/batches?cases=999999").status_code == 422
    assert client.post("/api/batches?holdout=0.9").status_code == 422


def test_health_and_catalogue(client):
    assert client.get("/api/health").json()["ok"] is True
    assert client.get("/api/rules").json() == RULE_NAMES
