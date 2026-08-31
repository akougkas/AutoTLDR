"""Borealis reservoir safety controller."""

from pathlib import Path

CONFIG_PATH = Path("config.json")
TELEMETRY_PATH = Path("measurements.parquet")
EVENT_STORE = Path("safety.sqlite")


def safety_state(
    reservoir_temp_c: float,
    pressure_kpa: float,
    target_temp_c: float = 18.0,
    pressure_ceiling_kpa: float = 240.0,
) -> str:
    """Return alert when reservoir temperature or pressure exceeds policy."""
    if reservoir_temp_c > target_temp_c or pressure_kpa > pressure_ceiling_kpa:
        return "alert"
    return "nominal"
