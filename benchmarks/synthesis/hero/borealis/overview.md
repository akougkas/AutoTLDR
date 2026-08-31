# Borealis reservoir safety

Borealis monitors and controls the Station Alpha cooling reservoir. The
operating target `reservoir_temp_c` is 18.0 °C and the pressure ceiling
`pressure_kpa` is 240 kPa. `controller.py` reads `config.json`, consumes
`measurements.parquet`, and records safety events in `safety.sqlite`.

Scientific context is split between `experiments.h5` and `forecast.nc`.
`analytics.duckdb` stores bounded aggregate profiles, while `capacity.xlsx`
computes the reserve and `safety_margin_pct`. Operators follow
`operations.html` and review `pipeline.ipynb`.

The current calibration dependency is `calibration/current.csv`; that file is
referenced by the operating notes but is not included in this collection.
