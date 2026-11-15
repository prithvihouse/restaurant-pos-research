# Project Verification

Prithvi Kurmi is the sole author of this work, responsible for proposing the research question, designing the cache-policy comparisons and experiments, and verifying all code, benchmark runs, analyses, and manuscript claims. AI tools were used as a coding and editorial aid; the author reviewed and takes full responsibility for all implementation, measurements, and conclusions.

All reported metrics come from controlled benchmark trials executed on a Linux host (PostgreSQL 17.11, Redis 8.0.2, Node.js 24.22.1, Express 4.21.2, Grafana k6 2.3.0) and can be verified from the archived raw traces via `python3 analysis/revision.py --verify-raw`.
