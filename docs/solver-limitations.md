# Solver limitations

- Coordinates are a local 0–100 grid, not road-network geography.
- The default travel model is deterministic Euclidean travel. It has no traffic, parking, weather, or live-map data.
- A result marked `FEASIBLE` is executable but not proven globally optimal.
- `TIME_LIMIT_FEASIBLE` means the search stopped at the limit with a candidate. `TIME_LIMIT_NO_SOLUTION` means it stopped without one.
- Route insertion evidence compares travel deltas against existing routes. Alternative deltas do not rerun time-window feasibility and are labelled accordingly.
- The current execution model records idempotent start and complete events against the formal assignment. Dispatch acceptance, en-route, arrival, cancellation, parts, crews, breaks, and multi-day work are not modelled.
- The included fixtures demonstrate trade-offs; they are not a production benchmark.
