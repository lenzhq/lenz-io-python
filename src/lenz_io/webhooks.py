"""Webhook signature verification + typed event parsing.

The Lenz Public API delivers verification lifecycle events as
HMAC-SHA256-signed JSON POSTs to a customer-supplied ``webhook_url``.
This module exposes:

* ``LenzWebhooks(secret).parse(raw_body, headers) -> WebhookEvent`` —
  the framework-agnostic high-level entry point. Verifies the signature,
  checks the timestamp replay window, deserialises the payload into a
  typed event union, and returns it.

* ``verify_signature(raw_body, signature, secret) -> True`` — low-level
  escape hatch for callers who want only the signature check.

Server-side signing lives in ``lenz/api/webhook_signing.py`` in the main
Lenz repo; both sides MUST produce byte-identical signatures. Contract
tests on the SDK side pin this against a known-good payload.

Replay protection: the signed payload includes ``delivered_at`` (ISO
8601). ``LenzWebhooks.parse`` rejects payloads older than
``replay_window_seconds`` (default 300s / 5 minutes).

Customers MUST register the same secret with us via the
``/api-integration`` page. Rotating the secret on Lenz's side invalidates
old deliveries.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .errors import LenzWebhookSignatureError

SIGNATURE_HEADER = "X-Lenz-Signature"
SIGNATURE_PREFIX = "sha256="
DEFAULT_REPLAY_WINDOW_SECONDS = 300


def _sign(body: bytes, secret: str) -> str:
    """HMAC-SHA256 over the raw bytes; hex-encoded.

    Must match ``lenz/api/webhook_signing.py:sign`` server-side, byte
    for byte. Do not pre-process ``body`` here (no trimming, no encoding
    conversion) — the server signs the bytes it sent.
    """
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256)
    return f"{SIGNATURE_PREFIX}{mac.hexdigest()}"


def verify_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """Return ``True`` if ``signature`` is valid for ``raw_body``; raise otherwise.

    Returns rather than raising on success makes ``if verify_signature(...)``
    idioms work; the raise-on-bad path means a silent ``False`` can't
    accidentally pass through.
    """
    if not signature:
        raise LenzWebhookSignatureError(
            message="Missing webhook signature",
            cause=f"No {SIGNATURE_HEADER} header on the request.",
            fix="Inspect the webhook delivery in /api-integration to confirm the secret is set.",
            doc_url="https://lenz.io/docs/webhooks",
        )
    if not isinstance(raw_body, (bytes, bytearray)):
        raise LenzWebhookSignatureError(
            message="raw_body must be bytes",
            cause="Pass the raw request body, not a string. Decoding may have already mangled it.",
            fix="Use request.body (Flask) / req.rawBody (Express) / req.body_bytes equivalent.",
            doc_url="https://lenz.io/docs/webhooks",
        )

    expected = _sign(bytes(raw_body), secret)
    if not hmac.compare_digest(expected, signature):
        raise LenzWebhookSignatureError(
            message="Webhook signature mismatch",
            cause="HMAC of the raw body using your secret does not match X-Lenz-Signature.",
            fix="Verify the secret in /api-integration matches the one you configured here.",
            doc_url="https://lenz.io/docs/webhooks",
        )
    return True


# ── Typed events ─────────────────────────────────────────────────────────


@dataclass
class WebhookEvent:
    """Base class. Use ``isinstance`` to discriminate the union."""

    event: str
    task_id: str
    attempt: int = 1
    delivered_at: str = ""
    verification_id: str | None = None
    batch_id: str | None = None
    status: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class VerificationCompleted(WebhookEvent):
    """``event=verification.completed`` — the pipeline produced a verdict."""

    result: dict[str, Any] = field(default_factory=dict)


@dataclass
class VerificationFailed(WebhookEvent):
    """``event=verification.failed`` — the pipeline terminated without a verdict."""

    error: str = ""


@dataclass
class VerificationNeedsInput(WebhookEvent):
    """``event=verification.needs_input`` — pipeline paused for caller input.

    Resolve by calling ``client.select(task_id, ...)``. Then a new
    pipeline run produces a ``verification.completed`` (or another
    ``needs_input``) event.
    """

    needs_input: dict[str, Any] = field(default_factory=dict)


def _build_event(payload: dict[str, Any]) -> WebhookEvent:
    """Discriminate on ``event`` and return the right typed dataclass."""
    event = str(payload.get("event") or "")
    common = {
        "event": event,
        "task_id": str(payload.get("task_id") or ""),
        "attempt": int(payload.get("attempt") or 1),
        "delivered_at": str(payload.get("delivered_at") or ""),
        "verification_id": payload.get("verification_id") or None,
        "batch_id": payload.get("batch_id") or None,
        "status": str(payload.get("status") or ""),
        "raw": payload,
    }
    if event == "verification.completed":
        return VerificationCompleted(**common, result=payload.get("result") or {})
    if event == "verification.failed":
        return VerificationFailed(**common, error=str(payload.get("error") or ""))
    if event == "verification.needs_input":
        return VerificationNeedsInput(**common, needs_input=payload.get("needs_input") or {})
    # Unknown event type — return generic. Future-compatible.
    return WebhookEvent(**common)


class LenzWebhooks:
    """Stateful handler bound to a single webhook signing secret.

    Construct once at boot and reuse across requests:

        webhooks = LenzWebhooks(secret=os.environ["LENZ_WEBHOOK_SECRET"])

        @app.post("/lenz-webhook")
        def receive(req):
            event = webhooks.parse(req.body, req.headers)
            match event:
                case VerificationCompleted(verification_id=vid):
                    ...
                case VerificationNeedsInput(task_id=tid):
                    ...

    The ``replay_window_seconds`` knob is mostly an attack-surface
    decision; defaults to 5 minutes which is generous and matches what
    Stripe / Svix recommend.
    """

    def __init__(
        self,
        *,
        secret: str,
        replay_window_seconds: int = DEFAULT_REPLAY_WINDOW_SECONDS,
    ) -> None:
        if not secret:
            raise ValueError("LenzWebhooks requires a non-empty secret. Get it from /api-integration.")
        self._secret = secret
        self._replay_window = replay_window_seconds

    def parse(self, raw_body: bytes, headers: dict[str, str] | Any) -> WebhookEvent:
        """Verify signature + timestamp + deserialise into a typed event.

        ``headers`` can be a plain dict or any mapping that supports
        case-insensitive ``.get`` — covers Flask's ``request.headers``,
        FastAPI's, and stdlib WSGI/ASGI environments.
        """
        sig = self._lookup_header(headers, SIGNATURE_HEADER)
        verify_signature(raw_body, sig, self._secret)

        try:
            payload = json.loads(raw_body.decode("utf-8") if isinstance(raw_body, (bytes, bytearray)) else raw_body)
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as exc:
            raise LenzWebhookSignatureError(
                message="Webhook body is not valid JSON",
                cause=str(exc),
                fix=(
                    "The signature verified but the body is malformed. "
                    "Check your reverse proxy isn't rewriting payloads."
                ),
                doc_url="https://lenz.io/docs/webhooks",
            ) from exc

        if not isinstance(payload, dict):
            raise LenzWebhookSignatureError(
                message="Webhook body must be a JSON object",
                cause=f"Got {type(payload).__name__}.",
                fix="Confirm the request comes from Lenz; an upstream proxy may be wrapping the body.",
                doc_url="https://lenz.io/docs/webhooks",
            )

        self._check_replay(payload)
        return _build_event(payload)

    # ── helpers ──

    @staticmethod
    def _lookup_header(headers: Any, name: str) -> str:
        if headers is None:
            return ""
        # Try the exact case first, then lower-case (most frameworks
        # normalize to lower; some preserve case).
        for key in (name, name.lower(), name.replace("-", "_").upper()):
            try:
                value = headers.get(key) if hasattr(headers, "get") else None
            except Exception:
                value = None
            if value:
                return str(value)
        return ""

    def _check_replay(self, payload: dict[str, Any]) -> None:
        raw_ts = payload.get("delivered_at")
        if not raw_ts:
            return  # No timestamp on the payload — accept and move on.
        try:
            # ISO 8601 — accept trailing 'Z' (UTC)
            ts = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        if age > self._replay_window:
            raise LenzWebhookSignatureError(
                message="Webhook delivered_at is outside the replay window",
                cause=f"Payload is {int(age)}s old; window is {self._replay_window}s.",
                fix=(
                    "Confirm your server clock is in sync; raise replay_window_seconds "
                    "if you intentionally batch deliveries."
                ),
                doc_url="https://lenz.io/docs/webhooks",
            )


__all__ = [
    "SIGNATURE_HEADER",
    "LenzWebhooks",
    "VerificationCompleted",
    "VerificationFailed",
    "VerificationNeedsInput",
    "WebhookEvent",
    "verify_signature",
]
