# Greenhouse Irrigation Protocol

The controller in [controller.py](controller.py) consumes
[readings.csv](readings.csv) and [readings.jsonl](readings.jsonl) according to
[layout.json](layout.json). A pending calibration procedure is referenced as
[calibration.md](calibration.md).
Vendor documentation is external at
https://example.org/irrigation/controller?v=2#manual and is not expected in this
collection.

The shared measurements are `sample_id`, `soil_moisture_pct`,
`valve_duty_pct`, and `zone_temperature_c`.

sample_interval_s = 15

watering_window_min = 20

An early memo estimated about 18 minutes of watering; that approximation is
not an exact assignment.
