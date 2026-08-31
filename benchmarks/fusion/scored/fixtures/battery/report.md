# Battery Cycle Report

The analysis in [analysis.ipynb](analysis.ipynb) reads [cycles.csv](cycles.csv)
under the bounds in [limits.toml](limits.toml). The unapproved thermal appendix
is referenced as [thermal-appendix.md](thermal-appendix.md). Manufacturer data
at https://cells.example/specs/21700 is external.

The shared native fields are `cycle_index`, `capacity_mah`, and
`internal_resistance_mohm`.

cutoff_voltage_v = 2.80

rest_period_min = 30

nominal_voltage_v = 3.60

An early note called the cutoff approximately 2.8 volts; approximate language
is not another exact fact.
