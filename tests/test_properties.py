from hypothesis import given, settings
from hypothesis import strategies as st

from backend.decision import analyze_plan_cost
from backend.fixtures import get_fixture
from backend.models import DecisionCostPolicy
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
