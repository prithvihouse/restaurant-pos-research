"""Check, summarize, and plot the revised experiment from recorded observations."""

import argparse
import csv
import gzip
import hashlib
import json
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks"))
from open_load import FIELDS, REPS, SCREEN_RATES, analyze_run
from run_trial import file_hash

POLICIES = "ABCD"
COLORS = dict(zip(POLICIES, ("#434b58", "#007a92", "#b95021", "#6b5193")))
KEYS = ("overall_p95_ms", "get_mean_ms", "get_median_ms", "get_p95_ms", "post_mean_ms",
        "post_median_ms", "post_p95_ms", "delivered_pct", "goodput", "dropped_pct",
        "failure_pct", "menu_selects_per_1000_gets", "read_statements", "hit_pct",
        "driver_cpu_equivalent")


def read(path):
    return json.loads(path.read_text())


def csv_rows(path):
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream))


def write_csv(path, rows):
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def source_tree_fingerprints():
    benchmark = ROOT / "benchmarks"
    files = [path for path in sorted(benchmark.rglob("*"))
             if path.is_file() and path.suffix in {".py", ".js", ".yaml", ".sql"}]
    result = set()
    for excluded in (set(), {"check_freshness.py"}):
        digest = hashlib.sha256()
        for path in files:
            name = str(path.relative_to(benchmark))
            if name not in excluded:
                digest.update(name.encode() + b"\0" + path.read_bytes())
        result.add(digest.hexdigest())
    return result


def numeric_rows(path, recalculate):
    seen = set()
    controls = None
    allowed_hashes = source_tree_fingerprints()
    for row in csv_rows(path / "plan.csv"):
        run_id = row["run_id"]
        if run_id in seen:
            raise ValueError(f"Repeated run ID {run_id}.")
        seen.add(run_id)
        folder = path / run_id
        meta = read(folder / "metadata.json")
        summary = read(folder / "summary.json")
        if meta["status"] != "complete" or not meta["inventory_audit"]["valid"] or not summary["postgres"]["valid"]:
            raise ValueError(f"Incomplete run or invalid audit {run_id}.")
        if recalculate and meta["harness_sha256"] not in allowed_hashes:
            raise ValueError(f"Benchmark source fingerprint differs from {run_id}.")
        for field in FIELDS:
            if str(meta[field]) != row[field] or str(summary[field]) != row[field]:
                raise ValueError(f"Plan differs from {run_id} on {field}.")
        for filename, digest in meta["sources_sha256"].items():
            relative = Path(filename)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"Unexpected source path in {run_id}.")
            if file_hash(ROOT / relative) != digest:
                raise ValueError(f"Source fingerprint differs from {filename} used by {run_id}.")
        if file_hash(folder / "seed.sql") != meta["dataset_sha256"]:
            raise ValueError(f"Dataset fingerprint differs for {run_id}.")
        audit = meta["inventory_audit"]
        if audit["committed_orders"] != summary["requests"][2]["successful"]:
            raise ValueError(f"Order acknowledgments do not reconcile with committed orders in {run_id}.")
        run_controls = {key: meta[key] for key in ("hardware", "versions", "harness_sha256",
                        "allocated_vus", "dataset_sha256", "warmup_rps", "execution")}
        if controls is None:
            controls = run_controls
        if controls != run_controls:
            raise ValueError("Trial controls changed within a dataset.")
        if recalculate:
            checked = analyze_run(folder, {field: summary[field] for field in FIELDS}, read(folder / "settings.json"))
            if checked != summary:
                raise ValueError(f"Summary mismatch when replaying {run_id}.")
        yield meta, summary
    saved = {item.parent.name for item in path.glob("*/summary.json")}
    if saved != seen:
        raise ValueError(f"Unplanned summary files in {path.name}.")


