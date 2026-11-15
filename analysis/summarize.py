"""Validate completed measurements, produce run-level CSVs and manuscript figures."""

import argparse
import csv
import hashlib
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks"))
from analyze import summarize, write_csv

PAIRS = (("A", "B"), ("A", "C"), ("B", "C"))
CONTROL_KEYS = (
    "profile", "execution", "settings_sha256", "harness_sha256", "api_sha256",
    "api_package_lock_sha256", "hardware", "versions", "app_pool_max_configured",
    "cache_ttl_s", "dataset_sha256",
)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_trials(folder, recalculate=False):
    with (folder / "plan.csv").open(newline="") as stream:
        plan = list(csv.DictReader(stream))
    if not plan or len({row["run_id"] for row in plan}) != len(plan):
        raise ValueError("Empty plan or duplicate run identifiers.")
    records = []
    blocks = defaultdict(dict)
    controls = None
    for row in plan:
        trial = folder / row["run_id"]
        meta = json.loads((trial / "metadata.json").read_text())
        settings = json.loads((trial / "settings.json").read_text())
        if meta["status"] != "complete" or not meta["inventory_audit"]["valid"]:
            raise ValueError("Incomplete run or failed audit in " + row["run_id"])
        if digest(trial / "settings.json") != meta["settings_sha256"]:
            raise ValueError("Settings hash mismatch in " + row["run_id"])
        if digest(trial / "seed.sql") != meta["dataset_sha256"]:
            raise ValueError("Dataset hash mismatch in " + row["run_id"])
        result = summarize(trial, meta, settings) if recalculate else json.loads((trial / "summary.json").read_text())
        saved = json.loads((trial / "summary.json").read_text())
        if recalculate and result != saved:
            raise ValueError("Recalculation disagrees with saved summary in " + row["run_id"])
        for key in ("run_id", "workload", "vus", "repeat", "mode"):
            if str(meta[key]) != row[key] or str(result[key]) != row[key]:
                raise ValueError("Plan and measurements disagree in " + row["run_id"])
        observed_controls = [meta[key] for key in CONTROL_KEYS]
        observed_controls.extend(digest(trial / name) for name in ("pg_indexes.csv", "pg_settings.csv"))
        if controls is None:
            controls = observed_controls
        elif observed_controls != controls:
            raise ValueError("Benchmark controls changed in " + row["run_id"])
        if not result["postgres"]["valid"] or not result["redis"]["get_attribution_ok"]:
            raise ValueError("Invalid counter interval in " + row["run_id"])
        if result["warmup_failures"] or result["warmup_unfinished"]:
            raise ValueError("Warm-up failure in " + row["run_id"])
        before = json.loads((trial / "api_before.json").read_text())
        after = json.loads((trial / "api_after.json").read_text())
        delta = {key: after[key] - before[key] for key in after}
        if delta["menuDatabaseReads"] != result["postgres"]["menu_read_calls"]:
            raise ValueError("API and PostgreSQL menu counters disagree in " + row["run_id"])
        if delta["cacheHits"] != result["redis"]["hits"] or delta["cacheMisses"] != result["redis"]["misses"]:
            raise ValueError("API and Redis lookup counters disagree in " + row["run_id"])
        record = {"metadata": meta, "summary": result, "api_delta": delta}
        records.append(record)
        block = blocks[(row["workload"], int(row["vus"]), int(row["repeat"]))]
        block[row["mode"]] = record
    for key, modes in blocks.items():
        if set(modes) != {"A", "B", "C"}:
            raise ValueError(f"Incomplete matched block {key}.")
        for name in ("workload_seed", "measure_s", "warmup_s"):
            if len({entry["metadata"][name] for entry in modes.values()}) != 1:
                raise ValueError(f"Unmatched {name} in {key}.")
    return records, blocks


