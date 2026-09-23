# Chronological CSV-only evaluation

Evaluation uses history feature rows at daily UTC 19:00 origins for the
October and November 2025 tuning windows. Training uses only target vectors
whose complete 48-hour label interval ended by the fold cutoff:
`training_origin + 48 hours <= cutoff`. Scored origins must be at or after
the cutoff, and their entire target interval must end within the validation
block. This purges overlapping origin windows; it does not require an extra
48-hour gap between the actual training and validation target hours.

The fixed 14-candidate grid is compared by mean validation MAE across the two
tuning folds. That is the only selection criterion.

| Stage | Training labels complete by (UTC) | Scored origin/target block (UTC) | Purpose |
| --- | --- | --- | --- |
| October tuning | Oct 1, 2025 00:00 | Oct 1–Nov 1; daily 19:00 origins | Selection input |
| November tuning | Nov 1, 2025 00:00 | Nov 1–Dec 1; daily 19:00 origins | Selection input |
| Dec–Jan holdout | Dec 1, 2025 00:00 | Dec 1–Feb 1; daily 19:00 origins | Report only, family winners |
| Final refit | Jan 31, 2026 19:00 | No final-model training score used for selection | Final artifact |

Daily 48-hour forecasts overlap each other by 24 hours. The same observed
target can legitimately be scored twice at different forecast leads, but no
training target overlaps a scored fold. Metrics give equal weight to complete
origin/turbine vectors and report horizons 1–24 and 25–48 separately. All model
preprocessing is fitted only on the respective training split.

MAE is primary; RMSE and signed bias are supporting diagnostics. Missing target
values never enter a vector or receive imputation. Sparse or incomplete future
February labels cannot be created from the supplied January-ending CSVs, so no
February accuracy score is available.

The final overall validation winner is `artifacts/csv_only/best_model.pkl`.
Training also refits the validation winner restricted to learned families as
`best_ml_model.pkl`, without changing the overall winner. See
[completed results](results.md) for actual scores and coverage. Legacy weather
pilot scores are not part of this protocol.
