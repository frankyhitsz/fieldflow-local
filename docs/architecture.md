# Architecture

FieldFlow is a local FastAPI, SQLite, and React application.

## Plan publication

```text
request
  -> ScheduleRun
  -> solver result
  -> ScheduleCandidate
  -> independent verification
  -> atomic PlanVersion publication
```

`ScheduleRun` records the attempt, including failures. `ScheduleCandidate` stores the proposed schedule and verification report. `PlanVersion` is created only after publication succeeds. One failed attempt therefore does not consume a `V` number or replace the active plan.

## Revisions and versions

- `D` changes when work orders, technicians, or locks change.
- `V` changes only when a verified plan is published.
- Each plan stores the scenario snapshot, hashes, strategy, solver status, KPI, and source plan.
- Restoring history copies the old snapshot into a new `D` and a new `V`; it does not move a pointer backwards or delete later history.

## Storage

SQLite schema v4 uses foreign keys for scenario-owned data and enforces one parent for every schedule artifact. Connections use WAL and a busy timeout. Migrations create a timestamped backup before changing an existing database.

The legacy `schedules` table remains as an API compatibility projection of published plans. New business logic uses `plan_versions`, `schedule_runs`, and `schedule_candidates`.

