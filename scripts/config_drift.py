#!/usr/bin/env python3
"""Report where the live LLM config differs from the packaged default.

Additive seeding (#440) adds *new* keys to the live config at startup but never
overwrites existing values, so a value changed in the repo (say, a raised
``backends.openrouter.max_tokens``) does not reach an existing deployment. This
prints every such difference so a deploy shows it instead of hiding it.

A difference is not necessarily wrong — it may be a deliberate Settings-page
change. Run from the repo root; it reads ``LLM_CONFIG_PATH`` like the app does.

Usage
-----
    python scripts/config_drift.py            # report; always exits 0
    python scripts/config_drift.py --strict   # exit 1 if anything differs
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.services.config_path import DEFAULT_CONFIG, config_drift  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--strict", action="store_true", help="exit 1 if any value differs")
    args = parser.parse_args()

    live_path = Path(os.getenv("LLM_CONFIG_PATH", str(DEFAULT_CONFIG)))
    if live_path == DEFAULT_CONFIG:
        print(f"LLM_CONFIG_PATH unset or pointing at {DEFAULT_CONFIG}; nothing to compare.")
        return 0
    if not live_path.exists():
        print(f"Live config {live_path} does not exist yet; it will be seeded from the default.")
        return 0

    live = json.loads(live_path.read_text())
    default = json.loads(DEFAULT_CONFIG.read_text())
    drift = config_drift(live, default)

    if not drift:
        print(f"Config drift: none ({live_path} matches {DEFAULT_CONFIG} on every shared key).")
        return 0

    print(f"Config drift: {len(drift)} value(s) in {live_path} differ from {DEFAULT_CONFIG}:")
    for path, live_value, default_value in drift:
        print(f"  {path}: live={json.dumps(live_value)}  repo={json.dumps(default_value)}")
    print("Live values are kept. Change one through Settings or edit the live file if the repo value should win.")
    return 1 if args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
