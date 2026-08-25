# Changelog

## 0.5.6 — 2026-08-25

- Added Plan Manifest V2 and transitive effective trust across Plan, decision analysis, artifacts and paired risk comparisons.
- Closed Plan-use bypasses in execution, replay, clone, restore, comparison, reports and schedules; legacy plans now require publication as a new re-attested version.
- Moved analysis state into relational columns with reservation evidence and an irreversible terminal-state trigger.
- Corrected remaining-plan paid-shift and single-day cost semantics and reused them for capacity analysis.
- Made unconfirmed outsourcing conditional, with formal feasibility, KPI and economic recommendations withheld until supplier capacity is evidenced.
- Split published-commitment and all-demand SLA, included emergency demand and randomized travel, and reported emergency completion, on-time and unserved rates.
- Added dependency-bound, idempotent risk comparisons with preflight conflict detection and read-time revalidation.
- Split assignment feasibility from objective/metadata changes and isolated malformed Plan/A rows from history lists.

## 0.5.5 — 2026-08-25

- Made required Plan, A and Artifact attestations fail closed across every read, replay and retry path; legacy status now comes from an immutable schema column.
- Added complete input, result and failure manifests plus Python, solver, database, model, platform, build and dependency runtime provenance.
- Unified replan cost, capacity and risk around the publication-time remaining-work view and fixed route-entry tail append behavior.
- Separated risk event occurrence from caused harm, modeled idle responders, persisted per-trial evidence and added auditable paired comparisons.
- Made exact retry and current rerun idempotent across crashes before A allocation.
- Preserved analysis artifacts during legacy constraint migration, quarantined malformed JSON and moved plan display metadata out of frozen payloads.
- Added ESLint/React Hooks checks, strict Pyright on provenance code, an 85% coverage gate, dependency-declaration checks and commit-pinned GitHub Actions.
- Marked outsourced capacity and SLA as unverified assumptions when no supplier commitment exists.

## 0.5.4 — 2026-08-24

- Bound every published replan to an immutable publication-time route-entry context used by risk and capacity analysis.
- Made outsourced coverage self-contained with external assignments, work-order dispositions, formal counterfactual KPIs and artifact hashes.
- Persisted plan-independent simulation scenario sets, including emergency time, location, duration and skill, and added paired risk comparison.
- Split exact retry from current-context rerun, added idempotent transactional attempt allocation and database uniqueness.
- Added result, artifact and manifest attestations, publication verification artifacts and read-time tamper detection.
- Made technician and outsourcing cost sources explicit; deprecated direct analysis routes now create audited A records.
- Added structured frontend API errors, RUNNING analysis polling and a capacity evidence viewer.

## 0.5.3

- Corrected paid-shift overtime cash cost by separating regular labor, overtime base wage and overtime premium.
- Added explicit capacity cost units; infeasible options now expose diagnostics without formal benefit, impact or payback claims.
- Split emergency occurrence from emergency-caused harm and introduced keyed common random scenarios for paired plan comparison.
- Reworked A-run requests as a discriminated union, added truthful 200/201/202 replay status and explicit retry attempts that preserve failed records.
- Attested published schedules and reused full frozen-plan integrity checks for analysis, reports and historical activation.
- Persisted complete capacity counterfactual routes as separate artifacts and made post-lock manual-reassignment context changes terminal.

## 0.5.2

- Replaced the ambiguous full-day decision scope with explicit ex-ante, actual, remaining-forecast, and combined scopes; only ex-ante frozen-plan analysis is currently enabled.
- Bound every persisted analysis to the current execution watermark, as-of time, execution-context hash, algorithm version, and build SHA.
- Added full capacity-counterfactual verification, including route continuity, fixed assignments, real-depot return, overtime limits, and explicit feasibility violations.
- Added analysis horizons, cost cadences, labor-cost modes, conservative technician archetypes, targeted skill investment, and honest tail-append placement labels.
- Split Monte Carlo mean intervals from full-day late-minute percentiles and reported absence, no-show, window, overtime, and emergency disruption separately.
- Made manual reassignment resume the same persisted Run after lock commit or process restart without duplicating the lock, D, Run, or V.
- Changed the operations-review page to read A records on entry and create them only after an explicit action; partial analysis success remains visible.
- Persisted failed and interrupted A records, deprecated direct analysis endpoints, and expanded property, fault-injection, API, and component tests.

## 0.5.1

- Bound capacity analysis to the selected plan by default and added a same-policy controlled reoptimization mode.
- Made risk simulation follow published starts and separated cash cost, service-failure loss, total economic impact, and additional disruption risk.
- Added immutable, deduplicated A-numbered decision-analysis records with snapshot, travel, policy, code, and input provenance.
- Rejected full-day analysis when execution facts require a watermarked actual/forecast context.
- Added emergency intake recovery keys, an idempotent manual reassignment command, configurable active-service estimates, and stable replan lineage.
- Moved mutable plan applicability out of frozen plan payloads and upgraded SQLite through schema v13.
- Added Pyright, dependency audits, OpenAPI snapshots, property tests, a Python 3.11 compatibility job, and stronger decision benchmarks.

## 0.5.0

- Made repeated post-completion replanning safe and separated future route sequence from immutable booking identity.
- Added actual execution-time contracts, stale-assignment start gates, actual-position travel checks and active-service overrun projection.
- Added SolverPolicy V2, retryable startup command reconciliation and database-enforced execution sequence uniqueness.
- Added integer-cent cost analysis, six capacity what-if options, seeded risk simulation and benchmark smoke checks.
- Migrated current and historical technician cost snapshots from floating units to integer cents in schema v10.
- Isolated Playwright scenarios and fixed activation/restore provenance for non-solver history operations.

## 0.2.0

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
