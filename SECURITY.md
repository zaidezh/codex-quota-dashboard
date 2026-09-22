# Security and privacy

## Runtime defaults

- Monitoring, local fitting, and the bootstrap reference are disabled by
  default.
- The web service binds to 127.0.0.1 by default and sends no CORS headers.
- Non-loopback listening requires the explicit --allow-network-bind flag.
- The page reads a frozen snapshot; fitting does not run synchronously in an
  HTTP request.
- The project contains no analytics or outbound telemetry.

## Local data

Configured JSONL sources and authoritative quota observations can reveal local
activity. Keep state.directory outside the repository, restrict its filesystem
permissions, and choose a retention period appropriate for the deployment.
purge-local-state --yes deletes the exact configured state directory after
safety checks.

Never commit account snapshots, SQLite files, task titles, thread/session/
response identifiers, credentials, raw prompts, captured App Server responses,
or frozen local snapshots. Use synthetic fixtures for issues and tests.

The bundled bootstrap reference is immutable and contains only the allowlisted
aggregate fields described in
[docs/BOOTSTRAP_REFERENCE.md](docs/BOOTSTRAP_REFERENCE.md). Local fitting writes
only under the configured state directory and cannot modify that asset.

## Reporting

Use the repository's private GitHub security advisory workflow. Do not attach
real runtime data or credentials.
