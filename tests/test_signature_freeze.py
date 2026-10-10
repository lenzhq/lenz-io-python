"""The public signatures of the sync client, frozen: every public method of
``Lenz`` and its namespaces, with its parameters, their kinds, defaults and
annotations, and its return annotation (the ``get_review`` overloads too).
Compared with ``fixtures/freeze/signatures.json``, recorded from the release
before the sync and async clients shared one core (regenerate with ``python
tests/test_signature_freeze.py`` for a deliberate change only)."""

from __future__ import annotations

import inspect
import json
import sys
import typing
from pathlib import Path
from typing import Any

import pytest

from lenz_io.client import Lenz, _AskNamespace, _LibraryNamespace, _VerificationsNamespace

GOLDEN = Path(__file__).parent / "fixtures" / "freeze" / "signatures.json"
_CLASSES = {
    "Lenz": Lenz,
    "verifications": _VerificationsNamespace,
    "ask": _AskNamespace,
    "library": _LibraryNamespace,
}


def surface() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for label, cls in _CLASSES.items():
        for name, value in sorted(vars(cls).items()):
            if not callable(value) or (name.startswith("_") and name not in ("__init__", "__enter__", "__exit__")):
                continue
            out[f"{label}.{name}"] = str(inspect.signature(value))
    return out


def test_the_signatures_are_frozen() -> None:
    assert surface() == json.loads(GOLDEN.read_text())["signatures"]


@pytest.mark.skipif(sys.version_info < (3, 11), reason="typing.get_overloads is 3.11+")
def test_the_overloads_are_frozen() -> None:
    overloads = [str(inspect.signature(f)) for f in typing.get_overloads(Lenz.get_review)]  # type: ignore[attr-defined,unused-ignore]
    assert overloads == json.loads(GOLDEN.read_text())["get_review_overloads"]


if __name__ == "__main__":  # pragma: no cover - regeneration, by hand only
    data = {
        "signatures": surface(),
        "get_review_overloads": [str(inspect.signature(f)) for f in typing.get_overloads(Lenz.get_review)],  # type: ignore[attr-defined,unused-ignore]
    }
    GOLDEN.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    sys.stdout.write(f"wrote {GOLDEN}\n")
