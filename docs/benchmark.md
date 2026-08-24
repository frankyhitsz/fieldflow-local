# Benchmark status

The repository does not publish performance claims. The 8-technician/60-work-order and 12-technician/100-work-order fixtures are deterministic product checks, not representative field data.

`make benchmark-smoke` verifies the currently expressible scenario families and decision-model invariants. In particular, it checks that selected-plan capacity analysis uses the actual V signature, controlled analysis declares one common policy, risk follows published starts, travel fingerprint mismatches and execution-state analysis are rejected, and cost components reconcile. It also reports capability gaps instead of treating them as passing cases: Crew visits, cross-day planning and parts inventory are currently unsupported; multi-depot is limited to per-technician origins and a transparent start-location what-if.

A publishable benchmark needs several seeds and scenario families, cold and warm runs, p50/p95 runtime, feasible rate, verifier pass rate, KPI deltas against the greedy baseline, stability results, hardware details, and the solver, metric, travel, and score policy versions.
