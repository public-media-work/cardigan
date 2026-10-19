"""Single source of truth for the LLM config file location.

api and worker run as separate processes; both must read/write the SAME
config file so Settings changes made on the API take effect on the worker.
Point LLM_CONFIG_PATH at a path on a shared volume (Docker) or state dir
(native) in production.

The live file is seeded from the packaged default and then kept *additively*
in step with it (#440): keys the default has and the live file lacks are added
at startup, at any depth; values already present are never overwritten, so
Settings-UI changes survive. Value changes in the repo therefore do NOT reach
an existing live file — `config_drift()` (and scripts/config_drift.py) reports
them so a deploy can show them instead of hiding them.
"""

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, List, Tuple

logger = logging.getLogger(__name__)

# Packaged default shipped with the code (repo-relative).
DEFAULT_CONFIG = Path("config/llm-config.json")


def merge_missing(target: dict, default: dict, _prefix: str = "") -> List[str]:
    """Add keys from ``default`` that ``target`` lacks, recursing into nested dicts.

    Mutates ``target`` in place and returns the dotted paths that were added.
    Existing values are never replaced. Lists and scalars are leaves: a list
    present in ``target`` is kept as-is, not merged element-wise.
    """
    added: List[str] = []
    for key, default_value in default.items():
        path = f"{_prefix}{key}"
        if key not in target:
            target[key] = default_value
            added.append(path)
        elif isinstance(target[key], dict) and isinstance(default_value, dict):
            added.extend(merge_missing(target[key], default_value, f"{path}."))
    return added


def config_drift(live: dict, default: dict, _prefix: str = "") -> List[Tuple[str, Any, Any]]:
    """Return ``(path, live_value, default_value)`` for leaves whose values differ.

    Only keys present in both are compared; missing keys are merge_missing's job.
    Drift is not necessarily wrong — it may be a deliberate Settings-UI change —
    so this reports rather than corrects.
    """
    drift: List[Tuple[str, Any, Any]] = []
    for key, default_value in default.items():
        if key not in live:
            continue
        path = f"{_prefix}{key}"
        live_value = live[key]
        if isinstance(live_value, dict) and isinstance(default_value, dict):
            drift.extend(config_drift(live_value, default_value, f"{path}."))
        elif live_value != default_value:
            drift.append((path, live_value, default_value))
    return drift


def _write_atomic(target: Path, data: dict) -> None:
    # api and worker can both start at once; os.replace keeps readers from ever
    # seeing a half-written file.
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, target)


def live_config_path() -> Path:
    """Return the active config path. Pure: never creates or rewrites anything."""
    return Path(os.getenv("LLM_CONFIG_PATH", str(DEFAULT_CONFIG)))


def resolve_config_path() -> Path:
    """Return the active config path, seeding or additively updating it from the default.

    Call at process startup. The merge reads, then atomically replaces, the file;
    a Settings write landing inside that sub-millisecond window would be lost,
    which is acceptable because it only runs while the processes are starting.
    """
    target = live_config_path()
    if target == DEFAULT_CONFIG or not DEFAULT_CONFIG.exists():
        return target
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        # Copy to a temp file and rename, so api and worker starting together
        # never see (or seed from) a half-written file.
        tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        shutil.copyfile(DEFAULT_CONFIG, tmp)
        os.replace(tmp, target)
        return target

    try:
        live = json.loads(target.read_text())
        default = json.loads(DEFAULT_CONFIG.read_text())
    except (OSError, json.JSONDecodeError) as e:
        # Leave an unreadable live file alone; the config readers fail loudly on it.
        logger.warning("Skipping additive config seeding for %s: %s", target, e)
        return target
    if not isinstance(live, dict) or not isinstance(default, dict):
        logger.warning("Skipping additive config seeding for %s: top level is not a JSON object", target)
        return target

    added = merge_missing(live, default)
    if added:
        _write_atomic(target, live)
        logger.info("Added %d new config key(s) to %s: %s", len(added), target, ", ".join(added))
    return target
