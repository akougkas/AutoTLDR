Parcel Dispatch Runbook
=======================

Use `dispatch.py <dispatch.py>`_ with the live feed at
`current/parcels.tsv <current/parcels.tsv>`_ and the thresholds in
`policy.toml <policy.toml>`_. An unqualified `parcels.tsv <parcels.tsv>`_ is
intentionally ambiguous because both current and archive directories contain
that basename. The missing handoff plan is `handoff-map.csv
<handoff-map.csv>`_. The carrier API at https://carrier.example/api/v1 is
external.

The shared fields are ``parcel_id``, ``route_bucket``, and
``handoff_delay_ms``.

retry_window_s = 45

max_batch_parcels = 100

A one-second human handoff note uses ``handoff_timeout_s = 1``; it is not the
same named fact as a millisecond setting.
