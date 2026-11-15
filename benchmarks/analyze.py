"""Summarize real k6 points and database/cache snapshots from one trial."""

import csv
import gzip
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


REQUEST_FIELDS = (
    "phase", "finished_at", "endpoint", "lane", "status", "outcome",
    "reason", "window", "http_ms",
)
PROBE_FIELDS = (
    "observed_at", "probe_id", "item_id", "result", "acknowledged_version",
    "observed_version", "acknowledged_units", "observed_units", "overstates_units",
    "ack_to_read_ms",
)
ENDPOINTS = ("all", "menu_item", "order_create")


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def as_time(timestamp):
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def write_csv(path, fields, rows):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def points(path):
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        for line in stream:
            event = json.loads(line)
            if event.get("type") == "Point" and event["metric"] in {
                "pos_started", "pos_http_ms", "pos_probe", "pos_probe_started"
            }:
                yield event["metric"], event["data"]


def request_counts(raw_path, request_writer=None, probe_writer=None):
    counts = defaultdict(Counter)
    latency = defaultdict(list)
    probes = Counter()
    delays = []
    first_start = last_finish = None
    for metric, point in points(raw_path):
        tags = point.get("tags", {})
        timestamp = point["time"]
        value = point["value"]
        if metric == "pos_probe_started":
            probes["started"] += int(value)
            continue
        if metric == "pos_probe":
            result = tags["result"]
            eligible = result in {"stale", "at_least_ack"}
            probes[result] += 1
            if eligible:
                delays.append(value)
            if tags.get("overstates_units") in {"true", "false"}:
                probes["quantity_comparable"] += 1
                if result == "stale" and tags["overstates_units"] == "true":
                    probes["proven_overstatement"] += 1
            if probe_writer:
                probe_writer.writerow({
                    **{key: tags.get(key, "") for key in PROBE_FIELDS},
                    "observed_at": timestamp, "ack_to_read_ms": value if eligible else "",
                })
            continue

        endpoint = tags["endpoint"]
        if metric == "pos_started":
            instant = as_time(timestamp)
            first_start = instant if first_start is None else min(first_start, instant)
            for key in ("all", endpoint):
                counts[key]["started"] += int(value)
            continue

        instant = as_time(timestamp)
        last_finish = instant if last_finish is None else max(last_finish, instant)
        outcome, window = tags["outcome"], tags["window"]
        for key in ("all", endpoint):
            counts[key]["recorded"] += 1
            counts[key][outcome] += 1
            if window == "measurement":
                counts[key]["on_time"] += 1
                if int(tags["status"]) != 0:
                    counts[key]["on_time_http"] += 1
                if outcome == "success":
                    counts[key]["on_time_success"] += 1
            if outcome == "success":
                latency[key].append(value)
        if request_writer:
            request_writer.writerow({
                **{key: tags.get(key, "") for key in REQUEST_FIELDS},
                "finished_at": timestamp, "http_ms": value,
            })
    return counts, latency, probes, delays, first_start, last_finish


def endpoint_summary(counts, latency, seconds):
    rows = []
    for endpoint in ENDPOINTS:
        tally = counts[endpoint]
        samples = sorted(latency[endpoint])
        if tally["recorded"] > tally["started"]:
            raise ValueError(f"more completions than starts for {endpoint}")
        incomplete = tally["started"] - tally["recorded"]
        failures = tally["failure"] + incomplete
        rows.append({
            "endpoint": endpoint,
            "started": tally["started"],
            "recorded": tally["recorded"],
            "completed_in_window": tally["on_time"],
            "http_responses_in_window": tally["on_time_http"],
            "completed_in_drain": tally["recorded"] - tally["on_time"],
            "successful": tally["success"],
            "business_rejections": tally["business_rejection"],
            "failure_responses": tally["failure"],
            "incomplete": incomplete,
            "failure_rate": ratio(failures, tally["started"]),
            "business_rejection_rate": ratio(tally["business_rejection"], tally["started"]),
            "completed_rps": tally["on_time"] / seconds,
            "http_responses_rps": tally["on_time_http"] / seconds,
            "successful_rps": tally["on_time_success"] / seconds,
            "mean_ms": statistics.fmean(samples) if samples else None,
            "median_ms": statistics.median(samples) if samples else None,
            "p95_ms": samples[math.ceil(0.95 * len(samples)) - 1] if samples else None,
        })
    return rows