def measure(summary, meta):
    overall, get, post = summary["requests"]
    return {
        "overall_p95_ms": overall["p95_ms"],
        **{f"{name}_{quantile}_ms": rows[f"{quantile}_ms"] for name, rows in (("get", get), ("post", post))
           for quantile in ("mean", "median", "p95")},
        "delivered_pct": 100 * summary["delivered_fraction"],
        "goodput": overall["successful_rps"],
        "dropped_pct": 100 * summary["dropped"] / summary["offered"],
        "failure_pct": 100 * overall["failure_rate"] if overall["failure_rate"] is not None else None,
        "menu_selects_per_1000_gets": (1000 * summary["postgres"]["menu_read_calls"] / get["started"]
                                       if get["started"] else None),
        "read_statements": summary["postgres"]["read_calls"],
        "hit_pct": 100 * summary["hit_rate"] if summary["hit_rate"] is not None else None,
        "driver_cpu_equivalent": meta["k6_cpu_s"] / meta["k6_process_elapsed_s"],
    }


def interval(values):
    if not values:
        return (None,) * 5
    rng = random.Random(170419)
    center = statistics.median(values)
    boot = sorted(statistics.median(rng.choices(values, k=len(values))) for _ in range(4000))
    return center, boot[99], boot[3899], min(values), max(values)


def correctness(path):
    manifest = csv_rows(path / "plan.csv")
    keys = [(row["case"], int(row["repeat"]), row["mode"]) for row in manifest]
    objective = {(case, repeat, mode) for case, modes in (("race", "ACD"), ("stockout", "ABCD"))
                 for repeat in range(1, 11) for mode in modes}
    if len(keys) != len(objective) or set(keys) != objective:
        raise ValueError("Expected ten induced-race repetitions in A/C/D and stockout repetitions in A/B/C/D.")
    rows = []
    customers = []
    for key in manifest:
        folder = path / f"{key['case']}-r{int(key['repeat']):02d}-{key['mode']}"
        r = read(folder / "summary.json")
        if not r["inventory_audit"]["valid"] or not all(str(r[k]) == v for k, v in key.items()):
            raise ValueError("Invalid correctness run in " + folder.name)
        if r["case"] == "race":
            stale = r["followup_version"] < r["expected_version"]
            if stale != r["followup_stale"]:
                raise ValueError(f"Could not reconcile follow-up versions in {folder.name}.")
            rows.append({**key, "eligible": 1, "event_count": int(r["followup_stale"]),
                         "fence_rejected_fills": r["api_metrics"]["fenceRejectedFills"]})
        else:
            events = r["events"]
            stale_rejections = sum(
                event["displayed_version"] < event["authoritative_version_before_browsing"]
                and event["displayed_units"] > 0 and event["authoritative_units_before_browsing"] == 0
                and event["http_status"] == 409 and event["order_code"] == "INSUFFICIENT_INVENTORY"
                for event in events
            )
            if len(events) != r["browsers"] or stale_rejections != r["false_positive_stock_rejections"]:
                raise ValueError(f"Could not reconcile browsing events in {folder.name}.")
            rows.append({**key, "eligible": r["browsers"], "event_count": r["false_positive_stock_rejections"],
                         "fence_rejected_fills": 0})
            customers.extend(events)
    return rows, customers


def hot_item(path):
    rows = []
    plan = csv_rows(path / "plan.csv")
    if len(plan) != 40 or {(int(r["repeat"]), r["mode"]) for r in plan} != {
            (repeat, mode) for repeat in range(1, 11) for mode in POLICIES}:
        raise ValueError("Expected ten hot-item stress repetitions per mode.")
    for key in plan:
        folder = path / f"hot-item-r{int(key['repeat']):02d}-{key['mode']}"
        r = read(folder / "summary.json")
        if not r["audit"]["valid"] or not all(str(r[k]) == v for k, v in key.items()):
            raise ValueError("Inventory audit or trial plan mismatch in " + folder.name)
        eligible = stale = 0
        with gzip.open(folder / "events.csv.gz", "rt", newline="") as stream:
            for event in csv.DictReader(stream):
                if event["request"] == "GET" and event["status"] == "200" and int(event["reference_version"]) > 0:
                    eligible += 1
                    stale += int(int(event["observed_version"]) < int(event["reference_version"]))
        if (eligible, stale) != (r["eligible"], r["stale"]):
            raise ValueError("Failed reconciliation in " + folder.name)
        rows.append({**key, "eligible": eligible, "event_count": stale, "failures": r["failures"],
                     "fence_rejected_fills": r["api_metrics"]["fenceRejectedFills"]})
    return rows


