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
- `solver_config_hash` identifies the strategy config actually passed to the solver; the common business score is recomputed separately and never replaces that provenance.
- Travel provenance contains both a readable model version and a fingerprint of the effective configuration or imported matrix.
- Current Euclidean and matrix providers are static. Their interface accepts departure time for future providers, but the application does not claim time-dependent travel until a versioned time-bucket model exists.
- Reactivating a plan requires an unchanged business snapshot and creates a new `V` without changing `D`.
- Cloning creates an independent scenario from a historical snapshot.
- Resetting a scenario restores its own first revision, so a clone returns to the snapshot it was created from rather than to a similarly named built-in fixture.
- Business rollback is an explicit, previewed operation. It creates a new `D` and `V` and does not delete later history.
- Runtime applicability is stored in `plan_applicability`. Reading a plan overlays `active` and `coverage_status` without rewriting the frozen `plan_versions.payload`.
- Replan lineage stores both the immediate source and the original stability baseline. Activating a historical replan preserves the latter.

An emergency intake commits its work order and `D` revision before replanning. Intake and each solve attempt have separate idempotency records. If solving fails, the previous plan remains visible with `PARTIAL_NEW_DEMAND`; the work order is not rolled back and another attempt can be started without receiving it twice.

The intake command also stores the exact solve publication key. Startup reconciliation can therefore distinguish an intake that still needs a retry from a solve that already published, without guessing a key from the command namespace.

Replanning receives a persisted `PlanningContext`. Only explicit started or completed states are frozen automatically. A time-based inference is a warning, not an execution fact.

Publishing a replan also creates an immutable `PublicationPlanningContext`. It records a route entry for every technician, the publication planning time and execution sequence, source-plan hashes, and frozen assignment identities. Later risk and selected-plan capacity analysis use this context instead of restarting at the depot. Historical replan versions without it reject route-sensitive analysis.

## Execution events

Work-order status is not editable through the generic update route. Starting and completing service are explicit commands tied to the active plan assignment, technician, occurrence time, expected `D`, and idempotency key. The scenario update, revision, command result, and immutable execution event commit in one transaction. A completed assignment remains traceable to its source plan but is not copied into a future schedule.

## Experiments

Strategy experiments use one worker and a bounded four-slot queue. Cancellation is cooperative between candidate solves. A cancellation request cannot be overwritten by stale worker progress. A run can finish as `COMPLETED_WITH_ERRORS` when some profiles fail, and the experiment records the single candidate and plan selected for publication.

## Decision analysis

Cost, capacity, and risk analysis validate the selected plan's snapshot, publication evidence, solver policy, full schedule constraints and travel fingerprints. The UI creates a `DecisionAnalysisRun` instead of displaying an untracked recalculation; deprecated synchronous routes now do the same internally. Runs receive a scenario-local `A` number and persist the discriminated request, input policy, code version, input hash, attempt lineage and result. Running and completed inputs are deduplicated. Exact retry keeps the original frozen input and uses a database-unique attempt number; current-context rerun starts a new logical analysis.

The implemented decision scope is `EX_ANTE_FROZEN_PLAN`. Once execution events exist, clients must select it explicitly; the record binds the plan's publication context and states that actual execution is excluded. Current events do not change a historical input hash. Actual, remaining-forecast, and combined scopes are reserved enum values and return a stable unsupported error. Capacity defaults to selected-plan `TAIL_APPEND_ONLY` placement, then verifies the complete counterfactual route, fixed assignments, real return point, and overtime limits. Replan controlled reoptimization is rejected until it can preserve the same execution commitments.

Outsourced capacity uses explicit external assignments and one disposition per active work order. Its formal KPI includes internal, external and unserved counts, while the embedded schedule remains the internal route diagnostic. Risk runs persist a plan-independent scenario-set artifact with deterministic emergency target, time, location, duration and skill; paired comparison requires the same snapshot and scenario-set hash.

Completed A records contain a result hash, artifact hash manifest and overall analysis manifest. Reads recompute them. Modified or missing evidence is marked `FAILED`; pre-attestation records remain readable as `LEGACY_UNATTESTED`.

## Storage

SQLite schema v17 uses foreign keys for scenario-owned data, enforces one parent for every schedule artifact, limits each schedule run to one candidate, separates plan applicability, and stores work-order execution events, decision-analysis runs and analysis artifacts. `(logical_analysis_id, attempt_number)` is database-unique. Analysis runs keep running, completed, failed and interrupted attempts. Manual reassignment preallocates a stable schedule-run identity so restart recovery cannot duplicate the lock, revision, run or plan version; post-lock context conflicts become terminal commands. Connections use WAL and a busy timeout. Migrations create a timestamped backup before changing an existing database.

The legacy `schedules` table remains as an API compatibility projection of published plans. New business logic uses `plan_versions`, `plan_applicability`, `schedule_runs`, `schedule_candidates`, `decision_analysis_runs`, and `work_order_execution_events`. Saved candidates are immutable; publication rechecks their run, source, snapshot, planning context, and solver configuration.

The application factory delays Store initialization and worker creation until FastAPI lifespan startup. The current module still uses a process-local Store binding, so two app instances should not be served concurrently in one process.
