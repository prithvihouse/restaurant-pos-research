# Artifact Archival and Replication Notes

## Reproducing Reported Summaries and Figures

This repository archives the source code, recorded trial datasets, raw measurement traces, and analysis scripts for *Performance and Consistency Trade-offs of Redis Caching in a Restaurant Point-of-Sale Backend*.

To verify the raw trial records and recalculate all summary CSVs using Python 3.11+:

~~~sh
python3 analysis/revision.py --verify-raw
python3 analysis/summarize.py --recalculate
~~~

To regenerate the manuscript tables and figures:

~~~sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r analysis/requirements.txt
python analysis/revision.py --paper-assets
cd paper
latexmk -pdf -interaction=nonstopmode -halt-on-error main.tex
~~~

## Archival and Citation Metadata

Citation metadata is provided in [CITATION.cff](../CITATION.cff) in Citation File Format (CFF 1.2.0) for GitHub and Zenodo archival integration. When citing the manuscript or software artifact, use the metadata in `CITATION.cff` and reference the corresponding release tag or DOI.

## Scope and Extensions

The archived measurements characterize a single-host Linux environment running PostgreSQL 17.11, Redis 8.0.2, Node.js 24.22.1, Express 4.21.2, and Grafana k6 2.3.0 over loopback. Future extensions across multi-node deployments can separate the k6 load generator onto a dedicated host, extend measurement windows across multiple TTL expiry cycles, and evaluate durable invalidation queues across Redis restarts.