def summarize_open(rows):
    groups = defaultdict(list)
    trials = []
    for meta, result in rows:
        prefix = {key: result[key] for key in FIELDS}
        groups[(result["cell"], result["offered_rps"], result["cache_ttl_s"], result["mode"])].append((meta, result))
        trials.append({**prefix, **measure(result, meta), "starts": result["requests"][0]["started"],
                       "drops": result["dropped"], "schedule_difference": result["unaccounted_scheduled"],
                       "failures": result["requests"][0]["failure_responses"],
                       "business_rejections": result["requests"][0]["business_rejections"],
                       "incomplete": result["requests"][0]["incomplete"], "pass": result["operational_pass"]})
    summary = []
    for key, runs in sorted(groups.items()):
        prefix = dict(zip(("cell", "offered_rps", "cache_ttl_s", "mode"), key))
        required = 1 if prefix["cell"] == "screen" else REPS
        if sorted(r["repeat"] for _, r in runs) != list(range(1, required + 1)):
            raise ValueError(f"Incomplete or duplicate repeat for {key}.")
        for metric in KEYS:
            values = [v for m, r in runs if (v := measure(r, m)[metric]) is not None]
            all_stats = interval(values) if required > 1 else (values[0] if values else None, None, None, None, None)
            summary.append({**prefix, "metric": metric, "n": len(values),
                            "passing_trials": sum(r["operational_pass"] for _, r in runs),
                            **dict(zip(("median", "ci_low", "ci_high", "min", "max"), all_stats))})
    return trials, summary


def rates_check(screen, confirm, selected):
    screen_cells = {(run["offered_rps"], run["mode"]) for _, run in screen}
    if screen_cells != {(rate, mode) for rate in SCREEN_RATES for mode in POLICIES}:
        raise ValueError("Screen rates or modes differ from the fixed design.")
    failures = [run["offered_rps"] for _, run in screen if not run["operational_pass"]]
    if not failures or min(failures) != selected["boundary_rps"]:
        raise ValueError("Selected high rate does not follow the screening rule.")
    rates = {run["offered_rps"] for _, run in confirm if run["cell"] == "compare"}
    if rates != {selected["baseline_rps"], selected["boundary_rps"]}:
        raise ValueError("The comparison did not use the selected rates.")
    ttl_cells = {(run["offered_rps"], run["cache_ttl_s"], run["mode"])
                 for _, run in confirm if run["cell"] == "ttl"}
    expected_ttl_cells = {(selected["baseline_rps"], ttl, mode) for ttl in (1, 3, 10) for mode in "BD"}
    if ttl_cells != expected_ttl_cells:
        raise ValueError("The TTL sweep did not use the intended cells.")


def contrasts(trials):
    comparisons = defaultdict(dict)
    for r in trials:
        if r["cell"] == "compare":
            comparisons[(r["offered_rps"], r["repeat"])][r["mode"]] = r
    out = []
    for rate in sorted({key[0] for key in comparisons}):
        for before, after in ("AB", "AC", "BD", "CD"):
            for metric in KEYS:
                values = []
                for repeat in range(1, REPS + 1):
                    block = comparisons[(rate, repeat)]
                    x, y = block[before][metric], block[after][metric]
                    if x is not None and y is not None:
                        values.append(y - x)
                out.append({"offered_rps": rate, "baseline": before, "candidate": after, "metric": metric,
                            "n": len(values), **dict(zip(("median_difference", "ci_low", "ci_high", "min", "max"),
                                                        interval(values)))})
    return out


def sensitivity(trials, aggregate):
    rows = defaultdict(list)
    for trial in trials:
        if trial["cell"] != "compare" or trial["repeat"] == 1:
            continue
        for metric in ("get_p95_ms", "post_p95_ms", "delivered_pct"):
            rows[(trial["offered_rps"], trial["mode"], metric)].append(trial[metric])
    out = []
    for rate, mode, metric in sorted(rows):
        sample = rows[(rate, mode, metric)]
        complete = next(x for x in aggregate if (
            x["cell"], x["offered_rps"], x["mode"], x["metric"]) == ("compare", rate, mode, metric))
        out.append({"offered_rps": rate, "mode": mode, "metric": metric, "n": len(sample),
                    "all_repeats_median": complete["median"], "excluding_first_repeat_median": statistics.median(sample)})
    return out


