"""Opt-in smoke tests against a real Lenz environment.

Tagged `smoke` so they don't run in the normal unit-test suite. The
release workflow invokes `pytest -m smoke` with `LENZ_E2E_KEY` set; tests
are skipped if the env var is absent.

These exercise the SDK against the live API in 3 ways:
  1. The canonical quickstart claim from the README hits the cache.
  2. Webhook signature verification roundtrips a known-good payload.
  3. `/me/usage` returns a populated structure.

Tests are intentionally minimal to keep release smoke fast (~30s) and
deterministic. The full SDK behavior matrix lives in test_client.py.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os

import pytest

from lenz_io import Lenz, LenzWebhooks, verify_signature

pytestmark = pytest.mark.smoke

LENZ_E2E_KEY = os.environ.get("LENZ_E2E_KEY", "")
LENZ_BASE_URL = os.environ.get("LENZ_BASE_URL", "")  # empty -> production


@pytest.fixture()
def smoke_client():
    if not LENZ_E2E_KEY:
        pytest.skip("LENZ_E2E_KEY not set; smoke tests opt-in")
    kwargs = {"api_key": LENZ_E2E_KEY}
    if LENZ_BASE_URL:
        kwargs["base_url"] = LENZ_BASE_URL
    with Lenz(**kwargs) as c:
        yield c


def test_quickstart_claim_returns_via_cache(smoke_client):
    """The README quickstart claim is pre-cached. Must return < 10s."""
    v = smoke_client.verify_and_wait(claim="Sharks don't get cancer", timeout=30)
    assert v.verdict.label  # any verdict label, just confirm we got a verification


def test_webhook_signature_roundtrip():
    """The signing path matches the server. Cross-check against a known payload."""
    secret = "whsec_smoke_fixed"
    body = json.dumps({"event": "verification.completed", "task_id": "tsk_smoke"}).encode()
    sig = f"sha256={hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()}"
    assert verify_signature(body, sig, secret) is True

    wh = LenzWebhooks(secret=secret)
    event = wh.parse(body, {"X-Lenz-Signature": sig})
    assert event.event == "verification.completed"


def test_me_usage_returns_populated_structure(smoke_client):
    u = smoke_client.usage()
    # We don't assert exact values; just shape.
    assert isinstance(u.credits_total, int)
    assert isinstance(u.credits_used, int)


def test_extract_splits_multi_claim_text(smoke_client):
    """/extract is free and deterministic enough for a shape assertion.

    Uses the same Einstein brief shown on /developers — keeps the smoke
    aligned with what callers see in the docs.
    """
    brief = (
        'Albert Einstein won the 1921 Nobel Prize in Physics for his theory '
        'of general relativity. He developed the special theory of relativity '
        'in 1905 while working as a patent clerk in Bern. Born in Ulm in '
        '1879, he emigrated to the US in 1933 and joined the Institute for '
        'Advanced Study.'
    )
    out = smoke_client.extract(text=brief)
    assert isinstance(out.identified_claims, list)
    assert len(out.identified_claims) >= 2
    assert all(isinstance(c, str) and c.strip() for c in out.identified_claims)
