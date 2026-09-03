"""Webhook signature verification + typed event parsing.

Pins the wire contract with the server's `lenz/api/webhook_signing.py`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest

from lenz_io import (
    LenzWebhooks,
    LenzWebhookSignatureError,
    VerificationCompleted,
    VerificationFailed,
    VerificationNeedsInput,
    WebhookEvent,
    verify_signature,
)

SECRET = "whsec_test_abc123"


def _sign(body: bytes, secret: str = SECRET) -> str:
    mac = hmac.new(secret.encode(), body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


def _payload(event: str, **extra) -> bytes:
    base = {
        "event": event,
        "task_id": "tsk_abc",
        "attempt": 1,
        "delivered_at": datetime.now(timezone.utc).isoformat(),
        **extra,
    }
    return json.dumps(base).encode()


class TestVerifySignature:
    def test_valid_signature_returns_true(self):
        body = b'{"x": 1}'
        sig = _sign(body)
        assert verify_signature(body, sig, SECRET) is True

    def test_tampered_body_raises(self):
        body = b'{"x": 1}'
        sig = _sign(body)
        with pytest.raises(LenzWebhookSignatureError):
            verify_signature(body + b" ", sig, SECRET)

    def test_missing_signature_raises(self):
        with pytest.raises(LenzWebhookSignatureError):
            verify_signature(b"{}", "", SECRET)

    def test_str_body_rejected(self):
        with pytest.raises(LenzWebhookSignatureError):
            verify_signature("{}", "sha256=abc", SECRET)  # type: ignore[arg-type]


class TestLenzWebhooks:
    def test_constructor_requires_non_empty_secret(self):
        with pytest.raises(ValueError):
            LenzWebhooks(secret="")

    def test_parse_completed_event(self):
        body = _payload(
            "verification.completed",
            verification_id="vid_1",
            status="completed",
            result={
                "verification_id": "vid_1",
                "claim": "Sample claim.",
                "verdict": "False",
                "confidence": "high",
                "lenz_score": 2,
                "created_at": "2026-05-22T12:00:00Z",
                "modified_at": None,
            },
        )
        wh = LenzWebhooks(secret=SECRET)
        event = wh.parse(body, {"X-Lenz-Signature": _sign(body)})
        assert isinstance(event, VerificationCompleted)
        assert event.verification_id == "vid_1"
        # Flat verdict block — accessed by string key on the raw dict.
        # Categorical confidence only; the numeric confidence_score is gone.
        assert event.result["verdict"] == "False"
        assert event.result["confidence"] == "high"
        assert event.result["lenz_score"] == 2
        assert "confidence_score" not in event.result
        # `published_at` is no longer part of the payload
        assert "published_at" not in event.result

    def test_parse_failed_event(self):
        body = _payload("verification.failed", error="research_empty")
        wh = LenzWebhooks(secret=SECRET)
        event = wh.parse(body, {"X-Lenz-Signature": _sign(body)})
        assert isinstance(event, VerificationFailed)
        assert event.error == "research_empty"

    def test_parse_needs_input_event(self):
        body = _payload(
            "verification.needs_input",
            needs_input={"reason": "multi_claim", "claims": [{"text": "A", "domain": "x"}]},
        )
        wh = LenzWebhooks(secret=SECRET)
        event = wh.parse(body, {"X-Lenz-Signature": _sign(body)})
        assert isinstance(event, VerificationNeedsInput)
        assert event.needs_input["reason"] == "multi_claim"
        assert event.hint == ""  # an older server sends no hint

    def test_parse_needs_input_event_lifts_hint(self):
        hint = "The input holds two distinct claims. Pick the one to verify via /select."
        body = _payload(
            "verification.needs_input",
            needs_input={"reason": "multi_claim", "claims": [{"text": "A", "domain": "x"}], "hint": hint},
        )
        wh = LenzWebhooks(secret=SECRET)
        event = wh.parse(body, {"X-Lenz-Signature": _sign(body)})
        assert isinstance(event, VerificationNeedsInput)
        assert event.hint == hint
        assert event.needs_input["hint"] == hint  # the raw block still carries it too

    def test_tampered_body_raises_with_clear_message(self):
        body = _payload("verification.completed")
        wh = LenzWebhooks(secret=SECRET)
        with pytest.raises(LenzWebhookSignatureError) as ei:
            wh.parse(body + b"x", {"X-Lenz-Signature": _sign(body)})
        assert "mismatch" in str(ei.value).lower()

    def test_missing_signature_header_raises(self):
        body = _payload("verification.completed")
        wh = LenzWebhooks(secret=SECRET)
        with pytest.raises(LenzWebhookSignatureError):
            wh.parse(body, {})

    def test_old_delivered_at_outside_replay_window_raises(self):
        old_ts = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
        body = json.dumps({"event": "verification.completed", "task_id": "tsk", "delivered_at": old_ts}).encode()
        wh = LenzWebhooks(secret=SECRET, replay_window_seconds=300)
        with pytest.raises(LenzWebhookSignatureError) as ei:
            wh.parse(body, {"X-Lenz-Signature": _sign(body)})
        assert "replay" in str(ei.value).lower()

    def test_unknown_event_returns_generic_event(self):
        body = _payload("verification.future_event")
        wh = LenzWebhooks(secret=SECRET)
        event = wh.parse(body, {"X-Lenz-Signature": _sign(body)})
        assert type(event) is WebhookEvent
        assert event.event == "verification.future_event"

    def test_malformed_json_body_raises_signature_error(self):
        body = b"not json {"
        wh = LenzWebhooks(secret=SECRET)
        with pytest.raises(LenzWebhookSignatureError):
            wh.parse(body, {"X-Lenz-Signature": _sign(body)})

    def test_lowercase_header_lookup(self):
        body = _payload("verification.completed")
        wh = LenzWebhooks(secret=SECRET)
        # Header name lowercased (Flask / WSGI sometimes)
        event = wh.parse(body, {"x-lenz-signature": _sign(body)})
        assert isinstance(event, VerificationCompleted)


class TestCertificateAnchored:
    """`certificate.anchored` — the event publishers must key on.

    The warranty requires the certificate's timestamp to PRECEDE what the
    customer publishes. A pipeline that publishes on `verification.completed`
    races the anchor and can put the statement out before cover exists, so an
    SDK that leaves this event untyped quietly encourages the wrong ordering.
    """

    def _payload(self, **overrides):
        payload = {
            "event": "certificate.anchored",
            "task_id": "t_1",
            "verification_id": "a1b2c3d4",
            "status": "completed",
            "attempt": 1,
            "coverage": {
                "status": "covered",
                "reasons": [],
                "certificate_id": "9f2c",
                "certificate_url": "https://lenz.io/certificate/9f2c",
                "as_of": "2026-09-03T10:00:00+00:00",
                "currency": "EUR",
                "cap": 10000,
                "aggregate": 500000,
                "terms_version": "v1",
            },
        }
        payload.update(overrides)
        return payload

    def test_it_parses_as_its_own_type(self):
        from lenz_io.webhooks import CertificateAnchored, _build_event

        event = _build_event(self._payload())
        assert isinstance(event, CertificateAnchored)
        assert event.verification_id == "a1b2c3d4"
        assert event.coverage["certificate_id"] == "9f2c"
        assert event.coverage["cap"] == 10000

    def test_it_carries_coverage_instead_of_result(self):
        """`result` is null on this event — it reports a timestamp landing, not
        a verdict being produced. A caller reaching for `result` here gets
        nothing, which is why the block has its own field."""
        from lenz_io.webhooks import CertificateAnchored, _build_event

        event = _build_event(self._payload(result=None))
        assert isinstance(event, CertificateAnchored)
        assert not hasattr(event, "result")
        assert event.coverage["status"] == "covered"

    def test_a_missing_coverage_block_is_an_empty_dict_not_a_crash(self):
        from lenz_io.webhooks import _build_event

        event = _build_event(self._payload(coverage=None))
        assert event.coverage == {}

    def test_an_unknown_event_still_falls_through_to_the_base(self):
        """Adding a fourth branch must not break forward compatibility."""
        from lenz_io.webhooks import WebhookEvent, _build_event

        event = _build_event({"event": "something.new", "task_id": "t_2"})
        assert type(event) is WebhookEvent
