# History-only features and targets

`history.py` constructs one feature row per turbine/origin from prepared hourly
CSV observations. Every numeric feature is causal: it uses only values strictly
before its origin (the measured hourly interval must have ended by that time).

The feature frame contains `turbine_id` plus 89 numeric lag and
rolling summaries of normalized power, observed wind, and observed temperature.
`power_lag_1` is the most recent normalized power and is the persistence
baseline's required input. Target hour/month are calendar inputs for the
seasonal-median baseline. No external weather column or future observation is a
feature.

Origins occur every six hours, anchored at UTC hour 19. A target is a direct
48-column vector: vector element 1 is normalized power in the hour beginning
at the origin, and element 48 is the hour beginning 47 hours later. A training
vector is accepted only if all 48 target hours are complete observed labels.

Feature construction requires the last 24 complete hourly labels. It requests
up to 168 historical hours so older lags and rolling windows are available when
the source permits. Missing older inputs remain `NaN`; only fitted model
preprocessing may impute them. Missing recent required hours invalidate the
origin. Missing target values are never imputed.

See [models](models.md) for the fixed 14 direct-vector estimators and
[evaluation](evaluation.md) for the cutoff rules.

For power, wind, and temperature, lag offsets are 1, 2, 3, 6, 12, 24, 48, 72,
and 168 hours. Rolling mean, standard deviation, minimum, and maximum use
6/24/48/168-hour windows. Four coverage fractions describe missing historical
hours. Calendar features contain origin hour/month and sine/cosine encodings
of hour, weekday, day of year, and month. Despite the legacy names
`target_hour_utc` and `target_month_utc`, these two fields denote the origin
hour/month, which is also the start of the first target interval.
