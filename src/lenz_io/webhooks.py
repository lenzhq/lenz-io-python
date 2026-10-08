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
``/api-credentials`` page. Rotating the secret on Lenz's side invalidates
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
from .models import (
    CandidateClaim,
    Citecheck,
    FailureBlock,
    ReviewFull,
    _new_code,
    _old_code,
    _with_completion_times,
)

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
            fix="Inspect the webhook delivery in /api-credentials to confirm the secret is set.",
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
            fix="Verify the secret in /api-credentials matches the one you configured here.",
            doc_url="https://lenz.io/docs/webhooks",
        )
    return True


# ── Typed events ─────────────────────────────────────────────────────────
#
# Two payload shapes reach the same events. The original one is flat for
# ``verification.*`` (``result``, ``needs_input``, ``error`` at the top) and
# nested for ``review.*`` / ``citecheck.*``. The newer one is one envelope for
# every event: ``event``, ``event_id``, the work's id, ``status``, and the body
# polling returns under the work's kind (``verification``, ``review``,
# ``citecheck``). Every attribute below is filled from either shape, and the
# original attributes keep their original meaning.


@dataclass
class WebhookEvent:
    """Base class. Use ``isinstance`` to discriminate the union.

    ``event_id`` is the same on every delivery attempt of one event, while
    ``attempt`` counts up: deduplicate on it. ``""`` when a payload does not
    carry it (the original ``verification.*`` and ``certificate.*`` shape).
    """

    event: str
    task_id: str
    attempt: int = 1
    delivered_at: str = ""
    verification_id: str | None = None
    batch_id: str | None = None
    status: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)
    # Keyword-only, so positional construction of every event keeps its order.
    event_id: str = field(default="", kw_only=True)


@dataclass
class VerificationCompleted(WebhookEvent):
    """``event=verification.completed`` — the pipeline produced a verdict.

    ``result`` is the verification as a dict, the same body
    ``verifications.get`` returns, as sent; from the newer payload shape,
    which sends ``completed_at``, the deprecated ``modified_at`` is added.
    """

    result: dict[str, Any] = field(default_factory=dict)


@dataclass
class VerificationFailed(WebhookEvent):
    """``event=verification.failed`` — the pipeline terminated without a verdict.

    ``failure`` says why: ``failure.code`` is the cause (an open set),
    ``failure.detail`` one sentence on what happened, ``failure.failure_class``
    the closed set (``upstream_unavailable`` | ``insufficient_evidence`` |
    ``invalid_input`` | ``cancelled`` | ``internal``) and ``failure.retryable``
    the derived signal (true iff ``upstream_unavailable``).

    The original attributes keep their meaning: ``error`` is the cause code
    (``not_a_claim`` where ``failure.code`` reads ``no_checkable_claim``),
    and ``failure_class`` / ``retryable`` repeat the block's. They default when
    an older server omits them.
    """

    error: str = ""
    failure_class: str = ""
    retryable: bool | None = None
    failure: FailureBlock | None = None


@dataclass
class VerificationNeedsInput(WebhookEvent):
    """``event=verification.needs_input`` — pipeline paused for caller input.

    Resolve by calling ``client.select(task_id, ...)``. Then a new
    pipeline run produces a ``verification.completed`` (or another
    ``needs_input``) event.

    ``reason`` is why (``multi_claim``), ``claims`` the options to pick from
    (each with ``claim`` and ``domain``) and ``hint`` one sentence on what was
    unclear and how to resolve it (``""`` when an older server omits it).
    ``needs_input`` is the original dict, ``{"reason", "claims", "hint"}``,
    whose options carry the claim as ``text``: as sent, or built from the
    newer payload shape.
    """

    needs_input: dict[str, Any] = field(default_factory=dict)
    hint: str = ""
    reason: str = ""
    claims: list[CandidateClaim] = field(default_factory=list)


