from __future__ import annotations

from dataclasses import dataclass

from .models import (
    FieldImpact,
    PlanApplicability,
    PlanCoverageStatus,
    PlanVersion,
    ScheduleScenario,
    WorkOrderStatus,
)


@dataclass(frozen=True)
class PlanDependencyIndex:
    assigned_work_order_ids: frozenset[str]
    unassigned_work_order_ids: frozenset[str]
    route_technician_ids: frozenset[str]
    locked_work_order_ids: frozenset[str]

    @property
    def disposition_work_order_ids(self) -> frozenset[str]:
        return self.assigned_work_order_ids | self.unassigned_work_order_ids

    @classmethod
    def from_plan(cls, plan: PlanVersion) -> PlanDependencyIndex:
        return cls(
            assigned_work_order_ids=frozenset(item.work_order_id for item in plan.selected.assignments),
            unassigned_work_order_ids=frozenset(item.work_order_id for item in plan.selected.unassigned),
            route_technician_ids=frozenset(item.technician_id for item in plan.selected.assignments),
            locked_work_order_ids=frozenset(item.work_order_id for item in plan.scenario_snapshot.locked_assignments)
            if plan.scenario_snapshot
            else frozenset(),
        )


def coverage_status_from_applicability(applicability: PlanApplicability) -> PlanCoverageStatus:
    if not applicability.coverage_complete:
        return PlanCoverageStatus.partial_new_demand
    if not all(
        (
            applicability.route_executable,
            applicability.planning_current,
            applicability.metrics_current,
            applicability.commercial_current,
        )
    ):
        return PlanCoverageStatus.stale_data_changed
    return PlanCoverageStatus.current_and_complete


def applicability_from_legacy_status(status: PlanCoverageStatus) -> PlanApplicability:
    if status is PlanCoverageStatus.partial_new_demand:
        return PlanApplicability(
            route_executable=True,
            coverage_complete=False,
            planning_current=False,
            metrics_current=False,
        )
    if status is PlanCoverageStatus.stale_data_changed:
        # Old rows cannot prove route feasibility. Fail closed for execution.
        return PlanApplicability(
            route_executable=False,
            coverage_complete=True,
            planning_current=False,
            metrics_current=False,
        )
    return PlanApplicability()


def reduce_plan_applicability(
    plan: PlanVersion,
    previous_scenario: ScheduleScenario,
    updated_scenario: ScheduleScenario,
    current: PlanApplicability,
    impact: FieldImpact,
    invalid_assignment_ids: list[str] | None = None,
) -> PlanApplicability:
    """Derive the post-command projection from the exact active Plan in the transaction."""
    del previous_scenario  # Kept in the contract for future field-level change evidence.
    reduced = current.model_copy(deep=True)
    dependencies = PlanDependencyIndex.from_plan(plan)
    current_demand_ids = {
        order.id for order in updated_scenario.work_orders if order.status is not WorkOrderStatus.completed
    }
    reduced.coverage_complete = current_demand_ids.issubset(dependencies.disposition_work_order_ids)

    if impact is FieldImpact.metadata_only:
        return reduced
    if impact is FieldImpact.commercial_only:
        reduced.commercial_current = False
        reduced.metrics_current = False
    elif impact in {FieldImpact.planning_objective, FieldImpact.planning_constraint}:
        reduced.planning_current = False
        reduced.metrics_current = False
    elif impact is FieldImpact.new_demand:
        reduced.planning_current = False
        reduced.metrics_current = False
    elif impact is FieldImpact.capacity_added:
        # Added capacity does not change the published route or any metric on it.
        # It is an optimization opportunity, not evidence that the current plan is stale.
        reduced.reoptimization_opportunity = True
    elif impact is FieldImpact.removed_unassigned_demand:
        reduced.planning_current = False
        reduced.metrics_current = False
    elif impact in {FieldImpact.assignment_feasibility, FieldImpact.execution}:
        reduced.planning_current = False
        reduced.metrics_current = False
        referenced = dependencies.assigned_work_order_ids.intersection(invalid_assignment_ids or [])
        reduced.invalid_assignment_ids = sorted(set(reduced.invalid_assignment_ids) | referenced)
        if reduced.invalid_assignment_ids:
            reduced.route_executable = False
    return reduced
