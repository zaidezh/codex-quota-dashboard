# Contributing

Changes are welcome when they preserve these contracts:

1. M1 observations, M2 explanations, and M3 conditional forecasts remain
   separately identifiable.
2. Monitoring, local fitting, and bootstrap use stay explicit and auditable.
3. Public fixtures, screenshots, benchmarks, and tests contain only synthetic
   or separately approved safe data.
4. A passing test or rendered curve is not described as an official rate,
   accuracy guarantee, or external production adoption.

Run before opening a pull request:

~~~powershell
python -m pip install ".[test]"
python -m pytest -q
npm ci
npm test
npm run test:e2e
~~~

Also run the public-release scan documented in
[docs/VALIDATION.md](docs/VALIDATION.md) whenever files, examples, screenshots,
or reference data change.