@dataclass
class CertificateTimestamped(WebhookEvent):
    """``event=certificate.timestamped`` — the qualified timestamp landed.

    **This is the event to publish on, not ``verification.completed``.** The
    warranty requires the certificate's timestamp to PRECEDE what you publish
    or send, so a pipeline that publishes on `completed` races the anchor and
    can put the statement out before cover exists. `completed` says a verdict
    was produced; this says the qualified timestamp is in hand and cover is in
    force.

    Carries ``coverage`` INSTEAD of ``result``: the event reports that a
    timestamp landed, not that a verdict was produced, so ``result`` is null
    on this event and reading it will not give you the verification.
    """

    coverage: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReviewEvent(WebhookEvent):
    """``event=review.completed`` or ``review.failed`` — a review ended.

    ``review`` is the final review (the same body ``get_review`` returns),
    or ``None`` if it could not be parsed (``raw["review"]`` still has it).
    ``status`` is ``completed`` or ``failed``; read ``review.outcome`` and
    ``review.issues``.

    Deduplicate on ``event_id``: it is the same on every delivery attempt of
    one event, while ``attempt`` counts up. ``task_id`` (deprecated)
    identifies the delivery and cannot be polled on ``/verify/status``; it is
    ``""`` on a payload that does not carry it (the newer shape). The deep
    checks a review runs send no ``verification.*`` events of their own.
    """

    event_id: str = ""
    review_id: str = ""
    review: ReviewFull | None = None


@dataclass
class CitecheckEvent(WebhookEvent):
    """``event=citecheck.completed`` or ``citecheck.failed`` — a citation
    check ended.

    ``citecheck`` is the final check (the same body ``get_citecheck``
    returns), or ``None`` if it could not be parsed (``raw["citecheck"]``
    still has it). Deduplicate on ``event_id``: it is the same on every
    delivery attempt of one event. ``task_id`` (deprecated) identifies the
    delivery and cannot be polled on ``/verify/status``; it is ``""`` on a
    payload that does not carry it (the newer shape).
    """

    event_id: str = ""
    citecheck_id: str = ""
    citecheck: Citecheck | None = None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _opt_id(*values: Any) -> str | None:
    for value in values:
        if value:
            return str(value)
    return None


