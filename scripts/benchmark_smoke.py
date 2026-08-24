from __future__ import annotations

import json

from backend.decision import capacity_analysis, cost_analysis, simulate_plan_risk
from backend.fixtures import get_fixture
from backend.hashing import content_hash
from backend.models import CapacityAnalysisRequest, PlanVersion, RiskSimulationRequest, Skill
from backend.scheduler import baseline_schedule
from backend.verification import verify_schedule


def plan_for(scenario_id: str):
    scenario = get_fixture(scenario_id)
    schedule = baseline_schedule(scenario, 1)
    report = verify_schedule(scenario, schedule)
    assert report.publishable, [item.code for item in report.errors]
    return scenario, PlanVersion(
        id=f"BENCH-{scenario.id}",
        scenario_id=scenario.id,
        number=1,
        action="baseline",
        label="Benchmark baseline",
        data_revision=scenario.revision,
        created_at=schedule.created_at,
        scenario_snapshot=scenario,
        selected=schedule,
        scenario_snapshot_hash=content_hash(scenario),
    )


rows: list[dict[str, object]] = []

for name, fixture_id in (
    ("small", "main"),
    ("medium", "strategy-medium"),
    ("skill-shortage", "skill-shortage"),
    ("emergency", "emergency"),
):
    scenario, plan = plan_for(fixture_id)
    rows.append(
        {
            "name": name,
            "status": "verified",
            "orders": len(scenario.work_orders),
            "technicians": len(scenario.technicians),
            "signature": content_hash(plan.selected.assignments)[:12],
        }
    )

tight = get_fixture("main")
for order in tight.work_orders:
    order.window_end = min(order.window_end, order.window_start + 15)
tight_schedule = baseline_schedule(tight, 1)
assert verify_schedule(tight, tight_schedule).publishable
rows.append({"name": "tight-window", "status": "verified", "unassigned": len(tight_schedule.unassigned)})

infeasible = get_fixture("main")
infeasible.locked_assignments = []
for technician in infeasible.technicians:
    technician.skills = [Skill.electrical]
for order in infeasible.work_orders:
    order.required_skills = [Skill.hvac]
infeasible_schedule = baseline_schedule(infeasible, 1)
infeasible_report = verify_schedule(infeasible, infeasible_schedule)
assert not infeasible_report.publishable
assert "EMPTY_CANDIDATE" in {item.code for item in infeasible_report.errors}
assert len(infeasible_schedule.unassigned) == len(infeasible.work_orders)
rows.append(
    {
        "name": "infeasible",
        "status": "correctly-rejected",
        "reason": "EMPTY_CANDIDATE",
        "unassigned": len(infeasible_schedule.unassigned),
    }
)

scenario, plan = plan_for("main")
cost = cost_analysis(plan)
capacity = capacity_analysis(plan, CapacityAnalysisRequest())
risk = simulate_plan_risk(plan, RiskSimulationRequest(seed=20260824, trials=50))
assert cost.breakdown.total_cost_cents > 0
assert len(capacity.options) == 6
assert risk.late_minutes_p50 <= risk.late_minutes_p90 <= risk.late_minutes_p95

rows.extend(
    [
        {"name": "replan", "status": "covered-by-regression", "test": "complete/start replan suite"},
        {
            "name": "complete-replan-replan",
            "status": "covered-by-regression",
            "test": "test_completed_work_can_be_replanned_multiple_times",
        },
        {
            "name": "started-nonfirst",
            "status": "covered-by-regression",
            "test": "test_started_nonfirst_assignment_becomes_future_sequence_one",
        },
        {
            "name": "active-overrun",
            "status": "covered-by-regression",
            "test": "test_active_service_overrun_is_not_silently_available",
        },
        {
            "name": "multi-depot",
            "status": "partial",
            "note": "per-technician multi-origin supported; depot inventory is not",
        },
        {
            "name": "crew",
            "status": "unsupported",
            "note": "simultaneous multi-technician visits are outside the current domain",
        },
        {"name": "cross-day", "status": "unsupported", "note": "single planning-day contract"},
        {"name": "parts-shortage", "status": "unsupported", "note": "parts inventory is outside the current domain"},
    ]
)

print(
    json.dumps(
        {
            "benchmark_version": "FIELD_SERVICE_BENCHMARK_V1",
            "decision_checks": {
                "cost_total_cents": cost.breakdown.total_cost_cents,
                "capacity_options": len(capacity.options),
                "risk_input_hash": risk.simulation_input_hash,
            },
            "scenarios": rows,
        },
        ensure_ascii=False,
        indent=2,
    )
)
