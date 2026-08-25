from __future__ import annotations

import ast
import os
import platform
import sqlite3
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .hashing import HASH_SCHEMA_VERSION, content_hash
from .models import DecisionAnalysisContext, DecisionInputManifest, PlanVersion, ReleaseManifest, RuntimeManifest

DECISION_ALGORITHM_VERSION = "FIELD_SERVICE_DECISION_V3"
DECISION_SOURCE_FILES = (
    "decision.py",
    "hashing.py",
    "planning.py",
    "provenance.py",
    "scheduler.py",
    "timeutils.py",
    "travel.py",
    "verification.py",
)


def _decision_model_source_closure(backend_root: Path) -> list[dict[str, str]]:
    """Hash only model definitions reachable from decision modules."""
    model_path = backend_root / "models.py"
    source = model_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    definitions: dict[str, ast.AST] = {}
    for node in tree.body:
        names: list[str] = []
        if isinstance(node, ast.ClassDef | ast.FunctionDef):
            names = [node.name]
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [target.id for target in targets if isinstance(target, ast.Name)]
        for name in names:
            definitions[name] = node

    required: set[str] = set()
    for filename in DECISION_SOURCE_FILES:
        path = backend_root / filename
        if not path.exists():
            continue
        module = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(module):
            if isinstance(node, ast.ImportFrom) and node.module == "models":
                required.update(alias.name for alias in node.names)

    selected: set[str] = set()
    pending = sorted(required)
    while pending:
        name = pending.pop()
        if name in selected or name not in definitions:
            continue
        selected.add(name)
        referenced = {node.id for node in ast.walk(definitions[name]) if isinstance(node, ast.Name)}
        pending.extend(sorted(referenced - selected))
    return [
        {
            "path": f"models.py#{name}",
            "content": ast.get_source_segment(source, definitions[name]) or "",
        }
        for name in sorted(selected)
    ]


def build_plan_manifest_payload(plan: PlanVersion) -> dict[str, object]:
    """Return the immutable V2 facts that make a published plan actionable."""
    artifacts = [
        {
            "artifact_id": artifact.id,
            "role": artifact.role,
            "strategy": artifact.strategy,
            "schedule_hash": content_hash(artifact.schedule),
        }
        for artifact in sorted(plan.artifacts, key=lambda item: (item.role, item.id))
    ]
    return {
        "policy_version": "FIELD_SERVICE_PUBLICATION_MANIFEST_V2",
        "identity": {
            "id": plan.id,
            "scenario_id": plan.scenario_id,
            "number": plan.number,
            "action": plan.action,
            "data_revision": plan.data_revision,
            "created_at": plan.created_at,
        },
        "lineage": {
            "source_version_id": plan.source_version_id,
            "lineage_source_version_id": plan.lineage_source_version_id,
            "stability_baseline_version_id": plan.stability_baseline_version_id,
            "relation": plan.relation,
            "candidate_id": plan.candidate_id,
            "source_plan_snapshot_hash": plan.source_plan_snapshot_hash,
        },
        "content": {
            "scenario_snapshot_hash": plan.scenario_snapshot_hash,
            "selected_schedule_hash": plan.published_schedule_hash,
            "planning_context_hash": plan.publication_planning_context_hash,
            "verification_artifact_hash": (
                plan.publication_verification_artifact.artifact_hash if plan.publication_verification_artifact else None
            ),
            "verification_report_hash": plan.publication_verification_report_hash,
            "verification_policy_version": plan.publication_verification_policy_version,
            "schedule_integrity": plan.schedule_integrity.value,
            "source_solver_provenance": plan.source_solver_provenance,
            "inherited_source_solver_policy_hash": (
                content_hash(plan.inherited_source_solver_policy) if plan.inherited_source_solver_policy else None
            ),
            "replay_validation_policy": plan.replay_validation_policy,
            "reattestation_mode": plan.reattestation_mode.value if plan.reattestation_mode else None,
        },
        "artifacts": artifacts,
    }


