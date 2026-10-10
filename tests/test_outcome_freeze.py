"""How every public call form ends, frozen in full.

``test_request_freeze.py`` pins what each case in ``freeze_cases.py`` sends
and the error class it ends with. This pins the rest of what a caller can read
off the end of the call: every attribute of the error (``code``,
``status_code``, ``retryable``, ``retry_after``, ``request_id``,
``idempotency_key``, the ids, the partial body, ...), its ``str()``, the types
of its ``__cause__`` and ``__context__``, or the whole result as JSON.
Compared with ``fixtures/freeze/outcomes.json``.

Recorded from the release before the sync and async clients shared one core,
so the refactor that introduced it is proven not to change an outcome; the
async client is held to the same file (``test_async_parity.py``).

A difference here is a behaviour change. Regenerating the file
(``python tests/test_outcome_freeze.py``) is for a deliberate change only, and
the diff of the JSON is the review.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from freeze_cases import CASES
from freeze_harness import CLIENTS, outcome_detail, recording

GOLDEN = Path(__file__).parent / "fixtures" / "freeze" / "outcomes.json"


def observe(monkeypatch: pytest.MonkeyPatch, name: str) -> dict[str, Any]:
    _, client_name, call, answers = next(c for c in CASES if c[0] == name)
    with recording(monkeypatch, answers):
        client = CLIENTS[client_name]()
        try:
            return outcome_detail(lambda: call(client))
        finally:
            client.close()


@pytest.fixture(autouse=True)
def _no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LENZ_API_KEY", raising=False)
    monkeypatch.delenv("LENZ_BASE_URL", raising=False)


@pytest.mark.parametrize("name", [c[0] for c in CASES])
def test_the_outcome_is_frozen(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    golden = json.loads(GOLDEN.read_text())
    assert observe(monkeypatch, name) == golden[name]


def test_every_case_is_frozen() -> None:
    assert sorted(json.loads(GOLDEN.read_text())) == sorted(c[0] for c in CASES)


if __name__ == "__main__":  # pragma: no cover - regeneration, by hand only
    import os

    os.environ.pop("LENZ_API_KEY", None)
    os.environ.pop("LENZ_BASE_URL", None)
    out: dict[str, Any] = {}
    mp = pytest.MonkeyPatch()
    try:
        for case in CASES:
            out[case[0]] = observe(mp, case[0])
            mp.undo()
    finally:
        mp.undo()
    GOLDEN.write_text(json.dumps(out, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    sys.stdout.write(f"wrote {len(out)} cases to {GOLDEN}\n")
