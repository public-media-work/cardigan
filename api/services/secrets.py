"""
Centralized secret management for Cardigan.

Resolution order:
1. Docker secret file (/run/secrets/<name_lowercase>)
2. Environment variable
3. get-secret.sh on PATH (local dev only)

This module replaces the duplicated keychain-loading boilerplate that was
previously copy-pasted across main.py, run_worker.py, airtable.py, and
langfuse_client.py.
"""

import logging
import shutil
import subprocess
import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)

DOCKER_SECRETS_DIR = Path("/run/secrets")

# ---------------------------------------------------------------------------
# macOS Keychain loader (local dev only — not available in Docker/CI)
# ---------------------------------------------------------------------------


def _keychain_get_secret(key: str) -> Optional[str]:
    """Resolve a secret via ``get-secret.sh``, which the environment puts on PATH.

    Local dev only. Docker/CI resolve from /run/secrets or the environment and
    never reach this, so a missing script is warned about, not raised.
    """
    exe = shutil.which("get-secret.sh")
    if not exe:
        _log.warning(
            "get-secret.sh is not on PATH; cannot resolve %s from the secrets "
            "backend. Install the machine-ops bin dir on PATH or set %s in the "
            "environment.", key, key,
        )
        return None
    try:
        result = subprocess.run([exe, key], capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.rstrip("\n") or None


def get_secret(key: str) -> Optional[str]:
    """Get a secret value by name.

    Checks in order:
    1. Docker secret file at /run/secrets/<key_lowercase>
    2. Environment variable with the exact key name
    3. macOS Keychain (local dev fallback)

    Returns None if the secret is not found in any source.
    """
    # 1. Docker secret file
    secret_file = DOCKER_SECRETS_DIR / key.lower()
    if secret_file.is_file():
        try:
            return secret_file.read_text().strip()
        except OSError:
            pass

    # 2. Environment variable
    value = os.environ.get(key)
    if value:
        return value

    # 3. macOS Keychain
    value = _keychain_get_secret(key)
    if value:
        return value

    return None


@lru_cache(maxsize=1)
def _bootstrap_complete() -> bool:
    """One-time bootstrap: populate os.environ from Docker secrets / Keychain.

    Called from main.py and run_worker.py at startup so that downstream code
    using os.environ.get() (e.g., middleware, LLM client) picks up secrets
    without needing to call get_secret() directly.
    """
    keys = [
        "OPENROUTER_API_KEY",
        "AIRTABLE_API_KEY",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "CARDIGAN_API_KEY",
        "LOCAL_LLM_API_KEY",
    ]
    for key in keys:
        if key not in os.environ:
            value = get_secret(key)
            if value:
                os.environ[key] = value
    return True


def bootstrap_secrets() -> None:
    """Populate os.environ with secrets from Docker files / Keychain.

    Safe to call multiple times — only runs once.
    """
    _bootstrap_complete()
