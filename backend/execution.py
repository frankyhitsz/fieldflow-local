from __future__ import annotations

from .models import ExecutionSourceContext

ACTIVE_SERVICE_MINIMUM_REMAINING_MINUTES = 15


def execution_context_for_planning(
    context: ExecutionSourceContext,
    planning_time: int,
    default_remaining_minutes: int = ACTIVE_SERVICE_MINIMUM_REMAINING_MINUTES,
) -> tuple[ExecutionSourceContext, list[str]]:
    """Apply the conservative active-service overrun policy to an event projection."""
    projected = context.model_copy(deep=True)
    warnings: list[str] = []
    for projection in projected.technician_projections:
        if projection.state != "started" or planning_time < projection.available_at:
            continue
        projection.overrun = True
        projection.estimated_remaining_minutes = default_remaining_minutes
        projection.available_at = planning_time + projection.estimated_remaining_minutes
        warnings.append(
            f"ACTIVE_SERVICE_OVERRUN:{projection.source_work_order_id}:{projection.estimated_remaining_minutes}"
        )
        source = next(
            (item for item in projected.started_assignments if item.work_order_id == projection.source_work_order_id),
            None,
        )
        if source:
            source.projected_available_at = projection.available_at
    return projected, sorted(warnings)
