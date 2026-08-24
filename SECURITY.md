# Security

## Supported version

Security fixes are applied to the current `main` branch. There is no long-term support branch yet.

## Reporting a problem

Do not put database contents, customer names, addresses, tokens, or other private data in a public issue. Contact the repository owner through GitHub with:

- the affected commit;
- a short reproduction;
- the likely impact;
- whether the report can be made public after a fix.

## Local-data boundary

FieldFlow binds to `127.0.0.1` by default and does not need an external map API. This is not an authentication boundary. Do not expose the service to an untrusted network without adding authentication, TLS, request limits, and an explicit CORS policy.

The server accepts only configured local Host values and rejects browser write requests from unknown origins. These checks reduce accidental cross-site access; they do not replace authentication. The current API has no launch token.

`fieldflow.db`, migration backups, exported reports, customer names, work-order notes, and location coordinates can contain sensitive business data. Keep them out of shared folders and source control. Reports escape user-provided text, but they are not anonymized.
