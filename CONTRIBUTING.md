# Contributing

## Set up

```bash
make setup
make verify
```

Use a temporary database while developing:

```bash
FIELDFLOW_DB=/tmp/fieldflow-dev.db make demo
```

## Change rules

- Keep published `V` versions separate from data revision `D` numbers.
- Never publish a solver result before it passes `backend/verification.py`.
- A failed, cancelled, stale, empty, or infeasible candidate must leave the active plan unchanged.
- Database migrations must back up existing data and preserve v2+ plan history.
- Add a regression test for each bug fix.
- User-facing Chinese should be short, factual, and free of unsupported claims such as “最优” or “显著提升”.

Run `make verify` before opening a pull request. Describe any check that cannot run on your machine.

