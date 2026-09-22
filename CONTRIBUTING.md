# Contributing

Contributions are welcome when they preserve three boundaries:

1. Server observations, local estimates, and conditional forecasts remain
   visibly distinct.
2. Live mode stays loopback-first and fail-closed; it never silently replaces
   failed live data with demo data.
3. Tests and screenshots use synthetic data only.

Run before opening a pull request:

```powershell
$env:PYTHONPATH = (Resolve-Path .\src)
python -m unittest discover -s tests -p test_*.py
npm test
```

For browser verification, install Playwright and either its Chromium build or
use an existing Chrome installation:

```powershell
npm install
$env:PLAYWRIGHT_CHANNEL = 'chrome'
npm run test:e2e
```
