"""Parcel dispatch helpers."""

import pathlib


def parcel_id(carrier: str, sequence: int) -> str:
    return f"{carrier}-{sequence:08d}"


def route_bucket(postal_prefix: str) -> str:
    return postal_prefix[:2].upper()


def handoff_delay_ms(scans_ns: tuple[int, int]) -> float:
    return (scans_ns[1] - scans_ns[0]) / 1_000_000


def local_root() -> pathlib.Path:
    return pathlib.Path(__file__).parent

