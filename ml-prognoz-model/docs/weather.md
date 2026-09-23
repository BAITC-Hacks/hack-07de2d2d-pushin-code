# Archived weather replay — DEPRECATED

> This document describes a retired experiment. The final pipeline is CSV-only:
> it uses no external weather feature, weather download, cache, or weather pilot
> artifact. Use `configs/history.json`, `history.py`, and `offline.py` instead.
> Existing weather cache/pilot files must not be delivered as a final model or
> used to claim a current score.

`wind_forecast.weather` builds forecast features from archived NOAA GFS model
runs, rather than observations, reanalysis, or a current weather API. This is
essential: a row with `forecast_origin=2026-02-05T00:00:00Z` may only use data
that was available by that instant.

## Source and provenance

The primary source is NOAA's public GFS S3 bucket:

`https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.YYYYMMDD/HH/atmos/gfs.tHHz.pgrb2.0p25.fFFF`

NOAA describes this as the Global Forecast System, produced four times daily
at 00Z, 06Z, 12Z and 18Z. See the [NOAA Open Data Registry entry](https://registry.opendata.aws/noaa-gfs-bdp-pds/).
Each GRIB object has an `.idx` sidecar inventory. The reader parses that
inventory and requests only the byte ranges for:

- `UGRD`/`VGRD` at 10 m and 100 m above ground;
- `TMP` at 2 m above ground;
- `PRES` and `GUST` at the surface.

Every GRIB request must return HTTP `206 Partial Content` with an exact
`Content-Range`; an HTTP `200` response is rejected. It will never download a
global GRIB file as a fallback. The decoder uses [ecCodes](https://sites.ecmwf.int/docs/eccodes/namespaceec_codes.html)
and chooses the
nearest 0.25-degree grid point for turbine 1 `(43.645150, 78.535604)` and
turbine 2 `(43.643198, 78.538828)`.

These configured site coordinates and the NOAA bucket/model are provenance
inputs to the downloader. They should be retained in run/artifact manifests,
but a later call to `ForecastArtifact.predict` cannot independently authenticate
an arbitrary caller's values or prove that its source label/coordinates are
truthful. Use the trusted downloader with the same provider, model, bucket, and
configured sites as training.

## Leakage guard

For each configured daily origin, `select_run_for_origin` starts from the
newest 00/06/12/18Z cycle at least six hours old. It then heads the relevant
archived object and requires its S3 `Last-Modified` timestamp (when supplied)
to be no later than the simulated origin. `weather_available_at` is the later
of the six-hour guard and that timestamp. If that cannot be proved, the reader
steps back one cycle; it never moves forward to a newer run.

This produces origin-relative forecast horizons. `target_time` is computed as
`forecast_origin + lead_hours`, while `weather_run_time` stays separate. The
downloaded GFS file hour is `target_time - weather_run_time`, so a daily 18Z
origin using the safely available 12Z run requests `f007` through `f054` for
origin horizons 1--48; run-to-origin offset is intentionally not assumed to
equal lead time.

Weather lineage timestamps (`weather_run_time`, `weather_available_at`, and
`forecast_origin`), source labels, `.idx` metadata, and SHA-256 hashes provide
strong consistency and file-integrity checks. They are not independent
authentication of arbitrary values supplied outside this workflow. Do not treat
manually assembled current observations, a reanalysis, or a differently sited
weather table as a substitute for trusted future forecast-weather rows.

## Use

```python
from datetime import date
from wind_forecast.weather import GfsArchiveClient, daily_origins

client = GfsArchiveClient("data/weather-cache")
origins = daily_origins(date(2026, 1, 31), date(2026, 2, 27), hour_utc=18)

# Safe preflight: only headers and small IDX inventories, no GRIB messages.
plans = client.replay_daily_forecasts(origins, lead_hours=range(1, 49), dry_run=True)
print(sum(plan.estimated_bytes for plan in plans))

# Downloads selected messages, decodes them, then caches each origin as CSV
# plus metadata.json. A completed CSV is reused on later invocations.
# This full 28-issuance window exceeds the default 2 GiB cap: set a reviewed,
# explicit cap only after inspecting `plans`.
rows = client.replay_daily_forecasts(
    origins, lead_hours=range(1, 49), max_download_bytes=12 * 1024**3
)
```

`eccodes` and `numpy` are required for actual decoding. CSV is the default
compact cache; `cache_format="parquet"` also requires `pandas` and a Parquet
engine. `dry_run=True` is recommended before a historical replay because
individual global-grid messages are still sizeable even though they are fetched
selectively. The client uses a two-GiB default budget per invocation and fails
before any GRIB messages are downloaded when the exact IDX-based preflight
estimate exceeds it. The full Jan 31--Feb 27, 18Z example above therefore must
raise that cap; set `max_download_bytes` explicitly only after reviewing the
plans.

The cache schema is:

`turbine_id`, `forecast_origin`, `target_time`, `weather_run_time`,
`weather_available_at`, `lead_hours`, `wind_speed_10m`, `wind_speed_100m`,
`wind_u_100m`, `wind_v_100m`, `temperature_2m` (C), `surface_pressure` (Pa),
`gust_speed` (m/s), `weather_model`, `weather_source`.

The metadata sidecar records the exact forecast source URLs, model, run,
availability cutoff, delay policy, and schema. Keep it with any training
artifact so a backtest remains reproducible.

For a full 48-hour issuance, fetch 48 future lead rows for each turbine from
the same forecast origin (96 rows for these two sites). Turbine observations by
themselves cannot supply those future weather covariates. The generic artifact
prediction API may intentionally consume a valid subset, while CLI `replay`
enforces the complete per-turbine 1--48 lead set.
