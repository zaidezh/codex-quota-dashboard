# Bootstrap reference contract

The bundled bootstrap reference provides M3 with an initial parameter vector and
conservative parameter bounds while a deployment has insufficient local evidence
for M2. It is an algorithm-support asset with the state `reference_only`.

The package is disabled by default. An operator enables it with:

```toml
[bootstrap_reference]
mode = "bundled"
```

M3 records `reference_source=bootstrap_reference` and the immutable
`reference_id`. When explicitly enabled local fitting later produces a finite
`feasible` or `approximate` M2 explanation, that frozen local explanation takes
priority. Local results are stored only under the configured state directory and
never modify the bundled file.

## Contents and units

Each model has `uncached_input`, `cached_input`, and `output` channels. Values are
percentage points of an account quota window per one million tokens. `reference`
is the initialization value; `lower` and `upper` are conservative parameter
bounds used for sensitivity projection. They are not probability intervals.

The asset was produced by an allowlisted export of fitted aggregate coefficients:
values were rounded and bounds were expanded outward. The package carries no raw
observations, exact observation times, sample counts, design matrices, local
paths, runtime identifiers, prompts, payloads, or credentials. The adjacent
manifest records the field classes, SHA-256 digest, release checks, and license.

## Interpretation

The reference is not an official tariff, billing formula, universal account
conversion, or accuracy guarantee. Its purpose is to make an explicitly selected
cold-start path numerically usable. M2 evidence and out-of-time validation in the
target environment remain the basis for local adaptation.
