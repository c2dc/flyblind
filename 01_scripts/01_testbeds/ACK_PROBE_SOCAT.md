# ACK probe socat (opt-in)

**Default:** off (`ACK_PROBE_SOCAT=0`). Paper `./start_testbed.sh` behaviour unchanged.

When `ACK_PROBE_SOCAT=1`, the UAV `sitl` container also runs:

```text
socat UDP4-LISTEN:5763,reuseaddr,fork TCP:127.0.0.1:5762
```

alongside the paper C2 relay:

```text
socat UDP4-LISTEN:5760,reuseaddr,fork TCP:127.0.0.1:5761
```

The instrumentation script `command_ack_rtt.py` (default) uses GCS `udpout:UAV:5763` so COMMAND_ACK RTT crosses 5G without touching `:5760`.

```bash
ACK_PROBE_SOCAT=1 ./start_testbed.sh cross B0
```

Stamp fields: `ACK_PROBE_SOCAT`, `ACK_PROBE_UDP_PORT` (default 5763), `ACK_PROBE_SITL_TCP` (default 5762).
