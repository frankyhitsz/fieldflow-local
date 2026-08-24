# Architecture

FieldFlow is a local FastAPI, SQLite, and React application.

## Plan publication

```text
request
  -> ScheduleRun
  -> solver result
  -> ScheduleNormalizer
  -> ScheduleCandidate
  -> independent verification
  -> atomic PlanVersion publication
```

`ScheduleRun` records the attempt, including failures. The normalizer rebuilds travel, SLA, lock, change, explanation, KPI, and score fields from authoritative inputs. `ScheduleCandidate` stores the normalized schedule and verification report. Completing a run and inserting its candidate use one SQLite transaction. `PlanVersion` is created only after publication succeeds, so a failed attempt does not consume a `V` number or replace the active plan.

## Revisions and versions

- `D` changes when work orders, technicians, or locks change.
- `V` changes only when a verified plan is published.
- Each plan stores the scenario snapshot, hashes, strategy, solver status, KPI, and source plan.
- Reactivating a plan requires an unchanged business snapshot and creates a new `V` without changing `D`.
- Cloning creates an independent scenario from a historical snapshot.
- Business rollback is an explicit, previewed operation. It creates a new `D` and `V` and does not delete later history.

An emergency intake commits its work order and `D` revision before replanning. If solving fails, the previous plan remains visible with `PARTIAL_NEW_DEMAND`; the work order is not rolled back.

Replanning receives a persisted `PlanningContext`. Only explicit started or completed states are frozen automatically. A time-based inference is a warning, not an execution fact.

## Experiments

Strategy experiments use one worker and a bounded four-slot queue. Cancellation is cooperative between candidate solves. A run can finish as `COMPLETED_WITH_ERRORS` when some profiles fail, and the experiment records the single candidate and plan selected for publication.

## Storage

SQLite schema v5 uses foreign keys for scenario-owned data, enforces one parent for every schedule artifact, and limits each run to one candidate. Connections use WAL and a busy timeout. Migrations create a timestamped backup before changing an existing database.

The legacy `schedules` table remains as an API compatibility projection of published plans. New business logic uses `plan_versions`, `schedule_runs`, and `schedule_candidates`.

The application factory delays Store initialization and worker creation until FastAPI lifespan startup. The current module still uses a process-local Store binding, so two app instances should not be served concurrently in one process.
