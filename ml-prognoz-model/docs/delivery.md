# Delivery contract

Original task: `create mr and push to https://github.com/BAITC-Hacks/hack-07de2d2d-pushin-code`

## Goal and acceptance criteria

Deliver the completed CSV-only forecasting experiment as a separate package.

- Include preprocessing, causal 24/48-hour forecasting, all 14 model configurations,
  chronological tuning, tests, and documentation.
- Include the original fitted pickles, score tables, manifests, and example forecasts.
- Reuse the repository's unchanged raw CSVs; both SHA-256 hashes match the original run.
- Verify the package and existing application, then open one PR without merging.

## Scope and assumptions

Only `ml-prognoz-model/` is added. No backend, frontend, production model, shared
contract, deployment, or external weather download is part of this change.
The production contract v0.5 describes a different weather-only quantile model;
this experiment does not implement or replace that contract.

The original source files and fitted results are copied unchanged except for
repository-relative input paths, packaging/formatting, and delivery documentation.
Run manifests retain original paths as historical provenance, not current file locations.
Generated hourly and supervised tables are reproduced locally and are not committed.
The original fitted pickles are not retrained or selected using holdout scores.

## Risks and limitations

- Timezone remains the explicit, provisional fixed UTC+05 assumption.
- Persistence wins the declared validation rule; CatBoost is the best learned
  candidate. Both are delivered, with no claim that CatBoost wins every period.
- Normalized point forecasts only; no calibrated prediction intervals or MW conversion.
- Prediction requires recent actual history. No complete February replay or scoring
  is possible from these CSVs alone, which end on January 31.
- Load pickle files only from trusted sources, using the locked environment.
- Legacy weather code is retained for source/test completeness but is not called by
  the default CLI and contributes no data to these models.

## Handoff metadata

Branch: `csv-only-forecast-models`. Base: `main`. Forge: GitHub.
The PR body and taskflow receipt record the worktree, committed SHA, exact checks,
and review URL; those values are not embedded recursively into the commit itself.
