"""Single source of truth for the LLM config file location.

api and worker run as separate processes; both must read/write the SAME
config file so Settings changes made on the API take effect on the worker.
Point LLM_CONFIG_PATH at a path on a shared volume (Docker) or state dir
(native) in production.

The live file is seeded from the packaged default and then kept *additively*
in step with it (#440): keys the default has and the live file lacks are added
at startup, at any depth; values already present are never overwritten, so
Settings-UI changes survive. A changed default reaches an existing live file
only when it is listed in config/superseded-defaults.json and the live value
still equals an old shipped default (`apply_superseded_defaults`, #499). Every
other value change is reported by `config_drift()` (and scripts/config_drift.py)
so a deploy can show it instead of hiding it.
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
# Values the packaged default used to ship, so they can follow a changed default.
SUPERSEDED_DEFAULTS = Path("config/superseded-defaults.json")


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


_MISSING = object()


def _get_path(data: Any, path: List[str]) -> Any:
    for key in path:
        if not isinstance(data, dict) or key not in data:
            return _MISSING
        data = data[key]
    return data


def apply_superseded_defaults(live: dict, default: dict, superseded: List[dict]) -> List[Tuple[str, Any, Any]]:
    """Move values still at a previously shipped default onto the current default.

    ``superseded`` entries are ``{"path": [...], "was": [old values]}``. A live
    value equal to one of ``was`` was never chosen by anyone -- it is just the
    old default -- so it follows the new one. Anything else is a deliberate
    change and stays. This is how a changed default reaches an existing live
    file without breaking merge_missing's never-overwrite rule (#499).

    Mutates ``live`` in place; returns ``(dotted path, old value, new value)``.
    """
    changed: List[Tuple[str, Any, Any]] = []
    for entry in superseded:
        path = entry.get("path") or []
        if not path:
            continue
        new_value = _get_path(default, path)
        old_value = _get_path(live, path)
        if new_value is _MISSING or old_value is _MISSING or old_value == new_value:
            continue
        if old_value in (entry.get("was") or []):
            parent = _get_path(live, path[:-1]) if len(path) > 1 else live
            parent[path[-1]] = new_value
            changed.append((".".join(path), old_value, new_value))
    return changed


def load_superseded() -> List[dict]:
    """Entries from config/superseded-defaults.json; [] if absent or unreadable."""
    try:
        data = json.loads(SUPERSEDED_DEFAULTS.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    entries = data.get("superseded") if isinstance(data, dict) else None
    return entries if isinstance(entries, list) else []


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


def _replace_atomically(target: Path, write) -> None:
    """Write via ``write(tmp_path)`` to a temp file, then rename it over ``target``.

    api and worker can both start at once; os.replace keeps readers from ever
    seeing a half-written file. The temp file is removed if the write fails.
    """
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        write(tmp)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def _write_atomic(target: Path, data: dict) -> None:
    def write(tmp: Path) -> None:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")

    _replace_atomically(target, write)


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
        _replace_atomically(target, lambda tmp: shutil.copyfile(DEFAULT_CONFIG, tmp))
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
    moved = apply_superseded_defaults(live, default, load_superseded())
    if added or moved:
        _write_atomic(target, live)
    if added:
        logger.info("Added %d new config key(s) to %s: %s", len(added), target, ", ".join(added))
    for path, old, new in moved:
        logger.info("Moved %s from old default %r to new default %r in %s", path, old, new, target)
    return target
