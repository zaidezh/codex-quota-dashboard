# Fresh-machine validation

Checked on 2026-09-22 (Asia/Shanghai) from a pristine public Git clone in a new
temporary directory and a newly created Python virtual environment. No package
from the development checkout was added to `PYTHONPATH`.

## Environment

| Item | Value |
| --- | --- |
| OS | Windows 11 Pro for Workstations, build 22631 |
| Python | 3.12.10 |
| Node.js | 24.13.1 |
| npm | 11.8.0 |
| Public baseline | `327af33f495d2887f22ea2271cd4994e91e437f4` |

The baseline commit is the application build that was cold-tested before this
documentation update. The current commit is also exercised by the repository's
fresh GitHub Actions runner after each push.

## Results

| Gate | Result | Evidence |
| --- | --- | --- |
| Clean clone | PASS | Shallow-cloned the public HTTPS repository into a new GUID-named directory |
| Isolated install | PASS | New virtual environment initially contained only `pip`; non-editable, no-cache install completed in 9.074 s; `pip check` reported no broken requirements |
| Python tests | PASS | 11 tests passed in 0.620 s |
| Frontend contract tests | PASS | `npm ci` used a new cache directory; `npm test` passed |
| Browser flow | PASS | Chrome-backed E2E passed from the clean clone |
| Demo snapshot generation | PASS | 500 measured runs: median 0.808 ms, p95 0.900 ms after privacy projection; 53 future points returned |
| HTTP viewer | PASS | `/healthz` and `/api/dashboard` returned `200` and a complete 53-point demo curve |
| Real-account model fitting | **NOT INCLUDED** | Installed package has no `forecast_v2` module, historical ingestion, or fitting entry point |
| Real-account fit-time target | **NOT TESTABLE** | The returned curve is explicitly marked `synthetic_demo` / `Synthetic data. Not an account forecast.` |

The measured sub-millisecond operation is deterministic demo generation, not
model fitting. It must not be quoted as forecast training or fitting latency.

## Reproduce

```powershell
git clone --depth 1 https://github.com/zaidezhang728-arch/codex-quota-dashboard.git
Set-Location .\codex-quota-dashboard
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --no-cache-dir .
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_*.py
npm ci
npm test
$env:PLAYWRIGHT_CHANNEL = 'chrome'
npm run test:e2e
```

## Acceptance boundary

This repository currently passes a fresh-machine viewer/demo acceptance, not a
self-contained forecasting-engine acceptance. A real forecasting acceptance
would require the engine and its locked dependencies, a documented local data
contract, privacy-safe cold-start fixtures, a deterministic CLI, and measured
cold/warm timing thresholds. Until those artifacts are present, live mode must
continue to be described as a read-only consumer of upstream predictions.
