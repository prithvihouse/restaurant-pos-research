"""Print a seeded, counterbalanced formal-run plan as CSV."""

import argparse
import csv
import json
import random
import sys
from pathlib import Path


def plan(settings, include_cold=False, profile="formal"):
    vus_levels = settings["profiles"][profile]["vus"]
    blocks = [(workload, vus) for workload in ("read", "mixed", "freshness") for vus in vus_levels]
    if include_cold:
        blocks.append(("cold", min(vus_levels)))
    randomizer = random.Random(settings["schedule_seed"])
    rotations = {block: randomizer.sample(list("ABC"), 3) for block in blocks}
    for repeat in range(1, settings["repetitions"] + 1):
        order = blocks.copy()
        randomizer.shuffle(order)
        for workload, vus in order:
            modes = rotations[(workload, vus)]
            shift = (repeat - 1) % len(modes)
            for mode in modes[shift:] + modes[:shift]:
                run_id = f"{profile}-{workload}-{vus}vu-r{repeat}-{mode}"
                yield run_id, workload, vus, repeat, mode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, default=Path(__file__).with_name("settings.json"))
    parser.add_argument("--include-cold", action="store_true", help="Add the optional cold-start block.")
    args = parser.parse_args()
    settings = json.loads(args.settings.read_text())
    writer = csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(("run_id", "workload", "vus", "repeat", "mode"))
    writer.writerows(plan(settings, args.include_cold))


if __name__ == "__main__":
    main()
