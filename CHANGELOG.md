# Changelog

## 0.2.0 — unreleased

- Added verified `ScheduleRun` and `ScheduleCandidate` records before plan publication.
- Made failed, stale, empty, and infeasible candidates non-publishable.
- Added KPI V2, normalized workload fairness, versioned business scoring, solver provenance, and Pareto experiment results.
- Added snapshot hashes, experiment fingerprints, idempotent emergency replanning, and monotonic work-order states.
- Added TravelTimeProvider, zero-minute same-location travel, and route-local explanation evidence.
- Added SQLite schema v4 with backups, foreign keys, artifact parent checks, WAL, and integrity tests.
- Updated the React UI for stale plans, cross-day times, dynamic timelines, multiple depots, solver status wording, and accessibility labels.
- Added Ruff, TypeScript, coverage, migration, failure-path, concurrency, component, and Playwright checks.

