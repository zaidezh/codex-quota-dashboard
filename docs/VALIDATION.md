# Release validation

This page records the reproducible release checks for version `0.3.0`. The
figures below describe one fixed validation environment; they are not a
cross-machine performance guarantee and do not measure prediction accuracy.

## Environment

| Item | Value |
| --- | --- |
| Operating system | Windows 11 x64, build 22631 |
| Processor tier | 16 physical cores / 32 logical processors |
| Memory | 61.5 GiB |
| Python | 3.12.10 |
| Node.js / npm | 24.13.1 / 11.8.0 |
| Browser | Chrome 153.0.8010.53 |
| Isolation | Newly created virtual environment with no inherited site packages |

The package, including test dependencies, installed from a built wheel in
`28.71 s` with the pip cache disabled; `pip check` reported no broken
requirements. Network and package-index conditions still make this an
environment record rather than a download-speed promise. Import inspection
confirmed that the test process loaded version `0.3.0` from the isolated
environment's `site-packages`, not from the repository source tree. The wheel
also contained the integration module, bootstrap reference, complete Web assets
and third-party license texts; their installed-resource hashes were verified.

## Functional gates

| Gate | Result |
| --- | --- |
| Python contracts and algorithms | 23 passed |
| Frontend contracts | 4 passed |
| Installed-package Chrome E2E | passed |
| Default configuration | monitoring, local fitting and bootstrap all off |
| Empty history | explicit `empty_history`; no fabricated forecast |
| Startup reference | `reference_only`; deterministic hash and loader passed |
| Capacity adaptation | 1×, 10×, 20× and a custom positive multiplier scaled deterministically |
| Local takeover | finite local M2 ignored the bootstrap multiplier and became M3's only parameter source |
| Embedded host | host loaded the installed public module and byte-identical public Web assets |
| Explicit collection | selected JSONL source only; duplicate import was idempotent |
| Authoritative quota persistence | timezone-aware reset timestamp normalized; duplicate snapshot was idempotent |
| Cold-start promotion gate | one quota point remained `insufficient_evidence`; explicit bootstrap drove M3 |
| Mid-cycle anchor | first authoritative used value plus explained delta produced cycle-level current balance |
| First local fit | M2 finite result; M3 preferred `local_m2_explanation` |
| Incremental fit | new request and quota observation incorporated |
| Restart | frozen state and local fitting recovered from the configured state directory |
| Disable | no new monitoring or fitting; M2 reported `disabled` |
| Local-state deletion | exact configured state directory removed only with `--yes` |
| Failed refresh | previously frozen snapshot remained byte-for-byte unchanged |
| Complete Codex UI | overview and details use the same M1-M2-M3 snapshot; M2 comparison is 705 px high |
| Responsive UI | keyboard focus present; no page overflow at 320 px |

The startup-reference scenario includes a current authoritative quota
observation and recent workload because those are necessary inputs for any M3
conditional path. The reference supplies initial coefficients only.

## Fitting benchmark

The benchmark uses a deterministic seven-day synthetic dataset with `50,000`
native requests and `132` quota observations. Each run creates a separate state
directory, builds M1, fits M2 and freezes M3.

The release acceptance band for this fixed Windows hardware tier and dataset is
`fit_and_freeze_p95 <= 60 s` with finite M2 output, conditional M3 output and
peak working set below `512 MiB`.

| Measurement | Result |
| --- | --- |
| First final-package run | 40.5183 s |
| Five-run times | 40.5183, 40.5668, 40.7115, 40.7058, 40.3210 s |
| p50 | 40.5668 s |
| p95, inclusive | 40.7104 s |
| Maximum peak working set | 211.62 MiB |
| M2 result | 5/5 `feasible`, strict status `feasible` |
| M3 result | 5/5 `conditional`, source `local_m2_explanation` |

The measured p95 is inside the release band. Workload size, numerical
conditioning, storage and processor class can change the result; deployments
should rerun the benchmark and choose their own scheduler interval.

## Reproduction

~~~powershell
python -m venv .venv-validation
.\.venv-validation\Scripts\python.exe -m pip install ".[test]"
.\.venv-validation\Scripts\python.exe -m pip check
.\.venv-validation\Scripts\python.exe -m pytest -q

npm ci
npm test
$env:PYTHON = (Resolve-Path .\.venv-validation\Scripts\python.exe)
$env:E2E_INSTALLED = "1"
$env:PLAYWRIGHT_CHANNEL = "chrome"
npm run test:e2e

.\.venv-validation\Scripts\python.exe benchmarks\benchmark_fit.py --runs 5
~~~
