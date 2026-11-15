# Measurement provenance

The project workspace ran the included measurements on one Linux host with local Node.js, PostgreSQL, Redis, and k6 processes. Docker was not used for these trials. The menu dataset and traffic schedule reflect recorded trial data and real workload patterns. Each included result comes from an executed workload or correctness sequence.

| Directory | Contents |
| --- | --- |
| [revision-screen/](revision-screen/) | 24 screening trials and the rule selecting comparison rates |
| [revision-confirm/](revision-confirm/) | 140 open-loop comparison and TTL sensitivity trials with run plans |
| [revision-correctness/](revision-correctness/) | 70 induced-race or last-portion trials and per-browser outcomes |
| [revision-stress/](revision-stress/) | 40 ungated hot-item concurrency trials with per-request CSV event traces |
| [revision-script-checks/](revision-script-checks/) | Direct Redis validation of decimal version ordering and generation behavior |
| [local-exploratory/](local-exploratory/) | Earlier paced A/B/C trials at 10 VUs, kept for provenance |
| [local-pilot/](local-pilot/) | Earlier functional benchmark smoke trial |
| [functional-validation/](functional-validation/) | Earlier low-stock, duplicate-order, and failure-fallback exercises |

The analysis retains the old and new studies in separate outputs. The revised paper uses the `revision-*` evidence. The older pilot and fixed-VU files record the earlier implementation's source fingerprints. Raw data used for this edition have timestamps, run plans, environment metadata, settings, snapshots, and inventory audits. See [the raw schemas](../benchmarks/CSV_SCHEMAS.md).

Two setup attempts for the correctness driver stopped before producing observations. One exposed an incorrect initial-stock expectation in the test. The other exposed parsing of formatted SQL output. Both were repaired before the recorded correctness campaign. A short functional smoke check briefly shared the host during confirmation block 1. The analysis includes a sensitivity table with that block omitted.

The retained k6 NDJSON includes custom starts and timing points, dropped iterations, and VU time series. The runner filters unrelated standard metrics after collection. Each selected point retains its original value and timestamp. The screen predates the separate freshness driver, so its recorded benchmark-tree hash excludes that file. The comparison runner, API, and database trial dataset are the same for both performance stages.
