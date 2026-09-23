# CSV-only model catalogue

The active model is a direct multi-output regression: one past-only feature row
at an origin predicts a `shape == (48,)` vector of normalized power. It never
recursively feeds its own prediction into a later horizon.

All candidate preprocessing is fitted on each training split only. Numeric lag
and rolling features use training-only median imputation where the estimator
requires it; `turbine_id` is one-hot encoded in sklearn pipelines. CatBoost
uses `MultiRMSE`, `thread_count=4`, and disables file writing. Output clipping
to `[0, 1]` happens at artifact prediction/reporting time, not by modifying
training targets.

| Family | Candidate | Parameters |
| --- | --- | --- |
| Persistence | `persistence_last_power` | Repeat `power_lag_1` over 48 leads |
| Seasonal median | `seasonal_median_turbine_origin_calendar` | Per-turbine/origin hour/month vector median; global fallback |
| Ridge | `ridge_alpha_0.1` | `alpha=0.1` |
| Ridge | `ridge_alpha_1` | `alpha=1.0` |
| Ridge | `ridge_alpha_10` | `alpha=10.0` |
| Extra Trees | `extra_trees_150_leaf_4_depth_16` | 150 trees, leaf 4, depth 16 |
| Extra Trees | `extra_trees_200_leaf_12_depth_16` | 200 trees, leaf 12, depth 16 |
| Extra Trees | `extra_trees_200_leaf_24_depth_12` | 200 trees, leaf 24, depth 12 |
| Random Forest | `random_forest_120_leaf_8_depth_14` | 120 trees, leaf 8, depth 14 |
| Random Forest | `random_forest_150_leaf_16_depth_12` | 150 trees, leaf 16, depth 12 |
| Random Forest | `random_forest_150_leaf_32_depth_10` | 150 trees, leaf 32, depth 10 |
| CatBoost | `catboost_250_depth_4` | 250 iterations, depth 4, LR .05, L2 3 |
| CatBoost | `catboost_350_depth_5` | 350 iterations, depth 5, LR .04, L2 5 |
| CatBoost | `catboost_400_depth_6` | 400 iterations, depth 6, LR .03, L2 8 |

This 14-configuration grid is fixed before tuning. External forecast weather,
weather-archive files, and legacy weather-model candidates are excluded.
Historical wind and temperature measured in the two CSVs are included.
