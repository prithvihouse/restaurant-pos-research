"""Replay a stale refill and count stockouts following stale availability displays."""

import argparse
import csv
import hashlib
import json
import random
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path

from run_local import SETTINGS, Services, hardware, request, versions
from run_trial import ROOT, file_hash, now, save_json

CASES = (("race", "ACD"), ("stockout", "ABCD"))
REPEATS = 10
INITIAL_INGREDIENT_STOCK = 4
ITEM_FOR_ORDER = 1
ITEM_FOR_READ = 2
BROWSERS = 8


def order(base, item, scope):
    payload = {"orderId": str(uuid.uuid5(uuid.NAMESPACE_URL, scope)), "itemId": item, "quantity": 1}
    return request(base, "/orders", payload)


def item(base):
    status, body = request(base, f"/menu/items/{ITEM_FOR_READ}")
    if status != 200:
        raise RuntimeError(f"Unexpected GET status {status}.")
    return body


def drain(base, scope, step):
    status, receipt = order(base, ITEM_FOR_ORDER, f"{scope}/competing-order/{step}")
    if status != 201 or receipt["sellableUnitsAfter"] != (INITIAL_INGREDIENT_STOCK // 2 - step):
        raise RuntimeError("Dataset or deducting order failed the expected stock transition.")
    return receipt


def lookup_snapshot(service):
    value = service.sql(f"""
        COPY (SELECT SUM(i.revision), MIN(i.on_hand / r.units_per_item)
        FROM recipe_ingredients r JOIN ingredients i ON r.ingredient_id = i.id
        WHERE r.menu_item_id = {ITEM_FOR_READ}) TO STDOUT WITH (FORMAT csv);""").strip()
    version, units = value.split(",")
    return {"version": int(version), "units": int(units)}


def forced_refill(service, run_id):
    base = service.base
    drain(base, run_id, 1)
    # Force a miss, then release this one request only after the next write acknowledges.
    service.redis("DEL", f"pos:menu:{ITEM_FOR_READ}")
    gate_path = "/__test/refill-gate"
    if request(base, gate_path + "/arm", {"itemId": ITEM_FOR_READ}) != (200, {"state": "armed"}):
        raise RuntimeError("Could not arm the snapshot gate.")
    with ThreadPoolExecutor(max_workers=1) as pool:
        in_flight = pool.submit(item, base)
        deadline = time.monotonic() + 1.5
        while request(base, gate_path)[1]["state"] != "blocked":
            if time.monotonic() >= deadline or in_flight.done():
                raise RuntimeError("The in-flight GET did not reach the gate.")
            time.sleep(.005)
        drain(base, run_id, 2)
        if request(base, gate_path + "/release", {}) != (200, {"state": "released"}):
            raise RuntimeError("Invalid gate release.")
        overlap = in_flight.result()
    committed = lookup_snapshot(service)
    fresh = item(base)
    overlap_version = int(overlap["stockVersion"])
    last_version = int(fresh["stockVersion"])
    if overlap["sellableUnits"] != 1 or committed["units"] != 0 or not overlap_version < committed["version"]:
        raise RuntimeError("The interleaving did not capture the intended obsolete SQL snapshot.")
    return {
        "expected_version": committed["version"], "expected_units": committed["units"],
        "overlap_version": overlap_version, "overlap_units": overlap["sellableUnits"],
        "followup_version": last_version, "followup_units": fresh["sellableUnits"],
        "followup_stale": last_version < committed["version"],
        "false_positive_display": fresh["sellableUnits"] > committed["units"],
        "api_metrics": request(base, "/metrics")[1],
    }


def stockout(service, run_id, mode):
    base = service.base
    drain(base, run_id, 1)
    warm = item(base)
    if warm["sellableUnits"] != 1:
        raise RuntimeError("Could not prime the one-portion browsing snapshot.")
    drain(base, run_id, 2)
    committed = lookup_snapshot(service)
    if committed["units"] != 0:
        raise RuntimeError("Inventory remained after the competing last-unit sale.")

    def customer(index):
        displayed = item(base)
        version = int(displayed["stockVersion"])
        shown = displayed["sellableUnits"]
        status, result = order(base, ITEM_FOR_READ, f"{run_id}/customer/{index}") if shown else (None, {})
        return {"run_id": run_id, "mode": mode, "customer": index, "displayed_version": version,
                "displayed_units": shown, "authoritative_version_before_browsing": committed["version"],
                "authoritative_units_before_browsing": committed["units"], "http_status": status,
                "order_code": result.get("code"), "false_positive_stock_rejection":
                    version < committed["version"] and shown > 0 and status == 409
                    and result.get("code") == "INSUFFICIENT_INVENTORY"}

    with ThreadPoolExecutor(max_workers=BROWSERS) as pool:
        events = list(pool.map(customer, range(1, BROWSERS + 1)))
    if lookup_snapshot(service) != committed:
        raise RuntimeError("An unexpected follow-up order modified sold-out stock.")
    if any(row["http_status"] is not None and row["order_code"] != "INSUFFICIENT_INVENTORY" for row in events):
        raise RuntimeError("Unexpected follow-up response from the order API.")
    return {"browsers": BROWSERS, "order_attempts": sum(row["http_status"] is not None for row in events),
            "false_positive_stock_rejections": sum(row["false_positive_stock_rejection"] for row in events),
            "events": events}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", choices=("all", "race", "stockout"), default="all")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    settings = json.loads(SETTINGS.read_text())
    settings.update(stock_per_ingredient=INITIAL_INGREDIENT_STOCK, cache_ttl_s=3)
    controls = {"hardware": hardware(), "versions": versions(), "settings": settings,
                "execution": "local_process", "started_at": now(), "repeats": REPEATS,
                "backend_sha256": file_hash(ROOT / "backend/server.js"),
                "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    save_json(output / "metadata.json", controls)
    rng = random.Random(78623)
    jobs = []
    for repeat in range(1, REPEATS + 1):
        for case, modes in CASES:
            if args.case not in ("all", case):
                continue
            order_of_modes = list(modes)
            rng.shuffle(order_of_modes)
            jobs.extend((case, repeat, mode) for mode in order_of_modes)
    with (output / "plan.csv").open("w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(("case", "repeat", "mode"))
        writer.writerows(jobs)
    for case, repeat, mode in jobs:
        run_id = f"{case}-r{repeat:02d}-{mode}"
        folder = output / run_id
        folder.mkdir()
        with tempfile.TemporaryDirectory(prefix="pos-correctness-") as temp, ExitStack() as stack:
            service = Services(stack, Path(temp), folder, settings, mode, test_gate=case == "race")
            result = forced_refill(service, run_id) if case == "race" else stockout(service, run_id, mode)
            audit = service.audit(INITIAL_INGREDIENT_STOCK)
        if not audit["valid"] or audit["committed_orders"] != 2:
            raise RuntimeError("Inventory audit failed.")
        save_json(folder / "summary.json", {"run_id": run_id, "case": case, "repeat": repeat, "mode": mode,
                                          **result, "inventory_audit": audit, "finished_at": now()})
        print(run_id, result.get("followup_stale", result.get("false_positive_stock_rejections")), flush=True)
    controls["finished_at"] = now()
    save_json(output / "metadata.json", controls)


if __name__ == "__main__":
    main()
