# Threat model

## Protected data

Scenario snapshots can contain customer, technician, location, schedule, and note data. The SQLite database and its migration backups are sensitive local files.

## Current trust boundary

The normal launch command binds to loopback. The application assumes the local operating-system account and browser are trusted. It does not provide user accounts, authorization, encryption at rest, or audit-grade identity records.

## Main risks

- exposing the server beyond loopback;
- HTML or UI injection through editable text;
- stale or incomplete plans being published;
- database corruption or orphan references;
- duplicate requests producing repeated business actions;
- reports or backups being copied to an unsafe location.

## Existing controls

- HTML reports escape editable values;
- publication uses revision/hash checks, an independent verifier, and one transaction;
- emergency publication supports idempotency keys;
- SQLite uses backups, foreign keys, parent checks, WAL, and integrity tests;
- generated databases, backups, environments, dependencies, and build output are gitignored.

Authentication, TLS, per-user authorization, signed audit events, secret scanning, and encrypted storage are required before network or multi-user deployment.