def metrics(record):
    result = record["summary"]
    for row in result["requests"]:
        if row["started"]:
            for name in ("mean_ms", "median_ms", "p95_ms", "http_responses_rps",
                         "successful_rps", "failure_rate", "business_rejection_rate"):
                yield row["endpoint"], name, row[name]
    for key in ("read_calls", "menu_read_calls", "locking_select_calls"):
        yield "postgres", key, result["postgres"][key]
    gets = next(row for row in result["requests"] if row["endpoint"] == "menu_item")["successful"]
    yield "postgres", "menu_reads_per_1000_gets", 1000 * result["postgres"]["menu_read_calls"] / gets if gets else None
    yield "redis", "hit_rate", result["redis"]["hit_rate"]
    yield "redis", "misses", result["redis"]["misses"]
    yield "redis", "del_calls", result["redis"]["del_calls"]
    if result["workload"] == "freshness":
        yield "freshness", "stale_rate", result["freshness"]["stale_at_ack_rate"]
        yield "freshness", "overstatement_rate", result["freshness"]["overstatement_rate"]


def produce_tables(records, blocks, output):
    output.mkdir(parents=True, exist_ok=True)
    runs = []
    groups = defaultdict(list)
    for record in records:
        meta, result = record["metadata"], record["summary"]
        for endpoint, metric, value in metrics(record):
            prefix = {key: meta[key] for key in ("run_id", "workload", "vus", "repeat", "mode")}
            runs.append({**prefix, "endpoint": endpoint, "metric": metric, "value": value})
            if value is not None:
                groups[(meta["workload"], meta["vus"], meta["mode"], endpoint, metric)].append(value)
    write_csv(output / "run_metrics.csv", tuple(runs[0]), runs)
    aggregates = []
    for key, values in sorted(groups.items()):
        aggregates.append(dict(zip(("workload", "vus", "mode", "endpoint", "metric", "n",
                                    "median", "minimum", "maximum"),
                                   (*key, len(values), statistics.median(values), min(values), max(values)))))
    write_csv(output / "aggregate_metrics.csv", tuple(aggregates[0]), aggregates)
    pairs = []
    for (workload, vus, repeat), block in sorted(blocks.items()):
        by_mode = {mode: {(endpoint, name): value for endpoint, name, value in metrics(record)}
                   for mode, record in block.items()}
        for base, candidate in PAIRS:
            for endpoint, metric in sorted(by_mode[base].keys() | by_mode[candidate].keys()):
                a, b = by_mode[base].get((endpoint, metric)), by_mode[candidate].get((endpoint, metric))
                delta = b - a if a is not None and b is not None else None
                percent = 100 * delta / a if delta is not None and a else None
                pairs.append({
                    "workload": workload, "vus": vus, "repeat": repeat, "base": base, "candidate": candidate,
                    "endpoint": endpoint, "metric": metric, "base_value": a, "candidate_value": b,
                    "candidate_minus_base": delta, "relative_change_percent": percent,
                })
    write_csv(output / "paired_changes.csv", tuple(pairs[0]), pairs)
    fresh = []
    for record in records:
        meta, result = record["metadata"], record["summary"]
        if meta["workload"] == "freshness":
            fresh.append({**{k: meta[k] for k in ("run_id", "mode", "repeat", "vus")},
                          **result["freshness"]})
    write_csv(output / "freshness_counts.csv", tuple(fresh[0]), fresh)
    save = {"trials": len(records), "inventory_audits_passed": len(records),
            "method": "median and observed minimum/maximum of trial-level metrics",
            "execution": records[0]["metadata"]["execution"],
            "hardware": records[0]["metadata"]["hardware"], "versions": records[0]["metadata"]["versions"],
            "successful_requests": sum(r["summary"]["requests"][0]["successful"] for r in records),
            "failed_or_incomplete_requests": sum(r["summary"]["requests"][0]["failure_responses"]
                                               + r["summary"]["requests"][0]["incomplete"] for r in records),
            "business_rejections": sum(r["summary"]["requests"][0]["business_rejections"] for r in records),
            "started_probes": sum(r["summary"]["freshness"]["started_probes"] for r in records),
            "eligible_probes": sum(r["summary"]["freshness"]["eligible"] for r in records),
            "unfinished_probes": sum(r["summary"]["freshness"]["unfinished_probes"] for r in records),
            "redis_errors": sum(sum(r["api_delta"][k] for k in ("cacheGetErrors", "cacheSetErrors", "invalidationErrors"))
                                for r in records)}
    (output / "validation.json").write_text(json.dumps(save, indent=2) + "\n")
    return aggregates


