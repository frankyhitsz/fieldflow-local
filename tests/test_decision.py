import importlib
import json
import sqlite3
from contextlib import closing

from fastapi.testclient import TestClient

from backend.decision import analyze_plan_cost, capacity_analysis, simulate_plan_risk
from backend.fixtures import get_fixture
from backend.hashing import content_hash
from backend.models import CapacityAnalysisRequest, PlanVersion, RiskSimulationRequest, Technician, TechnicianUpdate
from backend.scheduler import baseline_schedule
from backend.storage import Store


def _plan(fixture_id: str = "main") -> PlanVersion:
    scenario = get_fixture(fixture_id)
    schedule = baseline_schedule(scenario, 1)
    return PlanVersion(
        id=f"PV-{scenario.id}-decision",
        scenario_id=scenario.id,
        number=1,
        action="baseline",
        label="决策测算",
        data_revision=scenario.revision,
        created_at=schedule.created_at,
        scenario_snapshot=scenario,
        selected=schedule,
        scenario_snapshot_hash=content_hash(scenario),
    )


def test_cost_model_uses_integer_cents_and_reconciles_total():
    plan = _plan()
    scenario = plan.scenario_snapshot
    assert scenario is not None
    breakdown = analyze_plan_cost(scenario, plan.selected)
    components = (
        breakdown.labor_cost_cents,
        breakdown.travel_cost_cents,
        breakdown.overtime_cost_cents,
        breakdown.sla_penalty_cents,
        breakdown.unserved_revenue_cents,
        breakdown.outsourcing_cost_cents,
    )
    assert all(isinstance(item, int) and item >= 0 for item in components)
    assert breakdown.total_cost_cents == sum(components)
    expected_labor = sum(
        kpi.occupied_minutes
        * next(item.cost_per_minute_cents for item in scenario.technicians if item.id == kpi.technician_id)
        for kpi in plan.selected.kpis.technician
    )
    assert breakdown.labor_cost_cents == expected_labor


def test_capacity_analysis_compares_all_six_options_on_one_method():
    plan = _plan("strategy-medium")
    result = capacity_analysis(plan, CapacityAnalysisRequest())
    assert result.evaluation_method == "DETERMINISTIC_GREEDY_WHAT_IF_V1"
    assert {item.option_id for item in result.options} == {
        "add_technician",
        "add_skill",
        "extend_shift",
        "allow_overtime",
        "outsource_unserved",
        "add_service_depot",
    }
    assert len({item.schedule_signature for item in result.options}) >= 2
    assert all(isinstance(item.marginal_cost_cents, int) for item in result.options)


def test_risk_simulation_is_seeded_and_percentiles_are_monotonic():
    plan = _plan()
    request = RiskSimulationRequest(seed=314159, trials=100)
    first = simulate_plan_risk(plan, request)
    second = simulate_plan_risk(plan, request)
    assert first.model_dump() == second.model_dump()
    assert first.late_minutes_p50 <= first.late_minutes_p90 <= first.late_minutes_p95
    assert 0 <= first.expected_sla_on_time_rate <= 1
    assert 0 <= first.plan_failure_probability <= 1


def test_decision_endpoints_use_frozen_plan_without_consuming_versions(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "decision.db"))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        baseline = client.post("/api/scenarios/main/baseline")
        assert baseline.status_code == 200
        versions = client.get("/api/scenarios/main/plan-versions").json()
        assert [item["number"] for item in versions] == [1]
        version_id = versions[0]["id"]

        cost = client.get(f"/api/scenarios/main/plan-versions/{version_id}/cost-analysis")
        capacity = client.post(f"/api/scenarios/main/plan-versions/{version_id}/capacity-analysis", json={})
        risk = client.post(
            f"/api/scenarios/main/plan-versions/{version_id}/risk-simulation",
            json={"seed": 7, "trials": 50},
        )
        assert cost.status_code == capacity.status_code == risk.status_code == 200
        assert len(capacity.json()["options"]) == 6
        assert risk.json()["seed"] == 7
        assert [item["number"] for item in client.get("/api/scenarios/main/plan-versions").json()] == [1]


def test_v10_migration_preserves_legacy_technician_cost_value(tmp_path):
    database = tmp_path / "money-migration.db"
    Store(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        payload = json.loads(connection.execute("SELECT payload FROM scenarios WHERE id='main'").fetchone()[0])
        payload["technicians"][0].pop("cost_per_minute_cents")
        payload["technicians"][0]["cost_per_minute"] = 1.25
        connection.execute(
            "UPDATE scenarios SET payload=? WHERE id='main'",
            (json.dumps(payload, ensure_ascii=False),),
        )
        connection.execute("PRAGMA user_version=9")

    migrated = Store(database).get_scenario("main")
    assert migrated is not None
    assert migrated.technicians[0].cost_per_minute_cents == 125
    with closing(sqlite3.connect(database)) as connection, connection:
        stored = json.loads(connection.execute("SELECT payload FROM scenarios WHERE id='main'").fetchone()[0])
        assert stored["technicians"][0]["cost_per_minute_cents"] == 125
        assert "cost_per_minute" not in stored["technicians"][0]
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 10


def test_legacy_technician_cost_input_remains_compatible():
    technician = Technician.model_validate(
        {
            "id": "TECH-LEGACY",
            "name": "旧客户端",
            "skills": ["electrical"],
            "shift_start": 480,
            "shift_end": 1020,
            "start_location": {"x": 50, "y": 50},
            "cost_per_minute": 1.75,
        }
    )
    update = TechnicianUpdate.model_validate({"cost_per_minute": 2.25})
    assert technician.cost_per_minute_cents == 175
    assert update.cost_per_minute_cents == 225
