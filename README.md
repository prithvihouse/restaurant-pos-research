# Redis Caching in a Restaurant POS Backend

**Preprint:** DOI 10.5281/zenodo.24367420 · **Code & Data Artifact:** DOI 10.5281/zenodo.24239951 — Prithvi Kurmi, Georgia State University.

This project evaluates four ways to serve item availability in an Express point-of-sale API. PostgreSQL owns inventory and order transactions. Redis can cache item displays, which include an ingredient stock version and available quantity.

| Mode | Item read | After a committed order |
| --- | --- | --- |
| A | PostgreSQL | PostgreSQL remains authoritative |
| B | Redis cache-aside and fixed expiry | Expiry controls payload lifetime |
| C | Same as B | Attempt deletion of affected payloads |
| D | Redis cache-aside and fixed expiry with generation-checked refills | Attempt atomic generation updates and deletion of affected payloads |

The revised study uses an induced delayed-refill interleaving, a concurrent hot-item stress test, a controlled last-portion scenario, and fixed-arrival-rate performance trials. The Docker Compose configuration is for replication. The included measurements were run on Linux with local processes. The request sequences and menu dataset reflect actual trial data. The timings, cache outcomes, and database counters are recorded measurements.

Plain deletion returned stale stock on all ten forced follow-up reads. The generation-checked policy returned fresh stock in all ten. In ten low-stock rounds, TTL-only caching prompted 80 rejected checkout attempts after a last-portion sale. The open-loop comparison includes ten repeats per mode at both 600 and 4,800 offered requests/s and a 1/3/10-second TTL sweep. See the paper for the denominator, confidence intervals, and limits of each comparison.

## Read the project

| Path | Contents |
| --- | --- |
| [paper/main.pdf](paper/main.pdf) | Compiled manuscript, including a contribution and measurement provenance statement |
| [paper/](paper/) | IEEEtran source, numbered references, figures, and verification checklist |
| [backend/](backend/) | Express API, PostgreSQL inventory transactions, Redis Lua scripts, and health and metrics routes |
| [database/](database/) | Schema, application role, and recorded trial dataset |
| [benchmarks/](benchmarks/) | k6 workloads, runner, settings, and revised protocol |
| [results/](results/) | Measured trials, raw observations, control records, and prior exploratory material |
| [analysis/](analysis/) | Python aggregation, interval estimates, plots, and results in CSV |
| [paper/RELEASE.md](paper/RELEASE.md) | Artifact archival, citation, and replication notes |
| [CITATION.cff](CITATION.cff) | Citation metadata for the preprint and artifact |

## Recreate results from recorded data

Use Python 3.11 or newer from this directory. Reanalysis uses the standard library and does not require Docker.

~~~sh
python3 analysis/revision.py --verify-raw
~~~

To regenerate the paper's figures and tables, install the plotting dependency in a virtual environment.

~~~sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r analysis/requirements.txt
python analysis/revision.py --paper-assets
cd paper
latexmk -pdf -interaction=nonstopmode -halt-on-error main.tex
~~~

The TeX installation needs the IEEEtran class and BibTeX. The paper PDF, figures, and generated table rows are included before rebuilding.

## Reproduce live runs on Linux

Use an unprivileged Linux account with PostgreSQL and its `pg_stat_statements` extension, Node.js 22+, Redis, Python 3.11+, and Grafana k6 on PATH. The runner starts temporary services on loopback and audits final inventory. Put the PostgreSQL binary directory on PATH when your distribution keeps `initdb` outside its default search path.

~~~sh
npm ci --prefix backend
python3 benchmarks/open_load.py smoke --output results/runs/r2-smoke
~~~

See [the benchmark instructions](benchmarks/README.md) for the screen, repeat jobs, race replay, stockout test, and hot-item stress test. A lab Linux machine or Linux VM can run this workflow without Docker Desktop on your Mac. Compare new machine results separately.

## Run the API with Docker Desktop

When Docker Desktop is available, create your local configuration and start the services from the project root. Set the two passwords in `.env` to local development values and set `CACHE_MODE` to one of `postgres`, `ttl`, `ttl-invalidate`, or `ttl-fenced`. The API binds to loopback on the host through Compose.

~~~sh
cp .env.example .env
# Set your own local values in .env.
docker compose config --quiet
docker compose up -d --wait postgres redis
docker compose exec -T postgres psql -X -q -v ON_ERROR_STOP=1 -U pos_admin -d pos < database/seed.sql
docker compose up -d --build --wait api
curl -fsS http://127.0.0.1:3000/healthz
curl -fsS http://127.0.0.1:3000/menu/items/1
~~~

Seed once per fresh demo database. Restart the API after changing cache mode, and start fresh cache and database state when comparing policies. The local correctness runner alone enables the loopback test gate. The Docker profile sets up the API and retains the earlier A/B/C fixed-user scripts. Use the Linux runner for the revised four-mode empirical protocol.

| Endpoint | Returns |
| --- | --- |
| `GET /menu` | Menu items, names, and prices |
| `GET /menu/items/:id` | Sellable units, availability flag, and stock version |
| `POST /orders` | Receipt for a valid order, or a business/infrastructure response |
| `GET /healthz` | Active policy, TTL, and connection pool size |
| `GET /metrics` | Application SQL, cache, and invalidation counters |