def format_metric(rows, selector, precision=2):
    row = next(x for x in rows if all(x[k] == v for k, v in selector.items()))
    if row["n"] == 0:
        return "---"
    return f"{row['median']:.{precision}f}"


def make_tables(output, summary, checks, stress, paired):
    destination = output / "tables"
    destination.mkdir(exist_ok=True)
    rates = sorted({r["offered_rps"] for r in summary if r["cell"] == "compare"})
    performance = []
    for index, rate in enumerate(rates):
        if index:
            performance.append(r"\midrule")
        for mode in POLICIES:
            def value(metric, digits=2):
                return format_metric(summary, dict(cell="compare", offered_rps=rate, mode=mode, metric=metric), digits)
            passes = next(r["passing_trials"] for r in summary if r["cell"] == "compare"
                          and r["offered_rps"] == rate and r["mode"] == mode)
            line = [str(rate), mode, value("get_p95_ms"), value("post_p95_ms"), value("goodput", 1),
                    value("dropped_pct"), value("menu_selects_per_1000_gets", 1), f"{passes}/10"]
            performance.append(" & ".join(line) + r" \\")
    (destination / "performance-rows.tex").write_text("\n".join(performance) + "\n\\bottomrule\n")
    display = []
    for mode in POLICIES:
        totals = []
        for rows in ([x for x in checks if x["case"] == "race"],
                     [x for x in checks if x["case"] == "stockout"], stress):
            group = [r for r in rows if r["mode"] == mode]
            totals.append((sum(r["event_count"] for r in group), sum(r["eligible"] for r in group)))
        text = [f"{n:,}/{denom:,}" if denom else "---" for n, denom in totals]
        display.append(" & ".join([mode, *text]) + r" \\")
    (destination / "correctness-rows.tex").write_text("\n".join(display) + "\n\\bottomrule\n")

    high = max(rates)
    lines = []
    for before, after in ("AB", "CD"):
        cells = [f"{after} minus {before}"]
        for metric in ("get_p95_ms", "post_p95_ms", "delivered_pct"):
            row = next(r for r in paired if (r["offered_rps"], r["baseline"], r["candidate"], r["metric"])
                       == (high, before, after, metric))
            cells.append(f"{row['median_difference']:+.2f} ({row['ci_low']:+.2f}, {row['ci_high']:+.2f})")
        lines.append(" & ".join(cells) + r" \\")
    (destination / "paired-rows.tex").write_text("\n".join(lines) + "\n\\bottomrule\n")


