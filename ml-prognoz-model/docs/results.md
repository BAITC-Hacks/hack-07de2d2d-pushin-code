# Completed CSV-only experiment

Executed September 23, 2026. Only the two supplied turbine CSVs were used.
External weather downloads were stopped at the user's request; the old cache
and pilot artifacts were neither read nor used in this experiment.

## Delivered artifacts and honest selection

- `artifacts/csv_only/best_model.pkl`: **persistence**, the overall winner of
  the predefined October–November validation comparison. It repeats the latest
  complete hourly power value across all 48 leads. This is a baseline, not a
  learned ML model, and the flat output is intentional.
- `artifacts/csv_only/best_ml_model.pkl`: **CatBoost**, the best learned
  candidate by the same validation metric. It uses 250 iterations, depth 4,
  learning rate 0.05, L2 regularization 3, MultiRMSE, and random seed 42.

Both artifacts were refitted/prepared using eligible data through
`2026-01-31T19:00:00Z`. Both include their preprocessing/feature contract and
adjacent SHA-256 manifests. The supplementary ML artifact is selected solely
from learned candidates' validation scores; it does not replace the overall
winner after inspecting the holdout.

## Results

Lower is better. MAE/RMSE are errors on normalized power in `[0, 1]`, not
percentage errors relative to actual output and not MW/MWh.

| Family validation winner | Mean validation MAE | Frozen holdout MAE | Frozen holdout RMSE |
| --- | ---: | ---: | ---: |
| Persistence — overall validation winner | 0.279276 | 0.352499 | 0.472095 |
| CatBoost — best learned validation candidate | 0.303817 | 0.315074 | 0.354906 |
| Seasonal median | 0.306230 | 0.309863 | 0.368689 |
| Ridge, alpha 10 | 0.307375 | 0.309949 | 0.354573 |
| Extra Trees, 200 trees / leaf 24 / depth 12 | 0.316052 | 0.311705 | 0.353754 |
| Random Forest, 150 trees / leaf 32 / depth 10 | 0.317704 | 0.321246 | 0.361661 |

CatBoost holdout MAE is **0.297020** for hours 1–24 and **0.333129** for hours
25–48; its overall signed bias is **+0.088155**. Persistence's corresponding
MAEs are 0.319143 and 0.385856. These are scores of frozen models trained with
labels ending by December 1, not scores of the final January-refitted weights.

The holdout ranking differs from the validation ranking. We did not change
the selection rule in response. No learned candidate beat persistence on the
declared mean validation MAE. The substantial holdout errors and ranking
instability mean this experiment does **not** establish production-quality
24/48-hour accuracy from history alone.

## Data and evaluation coverage

There are 48,452 complete hourly observation rows. The direct-window builder
retained **6,549** origin/turbine windows from 8,442 candidate pairs, spanning
3,512 distinct eligible origins. Each retained window has 90 input features
(89 numeric plus turbine ID) and 48 complete target values. Overlapping
windows reuse observations; these are not 314,352 independent target hours.

| Stage | Training windows | Scored origin/turbine windows | Scored target cells |
| --- | ---: | ---: | ---: |
| October tuning | 5,701 | 34 | 1,632 |
| November tuning | 5,853 | 56 | 2,688 |
| December–January frozen holdout | 6,093 | 110 | 5,280 |
| Final refit | 6,549 | — | — |

October validation covers 17 eligible dates (October 10–29), November 28
dates (November 1–28), and the holdout 56 distinct dates (December 1–January
29). Some dates/turbines are excluded because recent history or the entire
48-hour target window is incomplete. Holdout has 56 turbine-1 and 54 turbine-2
windows. These coverage restrictions and the small October sample matter when
interpreting the metrics.

All 14 configurations completed both tuning folds (28 fits). Six family
winners were evaluated on the frozen holdout, followed by the main final
refit and supplementary learned-model refit. Full results are in
`artifacts/csv_only/evaluation_report.json`, `leaderboard.csv`,
`candidate_tuning.csv`, and `holdout_predictions.parquet`.

## Verification and example forecasts

- 80 automated tests pass; Ruff checks pass.
- Actual saved artifacts load successfully and produce 96 bounded hourly
  predictions for the two turbines; requesting 24 hours yields 48 rows.
- Adding different future measurements in memory leaves predictions unchanged.
- The 24-hour output is exactly the first half of the 48-hour output.
- Source CSV hashes and dataset hashes are retained in the manifests.

See `reports/generated/inference_verification.json` for the real-artifact
checks. Example forecasts are `artifacts/csv_only/forecast_ml_48h.csv` and
`forecast_ml_24h.csv` for CatBoost; `forecast_48h.csv` and `forecast_24h.csv`
are the overall validation-selected baseline's outputs.

The example origin is January 31 19:00 UTC, interpreted as February 1 midnight
under the **unconfirmed fixed UTC+05 assumption**. Its 48 hours cover February
1–2. The supplied CSVs end January 31, so later daily forecasts require new
actual measurement history, and no February accuracy score can be computed.

For reproduction, use the command sequence in [reproducibility.md](reproducibility.md).
`train` automatically produces both artifacts; no backend is included.
