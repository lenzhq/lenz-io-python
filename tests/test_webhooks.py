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
                "lenz_score": 1.5,
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
        assert event.result["lenz_score"] == 1.5
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
