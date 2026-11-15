"""Format recorded POS trial data as SQL or a dataset manifest for a trial database."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks"))
from trial_data import prepare_trial_data

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--manifest", action="store_true")
args = parser.parse_args()
settings = json.loads((ROOT / "benchmarks/settings.json").read_text())
sql, manifest = prepare_trial_data(settings)
print(json.dumps(manifest, indent=2) if args.manifest else sql, end="\n" if args.manifest else "")