def _legacy_needs_input(payload: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    """The original ``needs_input`` dict: as sent, or built from the newer
    envelope's polled body (each option carrying the claim as ``text``)."""
    if isinstance(payload.get("needs_input"), dict):
        return payload["needs_input"]  # type: ignore[no-any-return]
    claims = []
    for option in body.get("claims") or []:
        if isinstance(option, dict):
            claims.append({"text": option.get("claim", option.get("text")), "domain": option.get("domain", "")})
    out: dict[str, Any] = {"reason": body.get("reason", "")}
    out["claims"] = claims
    out["hint"] = body.get("hint", "")
    return out


def _build_event(payload: dict[str, Any]) -> WebhookEvent:
    """Discriminate on ``event`` and return the right typed dataclass."""
    event = str(payload.get("event") or "")
    # The newer envelope nests the polled body under the work's kind.
    body = _dict(payload.get("verification"))
    result = _dict(payload.get("result")) or _dict(body.get("result"))
    event_id = str(payload.get("event_id") or "")
    task_id = str(payload.get("task_id") or body.get("task_id") or "")
    try:
        attempt = int(payload.get("attempt") or 1)
    except (TypeError, ValueError):
        attempt = 1
    delivered_at = str(payload.get("delivered_at") or "")
    verification_id = _opt_id(payload.get("verification_id"), result.get("verification_id"))
    batch_id = _opt_id(payload.get("batch_id"), body.get("batch_id"))
    status = str(payload.get("status") or body.get("status") or "")
    common: dict[str, Any] = {
        "event": event,
        "task_id": task_id,
        "attempt": attempt,
        "delivered_at": delivered_at,
        "verification_id": verification_id,
        "batch_id": batch_id,
        "status": status,
        "raw": payload,
        "event_id": event_id,
    }
    if event == "verification.completed":
        if result and "modified_at" not in result:
            # The newer shape: add the original ``modified_at`` (computed),
            # leaving an original-shape result exactly as sent.
            result = _with_completion_times(result)
        return VerificationCompleted(**common, result=result)
    if event == "verification.failed":
        block = _dict(body.get("failure")) or _dict(payload.get("failure"))
        failure = FailureBlock.model_validate(block) if block else None
        legacy_error = payload.get("error")
        error = legacy_error if isinstance(legacy_error, str) and legacy_error else ""
        if not error and failure is not None:
            error = str(_old_code(failure.code, "not_a_claim") or "")
        failure_class = payload.get("failure_class") or (failure.failure_class if failure else "") or ""
        retryable = payload.get("retryable")
        if not isinstance(retryable, bool):
            retryable = failure.retryable if failure is not None else None
        if failure is None and (error or failure_class):
            # The original shape: the block from the flat fields.
            failure = FailureBlock.model_validate(
                {"code": _new_code(error), "failure_class": failure_class, "retryable": retryable}
            )
        return VerificationFailed(
            **common,
            error=error,
            failure_class=str(failure_class),
            retryable=retryable if isinstance(retryable, bool) else None,
            failure=failure,
        )
    if event == "verification.needs_input":
        needs_input = _legacy_needs_input(payload, body)
        return VerificationNeedsInput(
            **common,
            needs_input=needs_input,
            hint=str(needs_input.get("hint") or ""),
            reason=str(needs_input.get("reason") or ""),
            claims=[CandidateClaim.model_validate(c) for c in needs_input.get("claims") or [] if isinstance(c, dict)],
        )
    if event == "certificate.timestamped":
        coverage = _dict(payload.get("coverage")) or _dict(body.get("coverage")) or _dict(result.get("coverage"))
        return CertificateTimestamped(**common, coverage=coverage)
    if event in ("review.completed", "review.failed"):
        review_body = payload.get("review")
        try:
            review = ReviewFull.model_validate(review_body) if isinstance(review_body, dict) else None
        except ValueError:
            review = None
        return ReviewEvent(
            **common,
            review_id=str(payload.get("review_id") or _dict(review_body).get("review_id") or ""),
            review=review,
        )
    if event in ("citecheck.completed", "citecheck.failed"):
        check_body = payload.get("citecheck")
        try:
            check = Citecheck.model_validate(check_body) if isinstance(check_body, dict) else None
        except ValueError:
            check = None
        return CitecheckEvent(
            **common,
            citecheck_id=str(payload.get("citecheck_id") or _dict(check_body).get("citecheck_id") or ""),
            citecheck=check,
        )
    # Unknown event type — return generic. Future-compatible: ignore events
    # you do not handle rather than failing the delivery.
    return WebhookEvent(**common)


def parse_webhook(body: bytes | str | dict[str, Any]) -> WebhookEvent:
    """Parse a webhook body into its typed event, WITHOUT checking the signature.

    For a body whose signature you have already checked (with
    :func:`verify_signature`), or one you stored. For a request as it arrives,
    use :meth:`LenzWebhooks.parse`, which checks the signature and the replay
    window first.

    Returns a :class:`ReviewEvent` for ``review.*``, a :class:`CitecheckEvent`
    for ``citecheck.*``, the matching verification
    or certificate event otherwise, and a plain :class:`WebhookEvent` for an
    event type this release does not know. Branch with ``isinstance`` and
    ignore events you do not handle.
    """
    if isinstance(body, dict):
        payload: Any = body
    else:
        payload = json.loads(body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else body)
    if not isinstance(payload, dict):
        raise ValueError(f"A webhook body is a JSON object, got {type(payload).__name__}.")
    return _build_event(payload)


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
            raise ValueError("LenzWebhooks requires a non-empty secret. Get it from /api-credentials.")
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
    "CitecheckEvent",
    "LenzWebhooks",
    "ReviewEvent",
    "VerificationCompleted",
    "VerificationFailed",
    "VerificationNeedsInput",
    "WebhookEvent",
    "parse_webhook",
    "verify_signature",
]
