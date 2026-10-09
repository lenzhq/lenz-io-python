"""What every public call form puts on the wire, frozen.

For each case in ``freeze_cases.py``: every request's URL (query string as
sent), raw body bytes, ordered header pairs and the four ``httpx.Timeout``
components handed to httpx, the time of each request and every sleep under a
fake clock, and how the call ended. Compared with
``fixtures/freeze/requests.json``, recorded from the release before request
options existed: a call that passes no request option must send exactly this.

A difference here is a behaviour change. Regenerating the file
(``python tests/test_request_freeze.py``) is for a deliberate change only,
and the diff of the JSON is the review.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from freeze_cases import CASES
from freeze_harness import CLIENTS, outcome, recording

GOLDEN = Path(__file__).parent / "fixtures" / "freeze" / "requests.json"


def observe(monkeypatch: pytest.MonkeyPatch, name: str) -> dict[str, Any]:
    _, client_name, call, answers = next(c for c in CASES if c[0] == name)
    with recording(monkeypatch, answers) as rec:
        client = CLIENTS[client_name]()
        try:
            ended = outcome(lambda: call(client))
        finally:
            client.close()
    return {"requests": rec.requests, "sleeps": rec.clock.sleeps, "outcome": ended}


@pytest.fixture(autouse=True)
def _no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LENZ_API_KEY", raising=False)
    monkeypatch.delenv("LENZ_BASE_URL", raising=False)


@pytest.mark.parametrize("name", [c[0] for c in CASES])
def test_the_request_is_frozen(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
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
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(json.dumps(out, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    sys.stdout.write(f"wrote {len(out)} cases to {GOLDEN}\n")
