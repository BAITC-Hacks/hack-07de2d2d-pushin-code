# CSV-only inference

The learned CatBoost artifact is a trusted Python pickle at
`artifacts/csv_only/best_ml_model.pkl`. The overall validation winner is the
persistence baseline in `best_model.pkl`; both use the same interface. Load only artifacts produced by a trusted
run: pickle loading can execute code and the pickle does not bundle Python or
dependencies.

```python
from wind_forecast.history_artifact import HistoryForecastArtifact
from wind_forecast.io import read_table

artifact = HistoryForecastArtifact.load("artifacts/csv_only/best_ml_model.pkl")
hourly = read_table("data/processed/hourly.parquet")
prediction = artifact.predict(hourly, origin="2026-01-31T19:00:00Z")
first_24_hours = prediction.loc[prediction.lead_hours <= 24]
```

`hourly` is the prepared CSV-only hourly history, not weather or a manually
assembled future feature table. It must supply both configured turbines and at
least the most recent 24 complete hours at the requested UTC origin. The
artifact derives its past-only lag/rolling/calendar features internally. It
uses a 168-hour lookback; older inputs may be unavailable and are handled by
the fitted model preprocessing, while required recent complete labels are not
imputed.

The CLI exposes the same contract:

```bash
uv run wind-forecast --config configs/history.json predict \
  --model artifacts/csv_only/best_ml_model.pkl \
  --origin 2026-01-31T19:00:00Z --hours 24 \
  --output artifacts/csv_only/forecast_ml_24h.csv
```

`--hours` accepts 24 or 48. Lead 1 is the hour starting at `origin`; lead 48
starts at `origin + 47 hours`. Output is normalized power in `[0, 1]`, not MW,
MWh, or energy. The supplied CSVs do not contain future February actuals, so
prediction output is not an accuracy result.

For a new forecast, append only actually measured readings to the CSV inputs,
rerun `prepare`, then call `predict` with the new origin. No retraining is
required for each issuance. Omitting `--origin` uses the end of the latest
hour shared by both turbines; stale or incomplete recent history is rejected.
The first February issuance is midnight February 1 under the provisional
UTC+05 assumption. It forecasts February 1–2; later daily issuances require
new observations, so the January-only files cannot supply all February origins.

The artifact rejects origins earlier than its training cutoff. For reliable
inference use the supplied package with the locked dependency versions, not
an arbitrary Python environment. Validation scores apply to daily midnight
issuances; other whole-hour origins are accepted but have not been separately
benchmarked.
