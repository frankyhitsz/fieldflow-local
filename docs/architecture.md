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

Every new plan uses `FIELD_SERVICE_PUBLICATION_MANIFEST_V2`. The manifest binds relational identity, immutable headers, lineage, candidate, planning context, publication verification and all Plan artifacts. Read-time validation cross-checks relational columns before the plan can enter any business operation. `require_plan_for_use` is the single gate for execution, analysis, replay, clone, restore, comparison and reports. Legacy plans remain visible for audit and can only be reused by publishing a newly attested V; the old record is never upgraded in place.

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

Published non-replan plans use `FROZEN_FULL_PLAN`. Replans use `PUBLICATION_REMAINING_PLAN`: all analyses start at the publication-time route entries and remove already frozen started or completed work. The legacy `EX_ANTE_FROZEN_PLAN` request value is accepted only as an API alias and mapped by plan type. Cost, capacity and risk share the same scoped schedule signature. Capacity defaults to selected-plan `TAIL_APPEND_ONLY` placement, then verifies the complete counterfactual route, fixed assignments, real return point, and overtime limits. Replan controlled reoptimization is rejected until it can preserve the same execution commitments.

Outsourced capacity uses explicit external assignments and one disposition per active work order. Without a supplier commitment it is `EXTERNAL_CONDITIONAL`: only a conditional upper bound and diagnostic evidence are returned, while formal feasibility, KPI and economic advice remain unavailable. Risk runs persist a plan-independent scenario-set artifact with deterministic emergency target, time, location, duration and skill; paired comparison requires the same snapshot and scenario-set hash.

Completed A records contain input, result or failure manifests, a runtime manifest, artifact hashes and an overall analysis manifest. A status, timestamps and reservation proof also live in relational columns; terminal states cannot move backwards. Reads and idempotent replays recompute the record and its parent Plan. Artifact effective trust is bounded by its parent A. Risk comparisons bind both A manifests and both trial/scenario-set artifact pairs, then revalidate every dependency on GET and idempotent replay. Modified or missing required evidence is marked `FAILED` and its business result is withheld. Schema v19 marks pre-attestation records as `LEGACY_MIGRATED` in a relational column; deleting JSON proof fields cannot downgrade a new record.

## Storage

SQLite schema v19 uses foreign keys for scenario-owned data, enforces one parent for every schedule artifact, limits each schedule run to one candidate, separates plan applicability and display metadata, and stores execution events, decision analyses, analysis artifacts and paired risk comparisons. `(logical_analysis_id, attempt_number)` and comparison idempotency keys are database-unique. Required attestation and terminal-state triggers close legacy downgrade and RUNNING rollback paths. Manual reassignment preallocates a stable schedule-run identity so restart recovery cannot duplicate the lock, revision, run or plan version; post-lock context conflicts become terminal commands. Connections use WAL and a busy timeout. Migrations create a timestamped backup, preserve valid artifacts and copy records removed by the confirmed v1 history rebuild into the quarantine ledger. Malformed Plan/A rows are skipped by list operations while exact reads retain a stable integrity error.

The legacy `schedules` table remains as an API compatibility projection of published plans. New business logic uses `plan_versions`, `plan_applicability`, `schedule_runs`, `schedule_candidates`, `decision_analysis_runs`, and `work_order_execution_events`. Saved candidates are immutable; publication rechecks their run, source, snapshot, planning context, and solver configuration.

The application factory delays Store initialization and worker creation until FastAPI lifespan startup. The current module still uses a process-local Store binding, so two app instances should not be served concurrently in one process.
