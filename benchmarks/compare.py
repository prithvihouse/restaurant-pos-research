"""Compare actual formal trials as paired, run-level observations."""

import argparse
import csv
import hashlib
import json
import statistics
from collections import defaultdict
from pathlib import Path

from analyze import ratio, write_csv


PAIRS = (("A", "B"), ("A", "C"), ("B", "C"))
CONTROLS = (
    "settings_sha256", "compose_sha256", "harness_sha256", "dataset_sha256",
    "hardware", "docker_resources", "docker_versions", "compose_version",
    "images", "app_pool_max_configured", "cpu_sampling", "cache_ttl_s",
    "api_package_versions", "api_package_lock_sha256",
)


def observations(result):
    """Return metric names, values, and units used for absolute differences."""
    if result["workload"] != "freshness":
        for row in result["requests"]:
            if row["started"] == 0:
                continue
            endpoint = row["endpoint"]
            for name in ("mean_ms", "median_ms", "p95_ms", "http_responses_rps", "successful_rps"):
                yield endpoint, name, "ms" if name.endswith("_ms") else "req/s", row[name]
            for name in ("failure_rate", "business_rejection_rate"):
                rate = row[name]
                yield endpoint, name, "pp", None if rate is None else rate * 100

    pg, redis = result["postgres"], result["redis"]
    for name in ("read_calls", "locking_select_calls", "menu_read_calls"):
        yield "postgres", name, "calls", pg[name]
    menu_successes = next(row["successful"] for row in result["requests"] if row["endpoint"] == "menu_item")
    if result["workload"] != "freshness":
        per_thousand = ratio(1000 * pg["menu_read_calls"], menu_successes) if pg["menu_read_calls"] is not None else None
        yield "postgres", "menu_reads_per_1000_successful_gets", "calls/1000 GETs", per_thousand
    for name in ("hits", "misses", "get_calls", "del_calls"):
        yield "redis", name, "calls", redis[name]
    for name in ("hit_rate", "miss_rate"):
        rate = redis[name]
        yield "redis", name, "pp", None if rate is None else rate * 100
    if result["workload"] == "freshness":
        freshness = result["freshness"]
        for name in ("stale_at_ack_rate", "overstatement_rate"):
            rate = freshness[name]
            yield "freshness", name, "pp", None if rate is None else rate * 100


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_planned(plan, results):
    with plan.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    if not rows:
        raise ValueError("The plan has no trials.")
    if len({row["run_id"] for row in rows}) != len(rows):
        raise ValueError("The plan repeats a run ID.")
    blocks = defaultdict(dict)
    common = None
    for row in rows:
        folder = results / row["run_id"]
        meta = json.loads((folder / "metadata.json").read_text())
        result = json.loads((folder / "summary.json").read_text())
        for key in ("run_id", "workload", "vus", "repeat", "mode"):
            if str(meta[key]) != row[key] or str(result[key]) != row[key]:
                raise ValueError(f"Plan and trial disagree about {key} in {row['run_id']}.")
        if meta["profile"] != "formal" or meta["status"] != "complete":
            raise ValueError(f"Formal trial incomplete: {row['run_id']}.")
        if not meta["inventory_audit"]["valid"]:
            raise ValueError(f"Inventory audit failed in {row['run_id']}. Inspect the saved audit.")
        controls = [meta[key] for key in CONTROLS]
        controls.extend(digest(folder / name) for name in ("pg_indexes.csv", "pg_settings.csv"))
        if common is None:
            common = controls
        elif controls != common:
            raise ValueError(f"Control metadata differ in {row['run_id']}. Compare like-for-like trials.")
        block = blocks[(row["workload"], int(row["vus"]), int(row["repeat"]))]
        if row["mode"] in block:
            raise ValueError(f"Duplicate mode within the block for {row['run_id']}.")
        block[row["mode"]] = (meta, result)
    return blocks


def compare(blocks):
    details = []
    groups = defaultdict(list)
    for (workload, vus, repeat), modes in sorted(blocks.items()):
        if set(modes) != {"A", "B", "C"}:
            raise ValueError(f"Incomplete A/B/C block for {workload}, {vus} VUs, repetition {repeat}.")
        for field in ("workload_seed", "measure_s", "warmup_s"):
            if len({meta[field] for meta, _ in modes.values()}) != 1:
                raise ValueError(f"Unmatched {field} in {workload}, {vus} VUs, repetition {repeat}.")

        observations_by_mode = {
            mode: {(endpoint, name, unit): value for endpoint, name, unit, value in observations(result)}
            for mode, (_, result) in modes.items()
        }
        for base, candidate in PAIRS:
            left, right = observations_by_mode[base], observations_by_mode[candidate]
            for endpoint, metric, unit in sorted(left.keys() | right.keys()):
                before, after = left.get((endpoint, metric, unit)), right.get((endpoint, metric, unit))
                delta = after - before if before is not None and after is not None else None
                relative = ratio(100 * delta, before) if delta is not None and unit != "pp" else None
                details.append((workload, vus, repeat, base, candidate, endpoint, metric, unit,
                                before, after, delta, relative))
                groups[(workload, vus, base, candidate, endpoint, metric, unit)].append(delta)
    medians = []
    for key, effects in sorted(groups.items()):
        values = [value for value in effects if value is not None]
        medians.append((*key, len(values), len(effects),
                        statistics.median(values) if values else None,
                        min(values) if values else None, max(values) if values else None))
    return details, medians


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--results", type=Path, default=Path("results/runs"))
    args = parser.parse_args()
    try:
        details, medians = compare(load_planned(args.plan, args.results))
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(1, f"Comparison stopped. {exc}\n")

    common = ("base", "candidate", "endpoint", "metric", "difference_unit")
    outputs = [
        ("pairwise.csv", ("workload", "vus", "repeat", *common, "base_value",
                          "candidate_value", "difference", "relative_percent"), details),
        ("paired_summary.csv", ("workload", "vus", *common, "usable_pairs", "planned_pairs",
                                "median_difference", "min_difference", "max_difference"), medians),
    ]
    for name, fields, rows in outputs:
        write_csv(args.results / name, fields, [dict(zip(fields, row)) for row in rows])


if __name__ == "__main__":
    main()
