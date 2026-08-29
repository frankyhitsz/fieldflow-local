# Data retention and privacy

FieldFlow stores operational data in the configured SQLite database. Work-order names, notes, service locations, technician details, execution events, reports, and migration backups may contain confidential business information. The application does not anonymize these records.

Keep the database and exported reports on an access-controlled local disk. Do not commit them, attach them to public issues, or place them in a shared synchronization folder unless that folder has an appropriate retention policy. Deleting the repository does not remove copies exported elsewhere.

Public Plan versions, Scenario revisions, execution events, and analysis records form the audit trail and are not pruned automatically. Content-addressed blobs are removed only when no persisted record references them and they are older than the selected retention period. Preview the eligible set before applying a prune:

```bash
fieldflow artifacts prune --retention-days 30
fieldflow artifacts prune --retention-days 30 --apply
fieldflow artifacts vacuum
```

Migration commands create safety backups next to the database. Review and remove obsolete backups according to the operator's own retention requirements. FieldFlow cannot determine when a customer contract, regulation, or incident hold requires data to be retained.

The content hashes detect accidental changes and broken relationships. They are stored beside the data and are not signatures against an administrator who can rewrite the database. See `SECURITY.md` before binding the service to anything other than `127.0.0.1`.
