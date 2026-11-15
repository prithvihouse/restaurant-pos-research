# Revised benchmark instructions

Run commands from the project root on Linux with the prerequisites listed in [README.md](../README.md). Start with a smoke run and use new output directories. The commands below write under the ignored `results/runs/` path so the original measurements remain intact.

~~~sh
npm ci --prefix backend
python3 benchmarks/open_load.py smoke --output results/runs/repeat-smoke
python3 benchmarks/open_load.py screen --output results/runs/repeat/revision-screen
~~~

The screen uses six arrival rates and four policies for 24 short trials. Inspect the screen summaries for the first offered rate at which any policy delivers under 95% of offered requests or exceeds 50 ms overall P95. Use that rate as the high condition and 600 requests/s as the reference if the screen confirms it is below pressure. The included campaign selected 4,800 requests/s. Be prepared to choose different rates for another host. Pin the screen and selection rule before running confirmation.

~~~sh
python3 benchmarks/open_load.py confirm --low 600 --high 4800 \
  --output results/runs/repeat/revision-confirm
python3 benchmarks/check_freshness.py --output results/runs/repeat/revision-correctness
python3 backend/stress_freshness.py --output results/runs/repeat/revision-stress
python3 backend/check_generation.py --output results/runs/repeat/revision-script-checks
python3 analysis/revision.py --verify-raw --results results/runs/repeat --output analysis/replication
~~~

Confirmation has 140 runs. Eighty use the four policies at the two selected arrival rates, and 60 vary TTL in policies B and D at the reference rate. The configured warm-up and measurement windows total about 23 minutes, plus startup, drain, snapshots, and analysis. Run on an otherwise idle host, one trial at a time. If the high-rate driver saturates, retain that outcome and describe it as a test-system limit.

- `benchmarks/check_freshness.py --case race` runs the delayed-refill checks alone. The test gate requires loopback and is disabled during performance testing.
- `benchmarks/check_freshness.py --case stockout` runs the last-portion scenario alone.
- `backend/stress_freshness.py` tests six readers and two writers without an injected delay.
- `backend/check_generation.py` tests the Lua comparison above JavaScript's safe integer limit, generation survival after payload deletion, and the default-disabled gate.
- `analysis/revision.py` accepts the full set of runs and validates completed repeats. Generate paper assets only from the intended matching run set.
- The earlier `run_trial.py`, `run_local.py`, and `matrix.py` support the A/B/C fixed-user comparison. Keep those results separate from this profile.

See [PROTOCOL.md](PROTOCOL.md) for experimental definitions and [CSV_SCHEMAS.md](CSV_SCHEMAS.md) for the raw formats.