@lru_cache(maxsize=1)
def decision_build_sha() -> str:
    injected = os.getenv("FIELDFLOW_DECISION_BUILD_SHA", "").strip()
    if injected:
        return injected
    backend_root = Path(__file__).resolve().parent
    source = [
        {
            "path": path.name,
            "content": path.read_text(encoding="utf-8"),
        }
        for name in DECISION_SOURCE_FILES
        if (path := backend_root / name).exists()
    ]
    source.extend(_decision_model_source_closure(backend_root))
    return f"dev-{content_hash(source)[:16]}"


def _package_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "not-installed"


def _runtime_distribution_inventory(lock_path: Path) -> dict[str, str]:
    inventory: dict[str, str] = {}
    if not lock_path.exists():
        return inventory
    for raw_line in lock_path.read_text(encoding="utf-8").splitlines():
        requirement = raw_line.split(";", 1)[0].strip()
        if not requirement or requirement.startswith("#") or "==" not in requirement:
            continue
        distribution = requirement.split("==", 1)[0].strip()
        normalized = distribution.lower().replace("_", "-")
        inventory[normalized] = _package_version(distribution)
    return dict(sorted(inventory.items()))


@lru_cache(maxsize=1)
def decision_runtime_manifest() -> RuntimeManifest:
    root = Path(__file__).resolve().parent.parent
    runtime_lock = root / "requirements-runtime.lock"
    lock_inputs: list[dict[str, str]] = []
    for relative in ("pyproject.toml", "requirements-runtime.lock"):
        path = root / relative
        if path.exists():
            lock_inputs.append({"path": relative, "content": path.read_text(encoding="utf-8")})
    return RuntimeManifest(
        hash_schema_version=HASH_SCHEMA_VERSION,
        python_version=platform.python_version(),
        ortools_version=_package_version("ortools"),
        sqlite_version=sqlite3.sqlite_version,
        pydantic_version=_package_version("pydantic"),
        operating_system=f"{platform.system()} {platform.release()}",
        architecture=platform.machine() or "unknown",
        build_sha=decision_build_sha(),
        dependency_lock_hash=content_hash(lock_inputs),
        runtime_distributions=_runtime_distribution_inventory(runtime_lock),
    )


@lru_cache(maxsize=1)
def release_manifest() -> ReleaseManifest:
    root = Path(__file__).resolve().parent.parent
    frontend_lock = root / "frontend" / "package-lock.json"
    return ReleaseManifest(
        release_build_sha=os.getenv("FIELDFLOW_RELEASE_SHA", "dev-unreleased").strip() or "dev-unreleased",
        frontend_dependency_lock_hash=(
            content_hash(frontend_lock.read_text(encoding="utf-8")) if frontend_lock.exists() else "missing"
        ),
    )


def build_decision_input_manifest(
    *,
    analysis_type: str,
    request_snapshot: object,
    policy_snapshot: object,
    analysis_context: DecisionAnalysisContext,
    plan_manifest_hash: str,
    runtime_manifest: RuntimeManifest,
    scenario_snapshot_hash: str,
    schedule_hash: str,
    travel_model_fingerprint: str,
) -> DecisionInputManifest:
    request_hash = content_hash(request_snapshot)
    policy_hash = content_hash(policy_snapshot)
    context_hash = content_hash(analysis_context)
    runtime_hash = content_hash(runtime_manifest)
    semantic_hash = content_hash(
        {
            "policy_version": "FIELD_SERVICE_DECISION_SEMANTIC_INPUT_V1",
            "analysis_type": analysis_type,
            "request_hash": request_hash,
            "policy_hash": policy_hash,
            "analysis_context_hash": context_hash,
            "plan_manifest_hash": plan_manifest_hash,
            "runtime_manifest_hash": runtime_hash,
            "scenario_snapshot_hash": scenario_snapshot_hash,
            "schedule_hash": schedule_hash,
            "travel_model_fingerprint": travel_model_fingerprint,
        }
    )
    return DecisionInputManifest(
        request_hash=request_hash,
        policy_hash=policy_hash,
        analysis_context_hash=context_hash,
        plan_manifest_hash=plan_manifest_hash,
        runtime_manifest_hash=runtime_hash,
        scenario_snapshot_hash=scenario_snapshot_hash,
        schedule_hash=schedule_hash,
        travel_model_fingerprint=travel_model_fingerprint,
        semantic_input_hash=semantic_hash,
    )
