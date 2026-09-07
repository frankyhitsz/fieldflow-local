from __future__ import annotations

import math
import multiprocessing.connection
import os
from pathlib import Path
from typing import Any

from .database_lock import database_access
from .hashing import content_hash
from .job_authority import JobAuthority, job_authority
from .models import PlanningContext, ScheduleResult, ScheduleScenario, StrategyProfile
from .normalization import normalize_schedule
from .scheduler import (
    baseline_schedule,
    build_solver_policy_snapshot,
    optimized_schedule,
    replan_schedule,
    scenario_for_profile,
)
from .travel import DEFAULT_TRAVEL_PROVIDER, restore_travel_provider

PROCESS_MEMORY_LIMIT_BYTES = 2 * 1024 * 1024 * 1024


def stop_process(process) -> None:
    if process is None or process.pid is None:
        return
    if process.is_alive():
        process.terminate()
    process.join(timeout=2)
    if process.is_alive():
        process.kill()
        process.join(timeout=2)
    if process.is_alive():
        raise RuntimeError("CHILD_PROCESS_DID_NOT_STOP")


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


def solve_strategy_candidate_payload(
    scenario_payload: dict[str, Any], profile_payload: dict[str, Any], provider_payload: dict | None = None
) -> str:
    scenario = ScheduleScenario.model_validate(scenario_payload)
    profile = StrategyProfile.model_validate(profile_payload)
    _apply_resource_limits(profile.time_limit_seconds)
    effective = scenario_for_profile(scenario, profile)
    strategy_key = profile.id if profile.builtin else "custom"
    provider = restore_travel_provider(provider_payload) if provider_payload else DEFAULT_TRAVEL_PROVIDER
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
    provider_payload: dict | None = None,
) -> None:
    try:
        connection.send(("ok", solve_strategy_candidate_payload(scenario_payload, profile_payload, provider_payload)))
    except BaseException as error:
        connection.send(("error", {"type": type(error).__name__, "message": str(error)}))
    finally:
        connection.close()


def replan_candidate_process(
    connection, scenario_payload, previous_payload, planning_time, limit, strategy, context_payload, provider_payload
) -> None:
    try:
        _apply_resource_limits(limit)
        result = replan_schedule(
            ScheduleScenario.model_validate(scenario_payload),
            0,
            ScheduleResult.model_validate(previous_payload),
            planning_time,
            limit,
            strategy,
            planning_context=PlanningContext.model_validate(context_payload),
            provider=restore_travel_provider(provider_payload),
        )
        connection.send(("ok", result.model_dump_json()))
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
    authority: JobAuthority,
    provider_payload: dict,
    operation: dict | None = None,
) -> None:
    try:
        _apply_resource_limits(cpu_time_limit_seconds)
        from fastapi import Response

        from . import main as main_module
        from .models import DecisionAnalysisRunRequest, PlanUseCase
        from .storage import Store

        child_store = Store(
            database_path,
            travel_provider=restore_travel_provider(provider_payload),
            allow_migration=False,
            recover_runtime=False,
        )
        main_module.store = child_store
        request = main_module.TypeAdapter(DecisionAnalysisRunRequest).validate_python(request_payload)
        plan = main_module.require_plan_for_use(scenario_id, plan_version_id, PlanUseCase.analyze)
        with database_access(database_path), job_authority(authority):
            if operation:
                handler = (
                    main_module.retry_decision_analysis_run
                    if operation["mode"] == "retry"
                    else main_module.rerun_decision_analysis_current
                )
                run = handler(scenario_id, operation["analysis_id"], Response(), operation["idempotency_key"])
            else:
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
