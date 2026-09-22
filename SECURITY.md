# Security and privacy

## Supported scope

This project is a local viewer. The server binds to `127.0.0.1` by default,
does not add CORS headers, does not persist snapshots, and does not include
analytics or telemetry.

Live mode accepts loopback upstreams only unless
`--allow-remote-upstream` is supplied. Listening beyond loopback also requires
the separate `--allow-network-bind` flag.

## Data exposure boundary

Live snapshots and runtime details can contain local task, thread, host, and
schedule metadata. The projection layer drops unneeded identifiers, applies a
strict allowlist to forecast composition, and redacts runtime identities and
titles by default. Do not enable `--show-local-titles` on a server reachable by
other users.

Never commit real snapshots, SQLite files, authentication material, account
identifiers, raw task titles, or captured API responses. Use demo mode for
screenshots and bug reports.

## Reporting a vulnerability

Open a private GitHub security advisory for the repository. Do not include
real quota snapshots, credentials, or task titles in the report.
