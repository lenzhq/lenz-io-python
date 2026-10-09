"""Opt-in smoke tests against a real Lenz environment.

Tagged `smoke` so they don't run in the normal unit-test suite. The
release workflow invokes `pytest -m smoke` with `LENZ_E2E_KEY` set; tests
are skipped if the env var is absent.

These exercise the SDK against the live API across the four primitives:
  1. ``extract`` — free, parses identified_claims
  2. ``assess`` — fast 3-model verdict, returns flat claim entries
  3. ``verify_and_wait`` — the quickstart claim at ``depth="low"``, the
     cheap run (~60s); a cache hit is a bonus, never assumed
  4. ``ask.history`` — read-only follow-up surface (no exchange burned)
  5. ``cancel`` — a ``depth="low"`` run stopped right after it starts
  6. request options — ``with_options`` and a per-call ``timeout`` on ``assess``

Plus webhook signature roundtrip + ``/me/usage`` shape.

Tests are intentionally minimal to keep release smoke short (~1 min, 3 at
worst) and deterministic. The full SDK behavior matrix lives in
test_client.py.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os

import pytest

from lenz_io import Lenz, LenzPipelineError, LenzWebhooks, verify_signature

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


def test_quickstart_claim_verifies_at_low_depth(smoke_client):
    """The README quickstart claim through a real ``/verify`` run.

    The API's verdict cache lasts an hour, so this is usually a fresh
    pipeline run: ``depth="low"`` keeps it cheap and short (~60s). The
    150s budget covers a slow run; a cache hit inside the hour is a bonus.
    """
    v = smoke_client.verify_and_wait(claim="Sharks don't get cancer", depth="low", timeout=150)
    assert v.verdict  # any non-empty verdict string


def test_a_run_can_be_cancelled(smoke_client):
    """``cancel`` answers 200 whatever the state of the run: stopped by this
    call, or already ended. Neither is an error, so the test cannot be flaky.

    A cancelled verification is not charged. A cache hit (an answer the API
    already holds) has finished by the time we cancel, which is the other
    branch. Not the quickstart claim, so it is less likely to be one."""
    started = smoke_client.verify(claim="The Eiffel Tower is in Paris", depth="low")
    result = smoke_client.cancel(started.task_id)
    assert result.task_id == started.task_id
    if result.cancelled:
        assert result.status == "cancelled"
        # Safe to repeat: still cancelled, and a wait ends on it at once.
        again = smoke_client.cancel(started.task_id)
        assert (again.cancelled, again.status) == (True, "cancelled")
        with pytest.raises(LenzPipelineError) as raised:
            smoke_client.wait(started.task_id, timeout=30)
        assert raised.value.failure_class == "cancelled"
    else:
        # It finished first; nothing was left to stop.
        assert result.status in ("completed", "failed")


def test_assess_returns_typed_claims(smoke_client):
    """``/assess`` is sync, ~15s. Returns one entry per identified claim."""
    out = smoke_client.assess(text="Sharks don't get cancer")
    assert out.claims, "assess returned zero claims"
    first = out.claims[0]
    assert first.claim
    assert first.verdict
    assert first.confidence in ("high", "medium", "low")


def test_request_options_reach_the_live_api(smoke_client):
    """The request options on a real call: a copy with its own retries and an
    extra header, and a per-call timeout (the same claim as above, so the
    API's verdict memo usually answers it)."""
    copy = smoke_client.with_options(max_retries=1, extra_headers={"X-Smoke-Check": "request-options"})
    out = copy.assess(claim="Sharks don't get cancer", timeout=120)
    assert out.claims and out.claims[0].verdict


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
    # We don't assert exact values; just the balance + projection shape.
    assert isinstance(u.plan, str)
    assert isinstance(u.credits.remaining, int)
    assert u.costs["verify"] > 0  # the price list is populated
    for cap in (u.verify, u.ask, u.assess):
        assert isinstance(cap.quota_remaining, int)
        assert isinstance(cap.remaining, int)
        assert isinstance(cap.bonus, int)
    # assess is the unit, so its block counts the whole pool one-for-one.
    assert u.assess.remaining == u.credits.remaining // u.costs["assess"]
    assert isinstance(u.extract.daily_limit, int)


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
