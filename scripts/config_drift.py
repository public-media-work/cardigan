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
import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.services import config_path  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--strict", action="store_true", help="exit 1 if any value differs")
    args = parser.parse_args()

    default_path = config_path.DEFAULT_CONFIG
    live_path = config_path.live_config_path()
    if live_path == default_path:
        print(f"LLM_CONFIG_PATH unset or pointing at {default_path}; nothing to compare.")
        return 0
    if not live_path.exists():
        print(f"Live config {live_path} does not exist yet; it will be seeded from the default.")
        return 0

    # cardigan-deploy runs this on every deploy: report a bad file, don't crash.
    try:
        live = json.loads(live_path.read_text())
        default = json.loads(default_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"Config drift: cannot compare, {e.__class__.__name__}: {e}")
        return 1 if args.strict else 0
    if not isinstance(live, dict) or not isinstance(default, dict):
        print(f"Config drift: cannot compare, {live_path} or {default_path} is not a JSON object.")
        return 1 if args.strict else 0
    drift = config_path.config_drift(live, default)

    if not drift:
        print(f"Config drift: none ({live_path} matches {default_path} on every shared key).")
        return 0

    # Values still at an old shipped default move to the new one when the
    # services next start (apply_superseded_defaults); say so, so the deploy
    # doesn't present them as kept.
    moving = {
        path
        for path, _, _ in config_path.apply_superseded_defaults(
            copy.deepcopy(live), default, config_path.load_superseded()
        )
    }

    print(f"Config drift: {len(drift)} value(s) in {live_path} differ from {default_path}:")
    for path, live_value, default_value in drift:
        note = "  (old default: moves to the repo value at restart)" if path in moving else ""
        print(f"  {path}: live={json.dumps(live_value)}  repo={json.dumps(default_value)}{note}")
    if len(moving) < len(drift):
        print(
            "Other live values are kept. Change one through Settings or edit the live file if the repo value should win."
        )
    remaining = len(drift) - len(moving)
    return 1 if args.strict and remaining else 0


if __name__ == "__main__":
    sys.exit(main())
