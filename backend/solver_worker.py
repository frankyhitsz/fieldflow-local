from __future__ import annotations

import math
import multiprocessing.connection
import os
from pathlib import Path
from typing import Any

from .hashing import content_hash
from .models import ScheduleResult, ScheduleScenario, StrategyProfile
from .normalization import normalize_schedule
from .scheduler import (
    baseline_schedule,
    build_solver_policy_snapshot,
    optimized_schedule,
    scenario_for_profile,
)
from .travel import EuclideanTravelTimeProvider

PROCESS_MEMORY_LIMIT_BYTES = 2 * 1024 * 1024 * 1024


def _apply_resource_limits(time_limit_seconds: float) -> None:
    if os.name != "posix":
        return
    try:
        import resource

        cpu_limit = max(5, math.ceil(time_limit_seconds) + 5)
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit, cpu_limit + 1))
    except (ImportError, OSError, ValueError):
        # The parent still enforces a wall-clock deadline and can terminate us.
        return


def process_resident_memory_bytes(pid: int, *, proc_root: Path = Path("/proc")) -> int | None:
    """Read Linux resident memory without mistaking virtual mappings for RAM."""
    try:
        status = (proc_root / str(pid) / "status").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in status.splitlines():
        if not line.startswith("VmRSS:"):
            continue
        fields = line.split()
        if len(fields) != 3 or fields[2] != "kB":
            return None
        try:
            return int(fields[1]) * 1024
        except ValueError:
            return None
    return None


def process_exceeds_memory_limit(pid: int | None, *, limit_bytes: int = PROCESS_MEMORY_LIMIT_BYTES) -> bool:
    if pid is None:
        return False
    resident = process_resident_memory_bytes(pid)
    return resident is not None and resident > limit_bytes


def solve_strategy_candidate_payload(scenario_payload: dict[str, Any], profile_payload: dict[str, Any]) -> str:
    scenario = ScheduleScenario.model_validate(scenario_payload)
    profile = StrategyProfile.model_validate(profile_payload)
    _apply_resource_limits(profile.time_limit_seconds)
    effective = scenario_for_profile(scenario, profile)
    strategy_key = profile.id if profile.builtin else "custom"
    provider = EuclideanTravelTimeProvider()
    baseline = baseline_schedule(effective, 0, strategy_key, provider=provider)
    result = optimized_schedule(
        effective,
        0,
        previous=baseline,
        time_limit_seconds=profile.time_limit_seconds,
        strategy=strategy_key,
        provider=provider,
    )
    result.solver_policy = build_solver_policy_snapshot(
        effective,
        original_scenario=scenario,
        strategy=strategy_key,
        requested_time_limit_ms=result.requested_time_limit_ms,
        solver_name=result.solver_name,
        profile_id=profile.id,
        profile_name=profile.name,
        profile_snapshot=profile.model_dump(mode="json"),
        unassigned_penalty_scale=profile.weights.unassigned_penalty_scale,
    )
    result = normalize_schedule(
        scenario,
        result,
        provider=provider,
        solver_config_hash=content_hash(effective.solver_config),
    )
    return result.model_dump_json()


def strategy_candidate_process(
    connection: multiprocessing.connection.Connection,
    scenario_payload: dict[str, Any],
    profile_payload: dict[str, Any],
) -> None:
    try:
        connection.send(("ok", solve_strategy_candidate_payload(scenario_payload, profile_payload)))
    except BaseException as error:
        connection.send(("error", {"type": type(error).__name__, "message": str(error)}))
    finally:
        connection.close()


def decision_analysis_process(
    connection: multiprocessing.connection.Connection,
    database_path: str,
    scenario_id: str,
    plan_version_id: str,
    request_payload: dict[str, Any],
    cpu_time_limit_seconds: float,
) -> None:
    try:
        _apply_resource_limits(cpu_time_limit_seconds)
        from fastapi import Response

        from . import main as main_module
        from .models import DecisionAnalysisRunRequest, PlanUseCase
        from .storage import Store

        child_store = Store(database_path, allow_migration=False)
        main_module.store = child_store
        request = main_module.TypeAdapter(DecisionAnalysisRunRequest).validate_python(request_payload)
        plan = main_module.require_plan_for_use(scenario_id, plan_version_id, PlanUseCase.analyze)
        run = main_module.execute_decision_analysis_run(
            scenario_id,
            plan,
            request,
            Response(),
            on_reserved=lambda reserved: connection.send(("reserved", {"analysis_id": reserved.id})),
            resume_interrupted=True,
        )
        connection.send(("ok", {"analysis_id": run.id, "status": run.status, "error": run.error}))
    except BaseException as error:
        connection.send(("error", {"type": type(error).__name__, "message": str(error)}))
    finally:
        try:
            from . import main as main_module

            main_module.store = None
        except ImportError:
            pass
        connection.close()


def parse_strategy_candidate(payload: str) -> ScheduleResult:
    return ScheduleResult.model_validate_json(payload)
