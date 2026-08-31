"""Development relay implementation."""


def message_id(topic: str, offset: int) -> str:
    return f"{topic}:{offset}"


def delivery_attempts(previous: int) -> int:
    return previous + 1


def ack_latency_ms(sent_ns: int, ack_ns: int) -> float:
    return (ack_ns - sent_ns) / 1_000_000

