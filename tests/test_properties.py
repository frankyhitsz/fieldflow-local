from hypothesis import given, settings
from hypothesis import strategies as st

from backend.decision import analyze_plan_cost, capacity_analysis
from backend.fixtures import get_fixture
from backend.hashing import content_hash
from backend.models import AnalysisHorizon, CapacityAnalysisRequest, DecisionCostPolicy, PlanVersion
from backend.scheduler import baseline_schedule


@settings(max_examples=30, deadline=None)
@given(st.lists(st.integers(min_value=0, max_value=45), min_size=24, max_size=24))
def test_greedy_assignments_never_precede_generated_service_readiness(offsets: list[int]):
    scenario = get_fixture("main")
    for order, offset in zip(scenario.work_orders, offsets, strict=True):
        order.reported_at = min(order.window_end, order.window_start + offset)
    schedule = baseline_schedule(scenario, 1)
    orders = {item.id: item for item in scenario.work_orders}
    assert all(
        assignment.start_time
        >= max(orders[assignment.work_order_id].window_start, orders[assignment.work_order_id].reported_at or 0)
        for assignment in schedule.assignments
    )


@settings(max_examples=30, deadline=None)
@given(
    technician_rates=st.lists(st.integers(min_value=1, max_value=10_000), min_size=4, max_size=4),
    travel_rate=st.integers(min_value=0, max_value=2_000),
    overtime_basis_points=st.integers(min_value=0, max_value=30_000),
)
def test_generated_integer_cost_policies_always_reconcile(
    technician_rates: list[int],
    travel_rate: int,
    overtime_basis_points: int,
):
    scenario = get_fixture("main")
    for technician, rate in zip(scenario.technicians, technician_rates, strict=True):
        technician.cost_per_minute_cents = rate
    schedule = baseline_schedule(scenario, 1)
    breakdown = analyze_plan_cost(
        scenario,
        schedule,
        DecisionCostPolicy(
            travel_cost_per_minute_cents=travel_rate,
            overtime_premium_basis_points=overtime_basis_points,
        ),
    )
    assert breakdown.cash_operating_cost_cents == (
        breakdown.labor_cost_cents
        + breakdown.travel_cost_cents
        + breakdown.overtime_cost_cents
        + breakdown.outsourcing_cost_cents
    )
    assert breakdown.service_failure_loss_cents == (breakdown.sla_penalty_cents + breakdown.unserved_revenue_cents)
    assert breakdown.total_economic_impact_cents == (
        breakdown.cash_operating_cost_cents + breakdown.service_failure_loss_cents
    )


@settings(max_examples=20, deadline=None)
@given(
    overtime_limits=st.lists(st.integers(min_value=0, max_value=240), min_size=4, max_size=4),
    option=st.sampled_from(["add_technician", "add_skill", "extend_shift", "allow_overtime", "outsource_unserved"]),
    horizon_days=st.integers(min_value=1, max_value=30),
)
def test_any_capacity_option_marked_feasible_has_no_verification_violations(
    overtime_limits: list[int],
    option: str,
    horizon_days: int,
):
    scenario = get_fixture("main")
    for technician, overtime_limit in zip(scenario.technicians, overtime_limits, strict=True):
        technician.overtime_limit = overtime_limit
    schedule = baseline_schedule(scenario, 1)
    plan = PlanVersion(
        id="PV-property-capacity",
        scenario_id=scenario.id,
        number=1,
        action="baseline",
        label="属性测试",
        data_revision=scenario.revision,
        created_at=schedule.created_at,
        scenario_snapshot=scenario,
        selected=schedule,
        scenario_snapshot_hash=content_hash(scenario),
    )
    request = CapacityAnalysisRequest.model_validate(
        {
            "option_ids": [option],
            "analysis_horizon": AnalysisHorizon(days=horizon_days).model_dump(mode="json"),
        }
    )
    result = capacity_analysis(plan, request).options[0]
    if result.feasible:
        assert result.option_applicable
        assert result.schedule_feasible
        assert result.violations == []
    if result.fixed_cost_cadence.value == "ONE_TIME":
        assert result.horizon_total_impact_cents == (
            result.daily_operating_delta_cents * horizon_days + result.one_time_investment_cents
        )
