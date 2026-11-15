"""Count older snapshots during concurrent reads and writes on one hot item."""

import argparse
import csv
import gzip
import json
import random
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))
from run_local import SETTINGS, Services, hardware, request, versions
from run_trial import ROOT, file_hash, now, save_json

READERS = 6
WRITERS = 2
SECONDS = 1.5
REPEATS = 10
ITEM = 1


def exercise(service, scope):
    latest_ack = 0
    lock = threading.Lock()
    ready = threading.Barrier(READERS + WRITERS + 1)
    deadline = None

    def drive_reader(worker):
        events = []
        ready.wait()
        while time.monotonic() < deadline:
            with lock:
                reference = latest_ack
            started_ns = time.monotonic_ns()
            status, snapshot = request(service.base, f"/menu/items/{ITEM}")
            observed = int(snapshot["stockVersion"]) if status == 200 else None
            events.append({"request": "GET", "worker": worker, "started_ns": started_ns,
                           "status": status, "reference_version": reference, "observed_version": observed,
                           "stale": observed is not None and observed < reference})
        return events

    def drive_writer(worker):
        nonlocal latest_ack
        events = []
        ready.wait()
        while time.monotonic() < deadline:
            key = uuid.uuid5(uuid.NAMESPACE_URL, f"{scope}/{worker}/{len(events)}")
            started_ns = time.monotonic_ns()
            status, receipt = request(service.base, "/orders", {"orderId": str(key), "itemId": ITEM, "quantity": 1})
            version = int(receipt["stockVersionAfter"]) if status == 201 else None
            if version is not None:
                with lock:
                    latest_ack = max(latest_ack, version)
            events.append({"request": "POST", "worker": worker, "started_ns": started_ns,
                           "status": status, "reference_version": None, "observed_version": version, "stale": False})
            time.sleep(.010)
        return events

    with ThreadPoolExecutor(max_workers=READERS + WRITERS) as pool:
        readers = [pool.submit(drive_reader, n) for n in range(READERS)]
        writers = [pool.submit(drive_writer, n) for n in range(WRITERS)]
        deadline = time.monotonic() + SECONDS
        ready.wait()
        records = sorted((event for future in (*readers, *writers) for event in future.result()),
                         key=lambda event: event["started_ns"])
    eligible = [row for row in records if row["request"] == "GET" and row["status"] == 200
                and row["reference_version"] > 0]
    return records, len(eligible), sum(row["stale"] for row in eligible)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    settings = json.loads(SETTINGS.read_text()) | {"cache_ttl_s": 3}
    meta = {"platform": hardware(), "versions": versions(), "execution": "local_process",
            "readers": READERS, "writers": WRITERS, "target_item": ITEM, "duration_s": SECONDS,
            "repeat": REPEATS, "started_at": now(), "settings": settings,
            "runner_sha256": file_hash(Path(__file__)), "backend_sha256": file_hash(ROOT / "backend/server.js")}
    save_json(output / "metadata.json", meta)
    rng = random.Random(16590)
    plan = [(repeat, mode) for repeat in range(1, REPEATS + 1) for mode in rng.sample(list("ABCD"), 4)]
    with (output / "plan.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("repeat", "mode"))
        writer.writerows(plan)
    for repeat, mode in plan:
        scope = f"hot-item-r{repeat:02d}-{mode}"
        folder = output / scope
        folder.mkdir()
        with tempfile.TemporaryDirectory(prefix="pos-hot-item-") as temporary, ExitStack() as stack:
            services = Services(stack, Path(temporary), folder, settings, mode)
            rows, eligible, stale = exercise(services, scope)
            metrics = request(services.base, "/metrics")[1]
            audit = services.audit(settings["stock_per_ingredient"])
        with gzip.open(folder / "events.csv.gz", "wt", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        if not audit["valid"] or not eligible:
            raise RuntimeError("No eligible reads or invalid audit.")
        failures = sum(row["status"] != (200 if row["request"] == "GET" else 201) for row in rows)
        save_json(folder / "summary.json", {"repeat": repeat, "mode": mode, "eligible": eligible, "stale": stale,
                                            "failures": failures, "recorded_requests": len(rows),
                                            "committed_orders": audit["committed_orders"], "api_metrics": metrics,
                                            "audit": audit, "finished_at": now()})
        print(scope, stale, eligible, failures, flush=True)
    meta["finished_at"] = now()
    save_json(output / "metadata.json", meta)


if __name__ == "__main__":
    main()
