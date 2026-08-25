from __future__ import annotations

from .hashing import content_hash
from .models import ScheduleAssignment, ScheduleScenario, Technician, WorkOrder
from .travel import TravelTimeProvider


def work_order_planning_payload(order: WorkOrder) -> dict:
    """All fields that can change optimization or assignment feasibility."""
    return {
        "id": order.id,
        "required_skills": [skill.value for skill in order.required_skills],
        "location": order.location.model_dump(mode="json"),
        "service_duration": order.service_duration,
        "window_start": order.window_start,
        "window_end": order.window_end,
        "sla_deadline": order.sla_deadline,
        "priority": order.priority.value,
        "drop_penalty": order.drop_penalty,
        "vip": order.vip,
        "is_emergency": order.is_emergency,
        "reported_at": order.reported_at,
    }


def work_order_assignment_feasibility_payload(order: WorkOrder) -> dict:
    """Only facts that can make the published technician/visit infeasible."""
    return {
        "id": order.id,
        "required_skills": [skill.value for skill in order.required_skills],
        "location": order.location.model_dump(mode="json"),
        "service_duration": order.service_duration,
        "window_start": order.window_start,
        "window_end": order.window_end,
        "reported_at": order.reported_at,
    }


def work_order_objective_payload(order: WorkOrder) -> dict:
    return {
        "id": order.id,
        "sla_deadline": order.sla_deadline,
        "priority": order.priority.value,
        "drop_penalty": order.drop_penalty,
        "vip": order.vip,
        "is_emergency": order.is_emergency,
    }


def technician_planning_payload(technician: Technician) -> dict:
    return {
        "id": technician.id,
        "skills": [skill.value for skill in technician.skills],
        "shift_start": technician.shift_start,
        "shift_end": technician.shift_end,
        "start_location": technician.start_location.model_dump(mode="json"),
        "overtime_limit": technician.overtime_limit,
    }


def scenario_assignment_feasibility_payload(
    scenario: ScheduleScenario,
    provider: TravelTimeProvider,
) -> dict:
    """Facts that can invalidate a published route, excluding labels, prices and objective weights."""
    return {
        "planning_date": scenario.planning_date,
        "work_orders": [
            {
                **work_order_assignment_feasibility_payload(order),
                "status": order.status.value,
            }
            for order in sorted(scenario.work_orders, key=lambda item: item.id)
        ],
        "technicians": [
            technician_planning_payload(technician)
            for technician in sorted(scenario.technicians, key=lambda item: item.id)
        ],
        "locked_assignments": [
            item.model_dump(mode="json")
            for item in sorted(scenario.locked_assignments, key=lambda item: (item.work_order_id, item.technician_id))
        ],
        "travel_model_fingerprint": provider.fingerprint,
    }


def assignment_source_fingerprint(assignment: ScheduleAssignment) -> str:
    """Stable identity for the promised visit, independent of future route numbering."""
    return content_hash(
        {
            "work_order_id": assignment.work_order_id,
            "technician_id": assignment.technician_id,
            "planned_start": assignment.start_time,
            "planned_finish": assignment.finish_time,
        }
    )


def assignment_planning_fingerprint(
    scenario: ScheduleScenario,
    assignment: ScheduleAssignment,
    provider: TravelTimeProvider,
) -> str:
    orders = {item.id: item for item in scenario.work_orders}
    technicians = {item.id: item for item in scenario.technicians}
    order = orders.get(assignment.work_order_id)
    technician = technicians.get(assignment.technician_id)
    if order is None or technician is None:
        return ""
    locks = {item.work_order_id: item.technician_id for item in scenario.locked_assignments}
    return content_hash(
        {
            "work_order_feasibility": work_order_assignment_feasibility_payload(order),
            "technician": technician_planning_payload(technician),
            "locked_technician_id": locks.get(order.id),
            "travel_model_fingerprint": provider.fingerprint,
        }
    )
