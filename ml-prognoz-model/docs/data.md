# Data contract

## Raw observations

Each turbine CSV is expected to contain the supplied columns `ID`,
`Статистическое время`, `Средняя скорость ветра(m/s)`,
`Нормализованная активная мощность`, and
`Средняя температура окружающей среды(°C)`.

`Статистическое время` is a naive timestamp and the source does not document a
timezone. `read_observations` therefore makes the timezone an explicit
parameter, defaulting to the provisional `Asia/Almaty` assumption. It converts
the selected local civil time to timezone-aware UTC (`target_time`). The default
`ambiguous='raise', nonexistent='raise'` is intentional: a historical IANA
offset transition (including Almaty's 2024 change) must be resolved by a known
source convention, rather than silently changing an hour of data. The initial
pipeline configuration should pass the provisional fixed-offset assumption
`timezone='Etc/GMT-5'` (IANA's counterintuitive spelling for UTC+05:00); this
is an explicitly unverified assumption for this experiment and must be confirmed
against the data source before relying on operational accuracy. If the files
are actually UTC civil time, call `read_observations(..., timezone='UTC')` and
regenerate all joins, evaluations, and artifacts.

## Audit of the supplied files

Both files span March 11, 2023 00:00 through January 31, 2026 23:50 in
their raw timestamp convention, despite filenames mentioning February 28.
No February target observations are supplied. There are 25,392 candidate
hours per turbine, with the following measured coverage:

| Turbine | Raw rows | Complete hours | Empty hours | Partially observed hours | Missing 10-minute slots |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 | 142,360 | 23,667 | 1,629 | 96 | 9,992 |
| 2 | 149,499 | 24,785 | 388 | 219 | 2,853 |

Thus 48,452 complete hourly labels are available before joining forecast
weather. Neither file has duplicate timestamps or invalid raw rows under the
implemented checks. The machine-readable audit and incomplete-hour list are
in `reports/generated/data_quality.json` and `incomplete_hours.csv`.

## Hourly labels

An hourly label is emitted only when all six distinct 10-minute positions are
present with aligned timestamps, finite wind/temperature/power, and normalized
power in `[0, 1]`. Turbine IDs are strings `"1"` and `"2"`. The label schema is:

`turbine_id` (string), `target_time` (UTC hour start), `power`,
`observed_wind`, `observed_temperature`, `n_samples`, and `complete_hour`.

By default incomplete hours are excluded; they remain visible through
`ObservationReport.summary` and `ObservationReport.incomplete_hours`, including
wholly missing hours in the observed time range. The summary distinguishes
`incomplete_hours`, `total_missing_hours` (zero valid slots), and
`partially_observed_hours` (one through five valid slots). Duplicate raw
timestamps are rejected rather than averaged. No long-gap imputation is
performed.

## Legacy weather and training joins — not final pipeline

The remainder of this section documents the former archived-weather experiment
only. It is retained to explain old cache/pilot files, but is not used by the
current CSV-only final workflow. `configs/history.json`, `history.py`, and
`offline.py` use past turbine history only; no external weather file or weather
feature is read for final training or inference.

Weather must provide `turbine_id`, `forecast_origin`, `target_time`,
`weather_run_time`, `weather_available_at`, `lead_hours`,
`wind_speed_10m`, `wind_speed_100m`, `wind_u_100m`, `wind_v_100m`,
`temperature_2m` (C), `surface_pressure` (Pa), and `gust_speed`. All four time
columns are timezone-aware UTC. Optional `weather_model` and `weather_source`
may be retained as provenance metadata.

`build_training_table` joins weather to complete labels on turbine and target
hour. It requires `weather_run_time <= weather_available_at <= forecast_origin
< target_time`; origin and target are UTC-hour aligned; and integer lead
horizons from 1 through 48 must exactly match `target_time - forecast_origin`.
Wind-speed/gust values must be non-negative and pressure positive. Duplicate
weather rows per turbine/origin/target are rejected even during inference. Rows
without a valid label are skipped. During evaluation, additionally restrict labels so
`target_time + 1 hour <= training_cutoff`; the target interval must have ended
before the cutoff.

## Legacy weather inference features — not final pipeline

`build_features` produces a stable ordered schema:

`wind_speed_10m`, `wind_speed_100m`, `wind_u_100m`, `wind_v_100m`,
`temperature_2m`, `surface_pressure`, `gust_speed`, `wind_direction_sin`,
`wind_direction_cos`, `lead_hours`, `run_age_hours`, `target_hour_sin`,
`target_hour_cos`, `target_dayofyear_sin`, `target_dayofyear_cos`,
`target_hour_utc`, `target_month_utc`, `turbine_id`.

The observed power, wind, and temperature fields are deliberately never model
features: they are unavailable when forecasting the target hour. Calendar fields
are computed from the UTC target timestamp; wind direction is derived from the
100-m u/v components (calm air is encoded as `(0, 0)`); run age is forecast
origin minus weather-run time. This feature builder fits no global statistics.
Where a model uses scaling, encoding, or median imputation, those preprocessing
steps are fitted only on its training fold, inside the model pipeline. Missing
target hours are never imputed.
