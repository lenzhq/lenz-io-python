"""Key resolution + on-disk config — the only stateful piece of the CLI.

The SDK owns no config: you hand it a key. This module resolves the key from
``--api-key`` flag > ``LENZ_API_KEY`` env > config file, and persists a pasted
key. Config dir comes from ``platformdirs`` (correct on macOS/Windows/Linux),
never a hand-rolled ``XDG_CONFIG_HOME``.

Storage layout (``~/.config/lenz/config.json`` on Linux)::

    {"api_key": "lenz_...", "base_url": "https://lenz.io/api/v1"}

Dir is created ``0700``, file ``0600`` — the key is plaintext on disk
(an OS-keyring backend can drop in later behind this same module).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import platformdirs

from lenz_io.client import DEFAULT_BASE_URL

APP_NAME = "lenz"
ENV_API_KEY = "LENZ_API_KEY"
ENV_BASE_URL = "LENZ_BASE_URL"


class ConfigError(Exception):
    """Raised when the on-disk config exists but is unreadable / malformed."""


def config_path() -> Path:
    return Path(platformdirs.user_config_dir(APP_NAME)) / "config.json"


def _load() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Config file at {path} is unreadable ({exc}).") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Config file at {path} is not a JSON object.")
    return data


def resolve_api_key(flag_key: str | None) -> tuple[str, str]:
    """Return ``(key, source)`` where source is ``flag|env|file|none``."""
    if flag_key:
        return flag_key, "flag"
    env = os.environ.get(ENV_API_KEY)
    if env:
        return env, "env"
    key = _load().get("api_key") or ""
    if key:
        return key, "file"
    return "", "none"


def resolve_base_url(flag_base: str | None) -> str:
    if flag_base:
        return flag_base.rstrip("/")
    env = os.environ.get(ENV_BASE_URL)
    if env:
        return env.rstrip("/")
    base = _load().get("base_url")
    if base:
        return str(base).rstrip("/")
    return DEFAULT_BASE_URL


def save_api_key(key: str) -> Path:
    """Persist ``key`` to the config file with ``0600`` perms; returns the path."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = _load()
        except ConfigError:
            data = {}
    data["api_key"] = key
    # O_CREAT with mode 0600 so the key is never briefly world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def mask_key(key: str) -> str:
    if not key:
        return "(none)"
    if len(key) <= 10:
        return "****"
    return f"{key[:6]}…{key[-4:]}"
