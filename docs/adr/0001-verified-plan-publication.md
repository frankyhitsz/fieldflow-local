# ADR 0001: Publish only verified candidates

Status: accepted

## Decision

A solver call creates a `ScheduleRun`. Any returned schedule is stored as a `ScheduleCandidate` with an independent verification report. `PlanVersion` publication requires that candidate ID and rechecks its scenario revision, snapshot hash, publishability, and exact schedule payload inside the publication transaction.

## Reason

Solver status alone cannot prove coverage, business constraints, KPI correctness, or freshness. Keeping attempts and candidates separate also prevents failures from consuming public version numbers.

## Consequence

Low-level publication calls must first persist a verified candidate. Failed attempts remain queryable and the previous active plan remains unchanged.

