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


def resolve_api_key(flag_key: str | None, *, data: dict[str, Any] | None = None) -> tuple[str, str]:
    """Return ``(key, source)`` where source is ``flag|env|file|none``.

    Pass ``data`` (a pre-loaded config dict) to avoid re-reading the file when
    the caller already has it (see :func:`resolve_all`).

    Every source is ``.strip()``-ed: a stray newline/space from a paste makes an
    otherwise-valid key an illegal HTTP header value, which surfaces as a cryptic
    load-balancer 400 (not a 401) — strip it at the door so it can't.
    """
    flag = (flag_key or "").strip()
    if flag:
        return flag, "flag"
    env = (os.environ.get(ENV_API_KEY) or "").strip()
    if env:
        return env, "env"
    key = ((data if data is not None else _load()).get("api_key") or "").strip()
    if key:
        return key, "file"
    return "", "none"


def resolve_base_url(flag_base: str | None, *, data: dict[str, Any] | None = None) -> str:
    if flag_base:
        return flag_base.rstrip("/")
    env = os.environ.get(ENV_BASE_URL)
    if env:
        return env.rstrip("/")
    base = (data if data is not None else _load()).get("base_url")
    if base:
        return str(base).rstrip("/")
    return DEFAULT_BASE_URL


def resolve_all(flag_key: str | None, flag_base: str | None) -> tuple[str, str, str]:
    """Resolve ``(key, source, base_url)`` from a SINGLE config-file read.

    The callback needs both the key and the base URL; reading the file once
    keeps them a consistent snapshot (no TOCTOU window if a concurrent
    ``lenz login`` rewrites it) and halves the per-invocation I/O. Raises
    ``ConfigError`` if the file is malformed (the callback degrades gracefully).
    """
    data = _load()
    key, source = resolve_api_key(flag_key, data=data)
    base = resolve_base_url(flag_base, data=data)
    return key, source, base


def _write_config(data: dict[str, Any]) -> Path:
    """Write ``data`` to the config file atomically with ``0600`` perms.

    Single source of truth for the secure-write incantation so a future change
    (fsync, atomic rename, perm tweak) can't land on one writer and miss another.
    """
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    # O_CREAT with mode 0600 so the key is never briefly world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def save_api_key(key: str) -> Path:
    """Persist ``key`` to the config file with ``0600`` perms; returns the path."""
    data: dict[str, Any] = {}
    if config_path().exists():
        try:
            data = _load()
        except ConfigError:
            data = {}
    data["api_key"] = key
    return _write_config(data)


def clear_api_key() -> bool:
    """Remove the stored ``api_key`` from the config file.

    Returns ``True`` if a key was present. Other settings (e.g. ``base_url``)
    are preserved; the file is deleted if nothing else remains. A corrupt file
    is removed wholesale so the user can recover.
    """
    path = config_path()
    if not path.exists():
        return False
    try:
        data = _load()
    except ConfigError:
        path.unlink(missing_ok=True)
        return True
    had_key = bool(data.pop("api_key", None))
    if data:
        _write_config(data)
    else:
        path.unlink(missing_ok=True)
    return had_key


def mask_key(key: str) -> str:
    if not key:
        return "(none)"
    if len(key) <= 10:
        return "****"
    return f"{key[:6]}…{key[-4:]}"
