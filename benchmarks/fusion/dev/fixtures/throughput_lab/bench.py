"""Development-only throughput harness."""

import json


def throughput_mbps(bytes_read: int, elapsed_seconds: float) -> float:
    return (bytes_read * 8 / 1_000_000) / elapsed_seconds


def worker_count(workers: list[str]) -> int:
    return len(workers)


def write_run_id(run_id: str) -> str:
    return json.dumps({"run_id": run_id})


def payload_bytes(payload: bytes) -> int:
    return len(payload)