def read_pg(path):
    with path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    info, = [row for row in rows if row["row_type"] == "info"]
    return info, {row["queryid"]: row for row in rows if row["row_type"] == "statement"}


def sql_kind(query, extra_read):
    clean = re.sub(r"\A(?:\s+|/\*.*?\*/|--[^\n]*(?:\n|$))*", "", query, flags=re.S)
    if re.match(r"SELECT\b", clean, re.I):
        lock = re.search(r"\bFOR\s+(?:NO\s+KEY\s+UPDATE|KEY\s+SHARE|UPDATE|SHARE)\b", clean, re.I)
        return "select_locking" if lock else "select"
    if extra_read and extra_read.search(query):
        return "configured_read"
    return "unclassified_cte" if re.match(r"WITH\b", clean, re.I) else "other"


def postgres_delta(folder, settings):
    before_info, before = read_pg(folder / "pg_before.csv")
    after_info, after = read_pg(folder / "pg_after.csv")
    valid = (before_info["stats_reset"] == after_info["stats_reset"]
             and before_info["dealloc"] == after_info["dealloc"] and before.keys() <= after.keys())
    menu = re.compile(settings["menu_select_regex"], re.I | re.S) if settings["menu_select_regex"] else None
    extra = re.compile(settings["extra_read_regex"], re.I | re.S) if settings["extra_read_regex"] else None
    rows = []
    for queryid, row in after.items():
        start = before.get(queryid, {})
        calls = int(row["calls"]) - int(start.get("calls", 0))
        read_rows = int(row["rows"]) - int(start.get("rows", 0))
        execution = float(row["total_exec_ms"]) - float(start.get("total_exec_ms", 0))
        valid = valid and min(calls, read_rows, execution) >= 0
        if calls:
            kind = sql_kind(row["query"], extra)
            rows.append({
                "queryid": queryid, "kind": kind, "calls": calls, "rows": read_rows,
                "total_exec_ms": execution, "query": row["query"],
                "menu_read": bool(menu and kind in {"select", "select_locking", "configured_read"}
                                  and menu.search(row["query"])),
            })
    write_csv(folder / "sql_deltas.csv",
              ("queryid", "kind", "calls", "rows", "total_exec_ms", "query", "menu_read"),
              rows if valid else [])
    kinds = Counter()
    for row in rows:
        kinds[row["kind"]] += row["calls"]
    return {
        "valid": valid,
        "read_calls": sum(kinds[key] for key in ("select", "select_locking", "configured_read")) if valid else None,
        "locking_select_calls": kinds["select_locking"] if valid else None,
        "unclassified_cte_calls": kinds["unclassified_cte"] if valid else None,
        "menu_read_calls": sum(row["calls"] for row in rows if row["menu_read"]) if valid and menu else None,
        "statement_execution_ms": sum(row["total_exec_ms"] for row in rows) if valid else None,
    }


def parse_redis(path):
    data = {}
    for line in path.read_text().splitlines():
        if not line.startswith("#") and ":" in line:
            key, _, value = line.partition(":")
            data[key] = value.strip()
    return data


