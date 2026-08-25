from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from backend.applicability import coverage_status_from_applicability, reduce_plan_applicability
from backend.decision import analyze_plan_cost, capacity_analysis
from backend.fixtures import get_fixture
from backend.hashing import content_hash
from backend.models import (
    AnalysisHorizon,
    AnalysisIntegrityStatus,
    CapacityAnalysisRequest,
    DecisionCostPolicy,
    FieldImpact,
    LaborCostMode,
    PlanApplicability,
    PlanVersion,
    PublicationVerificationArtifact,
)
from backend.provenance import build_plan_manifest_payload
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
    labor_mode=st.sampled_from([LaborCostMode.occupied_minutes, LaborCostMode.paid_shift]),
)
def test_generated_integer_cost_policies_always_reconcile(
    technician_rates: list[int],
    travel_rate: int,
    overtime_basis_points: int,
    labor_mode: LaborCostMode,
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
            labor_cost_mode=labor_mode,
        ),
    )
    assert breakdown.cash_operating_cost_cents == (
        breakdown.regular_labor_cost_cents
        + breakdown.overtime_base_cost_cents
        + breakdown.overtime_premium_cost_cents
        + breakdown.travel_cost_cents
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
    report: dict[str, object] = {}
    artifact_payload = {
        "policy_version": "FIELD_SERVICE_PUBLICATION_VERIFICATION_V2",
        "candidate_snapshot": {},
        "planning_context_snapshot": None,
        "transaction_verification_report": report,
        "verified_schedule_hash": content_hash(schedule),
    }
    verification = PublicationVerificationArtifact(
        **artifact_payload,
        artifact_hash=content_hash(artifact_payload),
    )
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
        published_schedule_hash=content_hash(schedule),
        publication_verification_policy_version="FIELD_SERVICE_PUBLICATION_VERIFICATION_V2",
        publication_verification_report_hash=content_hash(report),
        publication_verification_artifact=verification,
        publication_manifest_version="FIELD_SERVICE_PUBLICATION_MANIFEST_V2",
        publication_manifest_hash="pending",
        integrity_status=AnalysisIntegrityStatus.verified,
        self_integrity=AnalysisIntegrityStatus.verified,
        effective_integrity=AnalysisIntegrityStatus.verified,
    )
    plan.publication_manifest_hash = content_hash(build_plan_manifest_payload(plan))
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
    if result.feasible and result.fixed_cost_cadence.value == "ONE_TIME":
        assert result.horizon_total_impact_cents == (
            result.daily_operating_delta_cents * horizon_days + result.one_time_investment_cents
        )


def _applicability_property_plan() -> PlanVersion:
    scenario = get_fixture("main")
    schedule = baseline_schedule(scenario, 1)
    report: dict[str, object] = {}
    artifact_payload = {
        "policy_version": "FIELD_SERVICE_PUBLICATION_VERIFICATION_V2",
        "candidate_snapshot": {},
        "planning_context_snapshot": None,
        "transaction_verification_report": report,
        "verified_schedule_hash": content_hash(schedule),
    }
    verification = PublicationVerificationArtifact(
        **artifact_payload,
        artifact_hash=content_hash(artifact_payload),
    )
    plan = PlanVersion(
        id="PV-applicability-state-machine",
        scenario_id=scenario.id,
        number=1,
        action="baseline",
        label="状态机计划",
        data_revision=scenario.revision,
        created_at=schedule.created_at,
        scenario_snapshot=scenario,
        selected=schedule,
        scenario_snapshot_hash=content_hash(scenario),
        published_schedule_hash=content_hash(schedule),
        publication_verification_policy_version="FIELD_SERVICE_PUBLICATION_VERIFICATION_V2",
        publication_verification_report_hash=content_hash(report),
        publication_verification_artifact=verification,
        publication_manifest_version="FIELD_SERVICE_PUBLICATION_MANIFEST_V2",
        publication_manifest_hash="pending",
        integrity_status=AnalysisIntegrityStatus.verified,
        self_integrity=AnalysisIntegrityStatus.verified,
        effective_integrity=AnalysisIntegrityStatus.verified,
    )
    plan.publication_manifest_hash = content_hash(build_plan_manifest_payload(plan))
    return plan


class PlanApplicabilityStateMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.plan = _applicability_property_plan()
        assert self.plan.scenario_snapshot is not None
        self.scenario = self.plan.scenario_snapshot.model_copy(deep=True)
        self.applicability = PlanApplicability()
        self.invalidated: set[str] = set()
        self.added_ids: list[str] = []
        self.next_added_id = 0

    @rule(index=st.integers(min_value=0, max_value=23))
    def invalidate_assignment(self, index: int) -> None:
        assignment = self.plan.selected.assignments[index % len(self.plan.selected.assignments)]
        previous = self.scenario.model_copy(deep=True)
        self.invalidated.add(assignment.work_order_id)
        self.applicability = reduce_plan_applicability(
            self.plan,
            previous,
            self.scenario,
            self.applicability,
            FieldImpact.assignment_feasibility,
            [assignment.work_order_id],
        )

    @rule()
    def add_uncovered_demand(self) -> None:
        previous = self.scenario.model_copy(deep=True)
        order = self.scenario.work_orders[0].model_copy(deep=True)
        order.id = f"WO-STATE-{self.next_added_id}"
        self.next_added_id += 1
        self.added_ids.append(order.id)
        self.scenario.work_orders.append(order)
        self.applicability = reduce_plan_applicability(
            self.plan,
            previous,
            self.scenario,
            self.applicability,
            FieldImpact.new_demand,
        )

    @rule()
    def remove_one_uncovered_demand(self) -> None:
        if not self.added_ids:
            return
        previous = self.scenario.model_copy(deep=True)
        removed = self.added_ids.pop(0)
        self.scenario.work_orders = [item for item in self.scenario.work_orders if item.id != removed]
        self.applicability = reduce_plan_applicability(
            self.plan,
            previous,
            self.scenario,
            self.applicability,
            FieldImpact.removed_unassigned_demand,
        )

    @invariant()
    def invalid_assignments_accumulate_and_coverage_is_derived(self) -> None:
        assert set(self.applicability.invalid_assignment_ids) == self.invalidated
        assert self.applicability.route_executable is (not self.invalidated)
        assert self.applicability.coverage_complete is (not self.added_ids)
        projected = coverage_status_from_applicability(self.applicability)
        assert projected.value in {
            "CURRENT_AND_COMPLETE",
            "PARTIAL_NEW_DEMAND",
            "STALE_DATA_CHANGED",
        }


TestPlanApplicabilityStateMachine = PlanApplicabilityStateMachine.TestCase
