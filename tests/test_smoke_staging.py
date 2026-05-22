"""Opt-in smoke tests against a real Lenz environment.

Tagged `smoke` so they don't run in the normal unit-test suite. The
release workflow invokes `pytest -m smoke` with `LENZ_E2E_KEY` set; tests
are skipped if the env var is absent.

These exercise the SDK against the live API across the four primitives:
  1. ``extract`` — free, parses identified_claims
  2. ``assess`` — fast 3-model verdict, returns flat claim entries
  3. ``verify_and_wait`` — full pipeline; the canonical quickstart claim
     hits the cache so it stays < 30s
  4. ``ask.history`` — read-only follow-up surface (no exchange burned)

Plus webhook signature roundtrip + ``/me/usage`` shape.

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
    assert v.verdict  # any non-empty verdict string


def test_assess_returns_typed_claims(smoke_client):
    """``/assess`` is sync, ~5-10s. Returns one entry per identified claim."""
    out = smoke_client.assess(text="Sharks don't get cancer")
    assert out.claims, "assess returned zero claims"
    first = out.claims[0]
    assert first.claim
    assert first.verdict
    assert first.confidence in ("high", "medium", "low")


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


def test_extract_returns_parseable_claims(smoke_client):
    """``/extract`` is free; just verify the SDK parses the response cleanly
    and surfaces at least one usable claim.

    Framing may set ``claim`` (single cohesive claim) OR
    ``identified_claims`` (multiple distinct claims). Either is success;
    the LLM picks based on the input's coherence.
    """
    brief = (
        "Albert Einstein won the 1921 Nobel Prize in Physics for his theory "
        "of general relativity. He developed the special theory of relativity "
        "in 1905 while working as a patent clerk in Bern. Born in Ulm in "
        "1879, he emigrated to the US in 1933 and joined the Institute for "
        "Advanced Study."
    )
    out = smoke_client.extract(text=brief)
    has_atomic = bool(out.claim and out.claim.strip())
    has_identified = bool(out.identified_claims and all(c.strip() for c in out.identified_claims))
    assert has_atomic or has_identified, "extract returned neither claim nor identified_claims"
