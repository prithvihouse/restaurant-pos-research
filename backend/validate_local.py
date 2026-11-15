"""Verify stockout handling, order replays, and shared-item cache behavior."""

import argparse
import json
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks"))
from run_local import Services, request


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def order(item, order_id=None):
    return {"itemId": item, "quantity": 1, "orderId": order_id or str(uuid.uuid4())}


def parallel_orders(base, bodies):
    with ThreadPoolExecutor(max_workers=len(bodies)) as pool:
        return list(pool.map(lambda body: request(base, "/orders", body), bodies))


def verify(output, settings, mode):
    output.mkdir()
    with tempfile.TemporaryDirectory(prefix="pos-check-") as workspace, ExitStack() as stack:
        api = Services(stack, Path(workspace), output, settings, mode)
        base = api.base
        menu_status, menu = request(base, "/menu")
        require(menu_status == 200 and len(menu) == 100, f"{mode} catalog response")
        path = "/menu/items/2"
        status, initial = request(base, path)
        require(status == 200 and initial["sellableUnits"] == 1, f"{mode} starting stock")

        bodies = [order(1) for _ in range(8)]
        replies = parallel_orders(base, bodies)
        winners = [(body, reply) for body, (code, reply) in zip(bodies, replies) if code == 201]
        stockouts = sum(code == 409 and result["code"] == "INSUFFICIENT_INVENTORY" for code, result in replies)
        require(len(winners) == 1 and stockouts == 7, f"{mode} concurrent last-unit check")
        _, observed = request(base, path)
        stale = observed["sellableUnits"] == 1 and observed["stockVersion"] == initial["stockVersion"]
        require(stale == (mode == "B"), f"{mode} shared item availability after order")
        code, result = request(base, "/orders", order(2))
        require(code == 409 and result["code"] == "INSUFFICIENT_INVENTORY", f"{mode} shared stockout")
        body, receipt = winners[0]
        require(request(base, "/orders", body) == (200, receipt), f"{mode} replay")
        code, result = request(base, "/orders", {**body, "quantity": 2})
        require(code == 409 and result["code"] == "ORDER_ID_CONFLICT", f"{mode} conflicting reuse")
        if mode == "B":
            time.sleep(settings["cache_ttl_s"] + .15)
            status, latest = request(base, path)
            require(status == 200 and latest["sellableUnits"] == 0, "B expiry check")

        repeated = parallel_orders(base, [order(5)] * 6)
        codes = sorted(code for code, _ in repeated)
        require(codes == [200] * 5 + [201], f"{mode} concurrent idempotency")
        require(all(response == repeated[0][1] for _, response in repeated), f"{mode} repeated receipt")
        if mode == "C":
            api.redis("SHUTDOWN", "NOSAVE")
            code, _ = request(base, "/orders", order(9))
            require(code == 201, "C acknowledgment after failed deletion")
            require(request(base, "/metrics")[1]["invalidationErrors"] == 1, "C invalidation failure counter")
            require(request(base, "/menu/items/9")[1]["sellableUnits"] == 0, "C cache error fallback")
        audit = api.audit(settings["stock_per_ingredient"])
        require(audit["valid"] and audit["committed_orders"] == (3 if mode == "C" else 2), f"{mode} audit")
        return {"mode": mode, "last_unit_acceptances": len(winners), "last_unit_rejections": stockouts,
                "shared_item_stale_before_expiry": stale, "replay_statuses": codes,
                "redis_failure_check": "passed" if mode == "C" else None, "audit": audit}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    settings = json.loads((ROOT / "benchmarks/settings.json").read_text())
    settings.update(stock_per_ingredient=2, cache_ttl_s=3)
    args.output.mkdir(parents=True, exist_ok=False)
    results = [verify(args.output / mode, settings, mode) for mode in "ABC"]
    result = {"purpose": "functional validation outside performance trials", "initial_stock": 2,
              "cache_ttl_s": 3, "modes": results}
    (args.output / "validation.json").write_text(json.dumps(result, indent=2) + "\n")
    print("Passed the low-stock, duplicate-order, cache and audit checks in all modes.")


if __name__ == "__main__":
    main()