def latex_performance(aggregates, path):
    lookup = {(r["workload"], r["mode"], r["endpoint"], r["metric"]): r for r in aggregates}
    lines = []
    for workload, label, endpoint, name in (
        ("read", "Read-only", "menu_item", "Item GET"),
        ("mixed", "Mixed", "menu_item", "Item GET"),
        ("mixed", "Mixed", "order_create", "Order POST"),
    ):
        if lines:
            lines.append(r"\midrule")
        for mode in "ABC":
            def measure(metric, scope=endpoint):
                return lookup[workload, mode, scope, metric]

            def spread(row, decimals):
                median, low, high = (f"{row[key]:.{decimals}f}" for key in ("median", "minimum", "maximum"))
                return f"{median} ({low}--{high})"

            line = (label, mode, name, f"{measure('mean_ms')['median']:.2f}",
                    f"{measure('median_ms')['median']:.2f}", spread(measure("p95_ms"), 2),
                    spread(measure("successful_rps", "all"), 3) if endpoint == "menu_item" else "---")
            lines.append(" & ".join(line) + r" \\")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("% Generated from the run-level medians by analysis/summarize.py.\n"
                    + "\n".join(lines) + "\n\\bottomrule\n")


def plots(records, aggregates, pdf_folder, png_folder):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import PercentFormatter
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 8, "axes.spines.top": False,
                         "axes.spines.right": False, "pdf.fonttype": 42, "savefig.bbox": "tight"})
    colors = {"A": "#454b54", "B": "#247a91", "C": "#be5428"}
    pdf_folder.mkdir(exist_ok=True, parents=True)
    png_folder.mkdir(exist_ok=True, parents=True)

    def save(fig, name):
        fig.savefig(pdf_folder / (name + ".pdf"))
        fig.savefig(png_folder / (name + ".png"), dpi=180)
        plt.close(fig)

    def panel(axis, workload, endpoint, metric, label):
        for x, mode in enumerate("ABC"):
            row = next(a for a in aggregates if (a["workload"], a["mode"], a["endpoint"], a["metric"])
                       == (workload, mode, endpoint, metric))
            values = [value for record in records if record["metadata"]["mode"] == mode
                      and record["metadata"]["workload"] == workload
                      for ep, name, value in metrics(record) if ep == endpoint and name == metric]
            median = row["median"]
            axis.errorbar([x], [median], yerr=[[median - row["minimum"]], [row["maximum"] - median]],
                          color=colors[mode], capsize=4, linewidth=1.5, marker="s", markersize=4)
            axis.scatter([x - .11, x, x + .11][:len(values)], values, s=11, color=colors[mode], alpha=.8)
        axis.set_xticks([0, 1, 2], ["A", "B", "C"])
        axis.set_ylabel(label)
        axis.grid(axis="y", alpha=.18)
        axis.set_axisbelow(True)

    fig, axes = plt.subplots(2, 1, figsize=(3.35, 3.05), sharey=True, layout="constrained")
    for axis, workload, title in zip(axes, ("read", "mixed"), ("Read-only GET", "Mixed GET")):
        panel(axis, workload, "menu_item", "p95_ms", "Per-run P95 (ms)")
        axis.set_title(title)
    save(fig, "latency")
    fig, axes = plt.subplots(2, 1, figsize=(3.35, 3.05), sharey=True, layout="constrained")
    for axis, workload, title in zip(axes, ("read", "mixed"), ("Read-only", "Mixed")):
        panel(axis, workload, "all", "successful_rps", "Successful requests/s")
        axis.set_title(title)
    axes[0].set_ylim(97.25, 98.85)
    axes[0].set_yticks([97.5, 98.0, 98.5])
    save(fig, "throughput")
    fig, axes = plt.subplots(2, 1, figsize=(3.35, 3.40), layout="constrained")
    for mode, color in colors.items():
        values = [a for a in aggregates if a["mode"] == mode and a["endpoint"] == "postgres"
                  and a["metric"] == "menu_reads_per_1000_gets" and a["workload"] in ("read", "mixed")]
        values.sort(key=lambda a: ("read", "mixed").index(a["workload"]))
        offset = ("ABC".index(mode) - 1) * .22
        axes[0].bar([i + offset for i in (0, 1)], [a["median"] for a in values],
                    width=.21, color=color, label=mode)
        for i, row in enumerate(values):
            axes[0].errorbar(i + offset, row["median"],
                             yerr=[[row["median"] - row["minimum"]], [row["maximum"] - row["median"]]],
                             color="#222222", capsize=2, linewidth=1)
    axes[0].set_xticks([0, 1], ["Read-only", "Mixed"])
    axes[0].set_ylabel("Menu SELECTs / 1,000 GETs")
    axes[0].legend(frameon=False, ncols=3, loc="upper center", bbox_to_anchor=(.5, 1.25))
    for i, mode in enumerate("BC"):
        for j, workload in enumerate(("read", "mixed")):
            row = next(a for a in aggregates if (a["workload"], a["mode"], a["endpoint"], a["metric"])
                       == (workload, mode, "redis", "hit_rate"))
            x = j + (i - .5) * .3
            axes[1].bar(x, row["median"] * 100, width=.28, color=colors[mode], label=mode if j == 0 else None)
            axes[1].errorbar(x, row["median"] * 100,
                             yerr=[[(row["median"] - row["minimum"]) * 100],
                                   [(row["maximum"] - row["median"]) * 100]],
                             color="#222222", capsize=2, linewidth=1)
    axes[1].set_xticks([0, 1], ["Read-only", "Mixed"])
    axes[1].set_ylabel("Redis GET hit rate (%)")
    axes[1].set_ylim(0, 108)
    axes[1].legend(frameon=False, ncols=2, loc="upper center", bbox_to_anchor=(.5, 1.23))
    save(fig, "database_cache")
    fig, axis = plt.subplots(figsize=(3.3, 2.3), constrained_layout=True)
    panel(axis, "freshness", "freshness", "stale_rate", "Stale probe rate")
    axis.set_ylim(-.06, 1.16)
    axis.set_yticks([0, .25, .5, .75, 1])
    axis.yaxis.set_major_formatter(PercentFormatter(xmax=1, decimals=0))
    for i, mode in enumerate("ABC"):
        matching = [r["summary"]["freshness"] for r in records
                    if r["metadata"]["mode"] == mode and r["metadata"]["workload"] == "freshness"]
        axis.text(i, 1.06, str(sum(r["stale"] for r in matching)) + "/" + str(sum(r["eligible"] for r in matching)),
                  ha="center", fontsize=8)
    save(fig, "freshness")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "results/local-exploratory")
    parser.add_argument("--output", type=Path, default=ROOT / "analysis/output")
    parser.add_argument("--recalculate", action="store_true")
    parser.add_argument("--paper-assets", action="store_true", help="Regenerate the plots and LaTeX table rows")
    args = parser.parse_args()
    records, blocks = read_trials(args.input.resolve(), args.recalculate)
    aggregates = produce_tables(records, blocks, args.output.resolve())
    if args.paper_assets:
        primary = args.input.resolve() == ROOT / "results/local-exploratory"
        paper = ROOT / "analysis/legacy-paper-assets" if primary else args.output.resolve()
        png_folder = paper / "previews"
        plots(records, aggregates, paper / "figures", png_folder)
        latex_performance(aggregates, paper / "tables/performance-rows.tex")
    print(f"Validated {len(records)} completed measured trials. Read {args.output}.")


if __name__ == "__main__":
    main()
