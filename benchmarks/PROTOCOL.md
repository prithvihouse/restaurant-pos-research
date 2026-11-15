# Revised experimental protocol

## Controls

Each trial starts PostgreSQL, Redis, and the API in temporary directories on loopback. All policies use the same schema, connection pool, SQL, trial datasets, and k6 script. The nominal open-loop traffic mix is four item GETs and one one-unit POST per five iterations, with order positions and items replayed from recorded trial seeds. Eighty percent of item selections target the 20 configured hot items. Start and end timestamps and seed hashes are in the trial manifests.

## Freshness and orders

1. **Induced stale refill.** Initial ingredients yield two portions. Sell one. Force a miss for a related item and hold its SQL snapshot. Sell the last portion and wait for acknowledgment. Release the delayed GET, then issue a new GET. Compare both responses against the authoritative item version. Repeat ten times each in A, C, and D. Plain TTL is tested elsewhere because this experiment isolates the race after deletion.
2. **Stockout exposure.** Sell one of two portions, read the related item to prime its positive availability, then sell the remaining portion. After the order acknowledges, start eight concurrent browsers. A browser tries to buy only if its GET shows at least one portion. An attributable event requires a stale positive display, zero authoritative stock before browsing, and an insufficient-inventory rejection. Eight browsers share one round. Ten rounds are recorded per policy.
3. **Hot-item stress.** Run six readers and two order writers for 1.5 seconds against one item, with ten trials per policy. A successful GET is eligible once the driver has recorded an acknowledged order version. The comparison uses the highest acknowledged version recorded before the GET. An observed version below that bound indicates staleness. Save the interaction trace. Keep the test gate disabled.

The inventory audit checks every ingredient against its starting quantity and the committed orders. Redis is outside the inventory authorization path. The results characterize these schedules. In-progress reads and cache failures have their own semantics.

## Performance

- The `constant-arrival-rate` executor schedules one request per iteration with 128 preallocated VUs, no think time, and a three-second graceful stop. Warm-up uses item reads only at 300/s for two seconds.
- The screen runs each mode once at 200, 600, 1,200, 2,400, 4,800, and 8,000 arrivals per second. It selects the first rate with a mode under 95% delivery or above 50 ms overall P95. The reference rate is 600/s. For each selected rate, compare all four modes at 3 s TTL for ten five-second windows per mode.
- Hold arrival rate at the reference and sweep TTL in B and D at 1, 3, and 10 seconds. Use ten 12-second windows per mode and TTL value. Treat these as short-window sensitivity tests.
- Use successful in-window completions for goodput, and successful starts observed through the drain for latency. For the load criteria, overall P95 uses both endpoints. Report started work, dropped iterations, failures, and nominal schedule discrepancies separately. The driver shares the host with the API and data services. Drops can reflect occupied VUs, client limitations, or a slower system.
- Use API hit and miss counters for payload lookup rates. D's MGET also reads generation metadata. Use PostgreSQL statement statistics to count the tagged item query. Report SQL reads per 1,000 started item GETs. These are query counts, with CPU outside that denominator.
- Compute trial medians and 95% percentile bootstrap intervals by resampling whole trials. For differences, pair policies by repeat and resample the paired differences. Browser or request counts are descriptive and do not supply independent replicates.

## Validity checklist

- [ ] Archive the screen, selection rule, schedule, and effective rates.
- [ ] Check warm-up and measurement durations, dependencies, settings, and inventory audits.
- [ ] Verify that a failed attempt, dropped iteration, and inventory rejection retain their own labels.
- [ ] Recalculate the paper tables from raw records and preserve actual observed variation.
- [ ] Keep prior closed-loop results and external replications in separate result directories.
