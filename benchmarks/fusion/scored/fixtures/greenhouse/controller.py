"""Greenhouse controller calculations."""

import statistics


def soil_moisture_pct(raw_adc: int, dry_adc: int, wet_adc: int) -> float:
    return 100.0 * (dry_adc - raw_adc) / (dry_adc - wet_adc)


def valve_duty_pct(samples: list[float]) -> float:
    return max(0.0, min(100.0, 50.0 - statistics.fmean(samples)))


def sample_id(zone: str, sequence: int) -> str:
    return f"{zone}-{sequence:04d}"


def zone_temperature_c(sensor_c: float) -> float:
    return round(sensor_c, 2)
