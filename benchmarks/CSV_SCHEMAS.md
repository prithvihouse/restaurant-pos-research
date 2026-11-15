# Revised benchmark files

| File | Contents |
| --- | --- |
| `plan.csv` | Trial ID, cell, repeat block, mode, TTL, offered rate, measured and warm-up seconds, and workload seed |
| `metadata.json` | Run status, times, host and software, source fingerprints, and inventory audit |
| `settings.json`, `trial_data.json`, `seed.sql` | Effective benchmark controls and recorded database trial dataset |
| `warmup.ndjson.gz`, `measured.ndjson.gz` | Original k6 points for `pos_started`, `pos_http_ms`, `dropped_iterations`, `vus`, and `vus_max` |
| `summary.json` | Per-endpoint starts, outcomes, timing, in-window goodput, scheduled work, drops, and database and Redis activity |
| `pg_before.csv`, `pg_after.csv`, `sql_deltas.csv` | Top-level SQL call counts and selected statement execution statistics |
| `redis_before.txt`, `redis_after.txt`, `api_before.json`, `api_after.json` | Service snapshots and application-level lookup and invalidation counters |
| `orders.csv.gz`, `inventory_audit.csv` | Committed orders and ingredients reconciled against initial stock |
| `events.csv.gz` | Hot-item request, worker, monotonic request start, status, acknowledged reference version, response version, and stale flag |
| `analysis/revision-output/*.csv` | Trial results, aggregate medians and bootstrap bounds, paired differences, first-block sensitivity, and correctness observations |

Latency is in milliseconds. A rate in requests per second uses the configured measurement duration. The cache hit denominator is classified payload hits plus misses. Stock versions represent integer sums of ingredient revisions. Missing or inapplicable rates remain empty instead of using zero.