def redis_delta(folder):
    before = parse_redis(folder / "redis_before.txt")
    after = parse_redis(folder / "redis_after.txt")

    def commands(snapshot, key, stat="calls"):
        counters = dict(pair.split("=", 1) for pair in snapshot.get(key, "").split(",") if "=" in pair)
        return int(counters.get(stat, 0))

    hits = int(after["keyspace_hits"]) - int(before["keyspace_hits"])
    misses = int(after["keyspace_misses"]) - int(before["keyspace_misses"])
    gets = commands(after, "cmdstat_get") - commands(before, "cmdstat_get")
    get_errors = (commands(after, "cmdstat_get", "failed_calls")
                  - commands(before, "cmdstat_get", "failed_calls"))
    deletes = commands(after, "cmdstat_del") - commands(before, "cmdstat_del")
    evictions = int(after["evicted_keys"]) - int(before["evicted_keys"])
    valid = before["run_id"] == after["run_id"] and min(hits, misses, gets, get_errors, deletes, evictions) >= 0
    attributed = valid and hits + misses == gets - get_errors
    result = {
        "valid": valid, "get_attribution_ok": attributed,
        "hits": hits if valid else None, "misses": misses if valid else None,
        "get_calls": gets if valid else None, "failed_gets": get_errors if valid else None,
        "del_calls": deletes if valid else None, "evicted_keys": evictions if valid else None,
        "db0_keys_before": int(before["db0_key_count"]), "db0_keys_after": int(after["db0_key_count"]),
        "hit_rate": ratio(hits, hits + misses) if attributed else None,
        "miss_rate": ratio(misses, hits + misses) if attributed else None,
    }
    write_csv(folder / "redis_delta.csv", tuple(result), [result])
    return result


def summarize(folder, metadata, settings):
    with gzip.open(folder / "requests.csv.gz", "wt", newline="", encoding="utf-8") as req, \
         (folder / "probes.csv").open("w", newline="", encoding="utf-8") as probe:
        req_writer = csv.DictWriter(req, fieldnames=REQUEST_FIELDS)
        probe_writer = csv.DictWriter(probe, fieldnames=PROBE_FIELDS)
        req_writer.writeheader()
        probe_writer.writeheader()
        counts, latency, probes, delays, first, last_measured = request_counts(
            folder / "measured.ndjson.gz", req_writer, probe_writer)
    gap_ms = None
    warmup = folder / "warmup.ndjson.gz"
    warmup_failures = warmup_unfinished = None
    if warmup.exists():
        warm, _, _, _, _, last = request_counts(warmup)
        warmup_unfinished = warm["all"]["started"] - warm["all"]["recorded"]
        warmup_failures = warm["all"]["failure"] + warmup_unfinished
        if first is not None and last is not None:
            gap_ms = (first - last).total_seconds() * 1000

    rows = endpoint_summary(counts, latency, metadata["measure_s"])
    prefix = {key: metadata[key] for key in ("run_id", "profile", "workload", "vus", "repeat", "mode")}
    write_csv(folder / "requests_summary.csv", tuple(prefix) + tuple(rows[0]),
              [{**prefix, **row} for row in rows])
    eligible = probes["stale"] + probes["at_least_ack"]
    total = sum(probes[key] for key in (
        "stale", "at_least_ack", "prime_failure", "order_business_rejection",
        "order_failure", "read_failure",
    ))
    result = {
        **prefix, "measure_s": metadata["measure_s"], "requests": rows,
        "warmup_to_first_measured_request_ms": gap_ms, "warmup_failures": warmup_failures,
        "warmup_unfinished": warmup_unfinished,
        "first_measured_request_at": first.isoformat() if first else None,
        "last_measured_completion_at": last_measured.isoformat() if last_measured else None,
        "postgres": postgres_delta(folder, settings), "redis": redis_delta(folder),
        "freshness": {
            "started_probes": probes["started"], "recorded_probes": total,
            "eligible": eligible, "stale": probes["stale"],
            "unfinished_probes": probes["started"] - total,
            "ineligible": total - eligible, "stale_at_ack_rate": ratio(probes["stale"], eligible),
            "quantity_comparable": probes["quantity_comparable"],
            "proven_overstatements": probes["proven_overstatement"],
            "overstatement_rate": ratio(probes["proven_overstatement"], probes["quantity_comparable"]),
            "median_ack_to_first_read_ms": statistics.median(delays) if delays else None,
        },
    }
    if "inventory_audit" in metadata:
        result["inventory_audit"] = metadata["inventory_audit"]
    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trial", type=Path)
    args = parser.parse_args()
    metadata = json.loads((args.trial / "metadata.json").read_text())
    settings = json.loads((args.trial / "settings.json").read_text())
    result = summarize(args.trial, metadata, settings)
    (args.trial / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
