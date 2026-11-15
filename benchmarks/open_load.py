"""Collect matched open-loop benchmark runs from disposable local services."""

import argparse
import csv
import gzip
import json
import os
import random
import resource
import shutil
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path

from analyze import endpoint_summary, postgres_delta, request_counts
from run_local import BINARIES, HERE, ROOT, SETTINGS, Services, hardware, versions
from run_trial import MODES, checked, file_hash, harness_hash, now, save_json

REPS = 10
BASE_TTL = 3
SCREEN_RATES = (200, 600, 1200, 2400, 4800, 8000)
OPEN_VUS = 128
WARM_S = 2
WARM_RATE = 300
MEASURE_S = 5
TTL_WARM_S = 2
TTL_MEASURE_S = 12
METRICS = {b"pos_started", b"pos_http_ms", b"dropped_iterations", b"vus", b"vus_max"}
FIELDS = ("run_id", "cell", "repeat", "mode", "cache_ttl_s", "offered_rps", "measure_s", "warmup_s", "workload_seed")


def schedule(stage, output, low=None, high=None):
    if stage == "screen":
        groups = [("screen", 1, rate, "ABCD", BASE_TTL, 3, WARM_S) for rate in SCREEN_RATES]
    else:
        groups = []
        rng = random.Random(92316)
        for repeat in range(1, REPS + 1):
            jobs = [
                ("compare", repeat, low, "ABCD", BASE_TTL, MEASURE_S, WARM_S),
                ("compare", repeat, high, "ABCD", BASE_TTL, MEASURE_S, WARM_S),
                *[("ttl", repeat, low, "BD", ttl, TTL_MEASURE_S, TTL_WARM_S) for ttl in (1, 3, 10)],
            ]
            # Interleave short and long windows across repetitions and rotate policy order inside cells.
            rng.shuffle(jobs)
            groups.extend(jobs)
    with (output / "plan.csv").open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDS)
        writer.writeheader()
        for group, (cell, repeat, rate, modes, ttl, seconds, warm) in enumerate(groups):
            offset = (repeat - 1 + rate // 200 + ttl) % len(modes)
            order = modes[offset:] + modes[:offset]
            if stage == "screen":
                shift = group % len(order)
                order = order[shift:] + order[:shift]
            for mode in order:
                row = {"run_id": f"{cell}-{rate}rps-ttl{ttl}-r{repeat:02d}-{mode}", "cell": cell, "repeat": repeat,
                       "mode": mode, "cache_ttl_s": ttl, "offered_rps": rate, "measure_s": seconds,
                       "warmup_s": warm, "workload_seed": 6212026 + repeat}
                writer.writerow(row)
                yield row


def retained_points(source, target):
    with source.open("rb") as raw, gzip.open(target, "wb", compresslevel=2) as output:
        for line in raw:
            if any(b'"metric":"' + name + b'"' in line for name in METRICS):
                output.write(line)


def phase(services, workspace, folder, run, name, *, rate, duration):
    warm = name == "warmup"
    raw = workspace / (name + ".ndjson")
    env = {
        **os.environ, "POS_BASE_URL": services.base, "POS_SETTINGS": str(folder / "settings.json"),
        "POS_TRIAL_DATA": str(folder / "trial_data.json"), "POS_VUS": str(OPEN_VUS),
        "POS_PHASE": name, "POS_WORKLOAD": "read" if warm else "mixed",
        "POS_WORKLOAD_SEED": str(run["workload_seed"]), "POS_DURATION_S": str(duration),
        "POS_RATE": str(rate), "POS_OPEN_VUS": str(OPEN_VUS), "K6_NO_USAGE_REPORT": "true",
    }
    initial = resource.getrusage(resource.RUSAGE_CHILDREN)
    began = time.monotonic()
    checked(["k6", "run", "--quiet", "--summary-mode=disabled", "--out", f"json={raw}",
             str(HERE / "k6/open.js")], f"k6 {name}", env=env, log=folder / (name + ".log"),
            timeout=duration + 15)
    elapsed = time.monotonic() - began
    final = resource.getrusage(resource.RUSAGE_CHILDREN)
    retained_points(raw, folder / (name + ".ndjson.gz"))
    return {"k6_cpu_s": final.ru_utime + final.ru_stime - initial.ru_utime - initial.ru_stime,
            "k6_process_elapsed_s": elapsed}


def dropped_points(path):
    with gzip.open(path, "rt") as file:
        rows = (json.loads(line) for line in file if '"metric":"dropped_iterations"' in line)
        return sum(int(row["data"]["value"]) for row in rows if row["type"] == "Point")


def analyze_run(folder, run, settings):
    path = folder / "measured.ndjson.gz"
    counts, latency, _, _, first, _ = request_counts(path)
    requests = endpoint_summary(counts, latency, run["measure_s"])
    all_requests = requests[0]
    offered = run["measure_s"] * run["offered_rps"]
    dropped = dropped_points(path)
    absent = offered - all_requests["started"] - dropped
    delivered = all_requests["successful_rps"] / run["offered_rps"]
    postgres = postgres_delta(folder, settings)
    snapshots = [json.loads((folder / f"api_{phase}.json").read_text()) for phase in ("before", "after")]
    api = {key: snapshots[1][key] - value for key, value in snapshots[0].items()}
    lookups = api["cacheHits"] + api["cacheMisses"]
    if not postgres["valid"] or postgres["menu_read_calls"] != api["menuDatabaseReads"]:
        raise RuntimeError("Could not reconcile PostgreSQL statement counts with API calls.")
    warm = request_counts(folder / "warmup.ndjson.gz")
    gap = (first - warm[-1]).total_seconds() if first and warm[-1] else None
    return {
        **run, "requests": requests, "offered": offered, "dropped": dropped, "unaccounted_scheduled": absent,
        "delivered_fraction": delivered, "operational_pass": bool(all_requests["p95_ms"] is not None
            and delivered >= .95 and all_requests["p95_ms"] <= 50),
        "postgres": postgres, "api_delta": api, "hit_rate": api["cacheHits"] / lookups if lookups else None,
        "warm_to_first_request_s": gap,
    }


def run_trial(output, row, settings, environment):
    folder = output / row["run_id"]
    folder.mkdir()
    controls = {**settings, "cache_ttl_s": row["cache_ttl_s"]}
    save_json(folder / "settings.json", controls)
    meta = {**row, **environment, "execution": "local_process", "started_at": now(),
            "status": "running", "warmup_rps": WARM_RATE, "allocated_vus": OPEN_VUS}
    save_json(folder / "metadata.json", meta)
    try:
        with tempfile.TemporaryDirectory(prefix="pos-open-") as temporary, ExitStack() as stack:
            workspace = Path(temporary)
            services = Services(stack, workspace, folder, controls, row["mode"])
            phase(services, workspace, folder, row, "warmup", rate=WARM_RATE, duration=row["warmup_s"])
            services.snapshot("before")
            meta["measured_command_started_at"] = now()
            meta.update(phase(services, workspace, folder, row, "measured", rate=row["offered_rps"],
                              duration=row["measure_s"]))
            meta["measured_command_finished_at"] = now()
            services.snapshot("after")
            meta["inventory_audit"] = services.audit(settings["stock_per_ingredient"])
            meta["dataset_sha256"] = services.trial_data["sql_sha256"]
        summary = analyze_run(folder, row, controls)
        if not meta["inventory_audit"]["valid"] or not summary["requests"][0]["started"]:
            raise RuntimeError("Failed inventory audit or no measured starts.")
        save_json(folder / "summary.json", summary)
        meta["status"] = "complete"
        print(row["run_id"], f"delivered={summary['delivered_fraction']:.3f}",
              f"p95_ms={summary['requests'][0]['p95_ms']}", f"drops={summary['dropped']}", flush=True)
    except (Exception, KeyboardInterrupt) as exc:
        meta.update(status="failed", error=str(exc) or type(exc).__name__)
        raise
    finally:
        meta["finished_at"] = now()
        save_json(folder / "metadata.json", meta)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("screen", "confirm", "smoke"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--low", type=int)
    parser.add_argument("--high", type=int)
    args = parser.parse_args()
    if args.stage == "confirm" and (
            args.low not in SCREEN_RATES or args.high not in SCREEN_RATES or not args.low < args.high):
        parser.error("Choose two distinct screening rates in ascending order for --low and --high.")
    if missing := [name for name in BINARIES if not shutil.which(name)]:
        parser.error("Missing programs: " + ", ".join(missing))
    if os.getuid() == 0:
        parser.error("Run as an unprivileged user.")
    settings = json.loads(SETTINGS.read_text())
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    write_controls = {"hardware": hardware(), "versions": versions(), "harness_sha256": harness_hash(),
                      "sources_sha256": {str(path.relative_to(ROOT)): file_hash(path) for path in (
                          ROOT / "backend/server.js", ROOT / "backend/refill_gate.js",
                          ROOT / "backend/lua/fill.lua", ROOT / "backend/lua/invalidate.lua",
                          ROOT / "backend/package-lock.json", ROOT / "database/init/001-schema.sql",
                      )}}
    if args.stage == "smoke":
        row = {"run_id": "smoke-200rps-D", "cell": "smoke", "repeat": 1, "mode": "D",
               "cache_ttl_s": BASE_TTL, "offered_rps": 200, "measure_s": 3, "warmup_s": WARM_S,
               "workload_seed": settings["workload_seed"]}
        run_trial(output, row, settings, write_controls)
        return
    rows = list(schedule(args.stage, output, args.low, args.high))
    for row in rows:
        run_trial(output, row, settings, write_controls)


if __name__ == "__main__":
    main()
