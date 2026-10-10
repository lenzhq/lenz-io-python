"""Every name importable from ``lenz_io`` and ``lenz_io.client`` stays
importable, and stays the same kind of thing.

Recorded from the release before the sync client's internals moved to the
shared core: a name a caller (or a test, or the CLI) imports from
``lenz_io.client``, private ones included, keeps resolving after the move.
New names may be added; none may disappear or change kind. Compared with
``fixtures/freeze/names.json`` (regenerate with ``python
tests/test_import_surface.py`` for a deliberate change only).
"""

from __future__ import annotations

import inspect
import json
import sys
import types
from pathlib import Path
from typing import Any

import lenz_io
import lenz_io.client

GOLDEN = Path(__file__).parent / "fixtures" / "freeze" / "names.json"


_TYPING = ("typing", "typing_extensions")


def _kind(value: Any) -> str:
    if inspect.isclass(value) and value.__module__ not in _TYPING:
        return "class"
    if getattr(value, "__module__", None) in _TYPING or type(value).__module__ in _TYPING:
        # A typing construct (``Any``, ``Literal[...]``, ``Final``, a
        # ``TypeVar``, ``overload``): what it is made of differs between
        # Python versions.
        return "typing"
    if inspect.isclass(value):
        return "class"
    if inspect.isfunction(value) or inspect.isbuiltin(value):
        return "function"
    return type(value).__name__


def surface() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for module in (lenz_io, lenz_io.client):
        out[module.__name__] = {
            name: _kind(value)
            for name, value in vars(module).items()
            if not name.startswith("__") and not isinstance(value, types.ModuleType)
        }
    out["lenz_io.__all__"] = {name: "listed" for name in lenz_io.__all__}
    out["lenz_io.client.__all__"] = {name: "listed" for name in lenz_io.client.__all__}
    return out


def test_no_name_disappears_or_changes_kind() -> None:
    golden = json.loads(GOLDEN.read_text())
    now = surface()
    for module, names in golden.items():
        missing = sorted(set(names) - set(now[module]))
        assert not missing, f"{module} lost {missing}"
        changed = sorted(n for n in names if now[module][n] != names[n])
        assert not changed, f"{module} changed kind: {changed}"


def test_the_patched_modules_are_still_there() -> None:
    # Tests and callers patch ``lenz_io.client.time.sleep`` / ``.monotonic``
    # and ``lenz_io.client.uuid.uuid4``: both stay module attributes.
    import time
    import uuid

    assert lenz_io.client.time is time
    assert lenz_io.client.uuid is uuid


if __name__ == "__main__":  # pragma: no cover - regeneration, by hand only
    GOLDEN.write_text(json.dumps(surface(), indent=1, sort_keys=True) + "\n")
    sys.stdout.write(f"wrote {GOLDEN}\n")
