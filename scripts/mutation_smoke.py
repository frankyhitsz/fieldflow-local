from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Mutation:
    name: str
    file: str
    before: str
    after: str
    test_file: str
    expression: str


MUTATIONS = (
    Mutation(
        "coverage-is-never-incomplete",
        "backend/applicability.py",
        "reduced.coverage_complete = current_demand_ids.issubset(dependencies.disposition_work_order_ids)",
        "reduced.coverage_complete = True  # mutation: ignore uncovered demand",
        "tests/test_correctness_freeze.py",
        "applicability_accumulates_invalid_assignments",
    ),
    Mutation(
        "invalid-assignments-do-not-accumulate",
        "backend/applicability.py",
        "reduced.invalid_assignment_ids = sorted(set(reduced.invalid_assignment_ids) | referenced)",
        "reduced.invalid_assignment_ids = sorted(referenced)  # mutation: discard earlier invalid assignments",
        "tests/test_correctness_freeze.py",
        "applicability_accumulates_invalid_assignments",
    ),
    Mutation(
        "no-emergency-is-perfect-completion",
        "backend/decision.py",
        "round(emergency_completed_trials / emergency_event_trials, 4) if emergency_event_trials else None",
        "round(emergency_completed_trials / emergency_event_trials, 4) if emergency_event_trials else 1.0",
        "tests/test_decision.py",
        "no_emergency_event_returns_not_applicable",
    ),
    Mutation(
        "no-emergency-has-zero-conditional-failure",
        "backend/decision.py",
        "round(emergency_caused_failure_trials / emergency_event_trials, 4) if emergency_event_trials else None",
        "round(emergency_caused_failure_trials / emergency_event_trials, 4) if emergency_event_trials else 0",
        "tests/test_decision.py",
        "no_emergency_event_returns_not_applicable",
    ),
    Mutation(
        "emergency-does-not-return-to-waiting-customer",
        "backend/decision.py",
        "arrival = current + provider.minutes(location, order.location, current)",
        "arrival = current  # mutation: omit return from emergency location",
        "tests/test_decision.py",
        "zero_randomness_candidate_projection_matches_selected_actual_terminal",
    ),
    Mutation(
        "publication-ignores-active-plan-cas",
        "backend/storage.py",
        'current_active_plan_id = current_row["active_plan_version_id"]',
        "current_active_plan_id = candidate.expected_active_plan_version_id  # mutation: ignore actual active Plan",
        "tests/test_correctness_freeze.py",
        "candidate_publication_cas_rejects_newer_active_plan",
    ),
    Mutation(
        "revision-proof-ignores-snapshot-content",
        "backend/storage.py",
        "or revision.scenario_snapshot_hash != content_hash(revision.scenario)\n                or revision.revision_hash != _scenario_revision_hash(revision)",
        "or False  # mutation: ignore revision snapshot content and proof\n                or False",
        "tests/test_correctness_freeze.py",
        "revision_proof_tampering_blocks_reset",
    ),
    Mutation(
        "fixed-only-includes-candidate-wage",
        "backend/decision.py",
        "regular_minutes * technician.cost_per_minute_cents if source.include_regular_wage else 0",
        "regular_minutes * technician.cost_per_minute_cents if True else 0",
        "tests/test_decision.py",
        "replan_cost_capacity_and_risk_share_publication_remaining_scope",
    ),
)


def run_mutation(mutation: Mutation) -> bool:
    with tempfile.TemporaryDirectory(prefix="fieldflow-mutation-") as directory:
        root = Path(directory)
        shutil.copytree(ROOT / "backend", root / "backend", ignore=shutil.ignore_patterns("__pycache__"))
        (root / "tests").mkdir()
        shutil.copy2(ROOT / mutation.test_file, root / mutation.test_file)
        target = root / mutation.file
        source = target.read_text(encoding="utf-8")
        if source.count(mutation.before) != 1:
            raise RuntimeError(f"mutation anchor drifted: {mutation.name}")
        target.write_text(source.replace(mutation.before, mutation.after), encoding="utf-8")
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(root)
        environment["FIELDFLOW_DB"] = str(root / "mutation.db")
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                mutation.test_file,
                "-k",
                mutation.expression,
                "--tb=short",
            ],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.returncode != 0


def main() -> int:
    survivors = []
    results: list[dict[str, str | bool]] = []
    for mutation in MUTATIONS:
        killed = run_mutation(mutation)
        print(f"{'KILLED' if killed else 'SURVIVED'} {mutation.name}")
        results.append({"name": mutation.name, "module": mutation.file, "killed": killed})
        if not killed:
            survivors.append(mutation.name)
    score = (len(MUTATIONS) - len(survivors)) / len(MUTATIONS)
    print(
        json.dumps(
            {
                "mutation_policy_version": "FIELD_SERVICE_SAFETY_MUTATION_V2",
                "scope": "safety-critical invariant anchors",
                "total": len(MUTATIONS),
                "killed": len(MUTATIONS) - len(survivors),
                "survived": len(survivors),
                "score": score,
                "minimum_score": 1.0,
                "results": results,
            },
            indent=2,
        )
    )
    if survivors:
        print(f"safety mutation score failed; survivors: {', '.join(survivors)}")
        return 1
    print(f"safety mutation score passed: {len(MUTATIONS)}/{len(MUTATIONS)} mutants killed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
