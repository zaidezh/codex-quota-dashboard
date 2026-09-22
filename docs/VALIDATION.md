# Release validation

This page records the reproducible release checks for version `0.2.0`. The
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

The package, including test dependencies, installed from the repository in
`30.49 s`; `pip check` reported no broken requirements. The host pip download
cache was available, so this number is an environment record rather than a
download-speed promise.

## Functional gates

| Gate | Result |
| --- | --- |
| Python contracts and algorithms | 16 passed |
| Frontend contracts | 4 passed |
| Installed-package Chrome E2E | passed |
| Default configuration | monitoring, local fitting and bootstrap all off |
| Empty history | explicit `empty_history`; no fabricated forecast |
| Startup reference | `reference_only`; deterministic hash and loader passed |
| Explicit collection | selected JSONL source only; duplicate import was idempotent |
| First local fit | M2 finite result; M3 preferred `local_m2_explanation` |
| Incremental fit | new request and quota observation incorporated |
| Restart | frozen state and local fitting recovered from the configured state directory |
| Disable | no new monitoring or fitting; M2 reported `disabled` |
| Local-state deletion | exact configured state directory removed only with `--yes` |
| Failed refresh | previously frozen snapshot remained byte-for-byte unchanged |
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
| First final-package run | 42.1952 s |
| Five-run times | 42.1952, 42.5204, 42.2630, 42.4284, 42.4015 s |
| p50 | 42.4015 s |
| p95, inclusive | 42.5020 s |
| Maximum peak working set | 218.77 MiB |
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
