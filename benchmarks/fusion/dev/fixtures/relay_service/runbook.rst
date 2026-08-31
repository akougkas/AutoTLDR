Relay Service
=============

The implementation is `relay.py <relay.py>`_. It writes `events.jsonl
<events.jsonl>`_ with settings from `settings.toml <settings.toml>`_. The
unwritten recovery map is `recovery-map.csv <recovery-map.csv>`_.

Operators inspect ``message_id``, ``delivery_attempts``, and ``ack_latency_ms``.

max_delivery_attempts = 5
