"""Exercise exact stock-version comparisons and Redis generations on a disposable instance."""

import argparse
import json
import tempfile
from contextlib import ExitStack
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))
from run_local import SETTINGS, Services, request
from run_trial import ROOT, now, save_json


def expect(actual, expected, message):
    if actual != expected:
        raise RuntimeError(f"{message}: got {actual!r}, expected {expected!r}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    settings = json.loads(SETTINGS.read_text())
    fill = (ROOT / "backend/lua/fill.lua").read_text()
    invalidate = (ROOT / "backend/lua/invalidate.lua").read_text()
    key, gen = "test:payload", "test:generation"
    with tempfile.TemporaryDirectory(prefix="pos-gens-") as directory, ExitStack() as stack:
        service = Services(stack, Path(directory), out, settings, "D")
        expect(request(service.base, "/__test/refill-gate")[0], 404, "test gate is off")

        def put(version, generation):
            body = json.dumps({"stockVersion": version})
            return int(service.redis("EVAL", fill, "2", key, gen, generation, version, body, "80").strip())

        lower, higher = "9007199254740992", "9007199254740993"
        expect(put(higher, "0"), 1, "empty cache with matching generation")
        expect(put(lower, "0"), 2, "late older refill in the same generation")
        expect(put(higher, "0"), 2, "equal refill in the same generation")
        expect(service.redis("PTTL", gen).strip(), "-2", "generation may be absent before first write")
        expect(service.redis("EVAL", invalidate, "2", key, gen).strip(), "1", "remove old payload")
        expect(service.redis("PTTL", gen).strip(), "-1", "generation survives expiry")
        expect(put(lower, "0"), 0, "old refill against empty payload and changed generation")
        expect(service.redis("EXISTS", key).strip(), "0", "old payload remains absent")
        expect(put(higher, "1"), 1, "new fill after advancing the generation")
        expect(json.loads(service.redis("GET", key)), {"stockVersion": higher}, "payload version")

    save_json(out / "validation.json", {"status": "passed", "verified_at": now(),
        "checks": ["test gate disabled by default", "initial empty fill", "older and equal fill rejection",
                   "exact comparison above 2**53", "generation retained on deletion", "empty-key stale refill rejection",
                   "new-generation refill"]})
    print("Redis generation and version checks passed.")


if __name__ == "__main__":
    main()
