# Changelog

## 0.2.0 — unreleased

- Added verified `ScheduleRun` and `ScheduleCandidate` records before plan publication.
- Made failed, stale, empty, and infeasible candidates non-publishable.
- Added KPI V2, normalized workload fairness, versioned business scoring, solver provenance, and Pareto experiment results.
- Added snapshot hashes, experiment fingerprints, idempotent emergency replanning, and monotonic work-order states.
- Added TravelTimeProvider, zero-minute same-location travel, and route-local explanation evidence.
- Added SQLite schema v5 with backups, foreign keys, artifact parent and one-candidate-per-run checks, WAL, and integrity tests.
- Updated the React UI for stale plans, cross-day times, dynamic timelines, multiple depots, solver status wording, and accessibility labels.
- Added Ruff, TypeScript, coverage, migration, failure-path, concurrency, component, and Playwright checks.
- Preserve emergency work orders when replanning fails and mark the last plan as partial coverage.
- Split history actions into plan reactivation, scenario cloning, and confirmed business-data rollback.
- Normalize all derived assignment fields before verification and publish Run/Candidate completion atomically.
- Add action-scoped command idempotency, snapshot-aware comparisons, and explicit replanning context.
- Add bounded, cancellable strategy experiments with partial-success and winner records.
- Delay database initialization until application startup and enforce local Host and browser Origin boundaries.
