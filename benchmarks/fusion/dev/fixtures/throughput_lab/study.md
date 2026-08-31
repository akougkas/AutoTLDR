# Throughput Lab

The harness in [bench.py](bench.py) writes [results.csv](results.csv) using the
schema in [config.json](config.json). The pending power analysis should live in
[power-analysis.md](power-analysis.md). External run logs remain at
https://example.org/labs/throughput?quarter=q3#raw and are not collection-local.

The addressable identifiers are `run_id`, `throughput_mbps`, `worker_count`,
and `payload_bytes`.

repeat_count = 40