def figures(root, summary):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 8, "axes.spines.top": False,
                         "axes.spines.right": False, "pdf.fonttype": 42})
    pdf = root / "paper/figures"
    png = root / "analysis/figures"
    pdf.mkdir(parents=True, exist_ok=True)
    png.mkdir(parents=True, exist_ok=True)

    def write(fig, name):
        fig.savefig(pdf / (name + ".pdf"), bbox_inches="tight")
        fig.savefig(png / (name + ".png"), bbox_inches="tight", dpi=190)
        plt.close(fig)

    fig, ax = plt.subplots(figsize=(3.30, 2.15), layout="constrained")
    for mode in POLICIES:
        rows = [r for r in summary if r["cell"] == "screen" and r["mode"] == mode and r["metric"] == "goodput"]
        ax.plot([r["offered_rps"] for r in rows], [r["median"] for r in rows], color=COLORS[mode],
                label=mode, marker=dict(zip(POLICIES, "os^D"))[mode], markersize=3.7, linewidth=1.15)
    ax.plot([0, max(SCREEN_RATES)], [0, max(SCREEN_RATES)], linestyle=":", color="#999999", label="Target")
    ax.set(xlabel="Offered requests/s", ylabel="Successful requests/s", ylim=(0, 8200))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f"{x:,.0f}"))
    ax.xaxis.set_major_formatter(FuncFormatter(lambda x, _: f"{x:,.0f}"))
    ax.legend(loc="upper left", ncols=2, frameon=False, fontsize=7)
    ax.grid(axis="y", alpha=.2)
    write(fig, "open_screen")

    def panel(axis, rows, category, levels, modes, metric, y_label):
        for index, mode in enumerate(modes):
            values = [next(r for r in rows if r[category] == level and r["mode"] == mode and r["metric"] == metric)
                      for level in levels]
            x = [i + .17 * (index - (len(modes) - 1) / 2) for i in range(len(levels))]
            y = [row["median"] for row in values]
            errors = [[max(0, row["median"] - row["ci_low"]) for row in values],
                      [max(0, row["ci_high"] - row["median"]) for row in values]]
            axis.errorbar(x, y, yerr=errors, fmt=dict(zip(POLICIES, "os^D"))[mode], label=mode,
                          color=COLORS[mode], capsize=3, markersize=4)
        axis.set_ylabel(y_label)
        axis.grid(axis="y", alpha=.20)
        axis.set_xticks(range(len(levels)), [f"{level:,}" for level in levels])
        axis.set_xlim(-.44, len(levels) - .56)

    confirm = [r for r in summary if r["cell"] == "compare"]
    rates = sorted({r["offered_rps"] for r in confirm})
    fig, axes = plt.subplots(2, figsize=(3.30, 3.32), layout="constrained", sharex=True)
    panel(axes[0], confirm, "offered_rps", rates, POLICIES, "get_p95_ms", "Item GET P95 (ms)")
    panel(axes[1], confirm, "offered_rps", rates, POLICIES, "post_p95_ms", "Order POST P95 (ms)")
    axes[0].legend(loc="lower center", bbox_to_anchor=(.5, 1.02), ncols=4, frameon=False, fontsize=8)
    axes[1].set_xlabel("Offered requests/s")
    write(fig, "open_latency")

    ttl = [r for r in summary if r["cell"] == "ttl"]
    levels = [1, 3, 10]
    fig, axes = plt.subplots(2, figsize=(3.30, 3.20), layout="constrained", sharex=True)
    panel(axes[0], ttl, "cache_ttl_s", levels, "BD", "hit_pct", "Redis lookup hit rate (%)")
    panel(axes[1], ttl, "cache_ttl_s", levels, "BD", "get_p95_ms", "Item GET P95 (ms)")
    axes[0].legend(loc="lower center", bbox_to_anchor=(.5, 1.02), ncols=2, frameon=False, fontsize=8)
    axes[1].set_xlabel("Cache TTL (s)")
    write(fig, "ttl_sweep")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=ROOT / "results")
    parser.add_argument("--output", type=Path, default=ROOT / "analysis/revision-output")
    parser.add_argument("--verify-raw", action="store_true")
    parser.add_argument("--paper-assets", action="store_true")
    args = parser.parse_args()
    result = args.results.resolve()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    screen = list(numeric_rows(result / "revision-screen", args.verify_raw))
    confirm = list(numeric_rows(result / "revision-confirm", args.verify_raw))
    if len(screen) != 24 or len(confirm) != 140:
        raise ValueError("Expected 24 screening trials and 140 repeated open-loop trials.")
    selection = result / "revision-screen/selected-rates.json"
    if selection.is_file():
        rates_check(screen, confirm, read(selection))
    checks, customers = correctness(result / "revision-correctness")
    stress = hot_item(result / "revision-stress")
    trials, summary = summarize_open([*screen, *confirm])
    write_csv(out / "open_trials.csv", trials)
    write_csv(out / "open_aggregates.csv", summary)
    paired = contrasts(trials)
    write_csv(out / "paired_differences.csv", paired)
    write_csv(out / "first_block_sensitivity.csv", sensitivity(trials, summary))
    write_csv(out / "correctness_trials.csv", checks)
    write_csv(out / "stockout_events.csv", customers)
    write_csv(out / "hot_item_trials.csv", stress)
    if args.paper_assets:
        make_tables(ROOT / "paper", summary, checks, stress, paired)
        figures(ROOT, summary)
    for name, rows in (("screen", screen), ("repeat", confirm), ("correctness", checks), ("stress", stress)):
        print(f"{name}: {len(rows)} trials validated")


if __name__ == "__main__":
    main()
