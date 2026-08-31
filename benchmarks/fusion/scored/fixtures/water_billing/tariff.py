"""Water tariff helpers."""


def account_ref(district: str, serial: int) -> str:
    return f"{district}-{serial:07d}"


def meter_reading_l(cubic_metres: float) -> int:
    return round(cubic_metres * 1000)


def tariff_band(annual_litres: int) -> str:
    return "high" if annual_litres > 200_000 else "standard"

