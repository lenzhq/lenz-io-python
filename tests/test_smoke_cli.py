"""Opt-in CLI smoke tests against a real Lenz environment.

The inverse of test_cli.py: instead of mocking the SDK, this drives the real
``lenz`` binary as a subprocess against the live API. It's the only layer that
catches *contract drift* — that the server still produces the shapes our mocked
unit tests assume (the class of bug behind the `select` 422).

Tagged ``smoke`` (like test_smoke_staging.py) so it's excluded from the normal
suite; run on demand / in the release workflow:

    LENZ_E2E_KEY=lenz_... pytest -m smoke

Skipped if no key is set. Token-minimizing, mirroring the SDK smoke:
  - ``verify`` uses the pre-cached quickstart claim -> cache hit, < 30s, no
    fresh 8-model pipeline burned.
  - ``assess`` reuses the same claim (sync, ~5-10s).
  - ``extract`` is free (no credit charge).
  - ``--version`` needs no API call at all.
  - No live ``ask`` (it would burn a fresh exchange) — its thin CLI layer is
    covered by mocks in test_cli.py and manual QA.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.smoke

# The console script installed next to this venv's python (pip install -e .[cli]).
_LENZ = Path(sys.executable).parent / "lenz"
# Same pre-cached claim the SDK smoke uses, so `verify` stays a cheap cache hit.
CACHED_CLAIM = "Sharks don't get cancer"


def _key() -> str:
    return os.environ.get("LENZ_E2E_KEY") or os.environ.get("LENZ_API_KEY") or ""


@pytest.fixture(autouse=True)
def _require_key_and_binary():
    if not _key():
        pytest.skip("LENZ_E2E_KEY / LENZ_API_KEY not set; CLI smoke is opt-in")
    if not _LENZ.exists():
        pytest.skip(f"lenz console script not found at {_LENZ}; run `pip install -e .[cli]`")


def _run(*args: str, timeout: float = 90.0) -> subprocess.CompletedProcess[str]:
    """Run the real `lenz` binary with the key injected via env."""
    env = {**os.environ, "LENZ_API_KEY": _key()}  # LENZ_BASE_URL passes through for staging
    return subprocess.run(
        [str(_LENZ), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


def test_cli_version_runs_without_api():
    proc = _run("--version", timeout=20)
    assert proc.returncode == 0
    assert "lenz-cli" in proc.stdout


def test_cli_extract_real_contract():
    """`extract` is free; assert the binary returns a parseable claim payload."""
    proc = _run("extract", "Einstein won the 1921 Nobel Prize for his work on relativity", "--json", timeout=60)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    # framing fills atomic_claim (single) and/or identified_claims (multi)
    assert (data.get("atomic_claim") or "").strip() or data.get("identified_claims")


def test_cli_assess_real_contract():
    proc = _run("assess", CACHED_CLAIM, "--json", timeout=60)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["claims"], "assess returned zero claims"
    first = data["claims"][0]
    assert first["verdict"]
    assert first["confidence"] in ("high", "medium", "low")


def test_cli_verify_cached_real_contract():
    """The pre-cached claim returns a verdict via cache hit (cheap, < 30s) —
    exercises submit -> poll -> completed -> render against the real server."""
    proc = _run("verify", CACHED_CLAIM, "--json", "--timeout", "60", timeout=90)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["verdict"], "verify returned no verdict"
    assert data["verification_id"], "verify returned no verification_id"
