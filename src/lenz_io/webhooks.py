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

import copy
import hashlib
import hmac
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from .errors import LenzWebhookSignatureError
from .models import (
    CandidateClaim,
    Citecheck,
    FailureBlock,
    ReviewFull,
    _fill_modified_at,
    _old_code,
    _verification_failure,
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

    @property
    def event_id(self) -> str:
        """The same on every delivery attempt of one event: deduplicate on it.
        ``""`` when the payload carries none (the original ``verification.*``
        and ``certificate.*`` payloads)."""
        value = self.raw.get("event_id")
        return value if isinstance(value, str) else ""


@dataclass
class VerificationCompleted(WebhookEvent):
    """``event=verification.completed`` — the pipeline produced a verdict."""

    result: dict[str, Any] = field(default_factory=dict)


@dataclass
class VerificationFailed(WebhookEvent):
    """``event=verification.failed`` — the pipeline terminated without a verdict.

    ``failure_class`` is WHY (closed set — ``upstream_unavailable`` |
    ``insufficient_evidence`` | ``invalid_input`` | ``cancelled`` |
    ``internal``); ``retryable`` is the derived signal (true iff
    ``upstream_unavailable``). Both default when an older server omits them.
    """

    error: str = ""
    failure_class: str = ""
    retryable: bool | None = None

    @property
    def failure(self) -> FailureBlock | None:
        """Why the run failed, from either payload shape: ``code`` (the cause,
        ``no_checkable_claim`` where ``error`` reads ``not_a_claim``),
        ``detail`` (one sentence, newer payloads only), ``failure_class``,
        ``retryable``, ``hint`` and ``docs_url``. ``None`` when the payload
        names no cause."""
        body = self.raw.get("verification")
        block = body.get("failure") if isinstance(body, dict) else None
        if not isinstance(block, dict):
            if not (self.error or self.failure_class):
                return None
            block = {"failure_reason": self.error, "failure_class": self.failure_class, "retryable": self.retryable}
        try:
            return FailureBlock.model_validate(_verification_failure(block))
        except ValidationError:
            return None


@dataclass
class VerificationNeedsInput(WebhookEvent):
    """``event=verification.needs_input`` — pipeline paused for caller input.

    Resolve by calling ``client.select(task_id, ...)``. Then a new
    pipeline run produces a ``verification.completed`` (or another
    ``needs_input``) event.

    ``hint`` is one sentence on what was unclear and how to resolve it
    (``needs_input["hint"]`` on the wire); ``""`` when an older server omits
    it.
    """

    needs_input: dict[str, Any] = field(default_factory=dict)
    hint: str = ""

    @property
    def reason(self) -> str:
        """Why the run paused (``multi_claim``)."""
        value = self.needs_input.get("reason") if isinstance(self.needs_input, dict) else None
        return value if isinstance(value, str) else ""

    @property
    def claims(self) -> list[CandidateClaim]:
        """The options to pick from, each with ``claim`` and ``domain``. An
        option that cannot be read is left out, never raised on."""
        options = self.needs_input.get("claims") if isinstance(self.needs_input, dict) else None
        out = []
        for option in options or []:
            if not isinstance(option, dict):
                continue
            try:
                out.append(CandidateClaim.model_validate({k: v for k, v in option.items() if v is not None}))
            except ValidationError:
                continue
        return out


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
    one event, while ``attempt`` counts up. ``task_id`` identifies the
    delivery and cannot be polled on ``/verify/status``; the newer payload
    shape leaves it out (it then reads ``review_id``). The deep checks a
    review runs send no ``verification.*`` events of their own.
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
    delivery attempt of one event. ``task_id`` identifies the delivery and
    cannot be polled on ``/verify/status``; the newer payload shape leaves it
    out (it then reads ``citecheck_id``).
    """

    event_id: str = ""
    citecheck_id: str = ""
    citecheck: Citecheck | None = None


#: The original ``verification.completed`` ``result``: every key, in order,
#: with the value it took when the verification left it out. A newer payload
#: sends only the keys it has.
_RESULT_DEFAULTS: tuple[tuple[str, Any], ...] = (
    ("verification_id", ""),
    ("claim", ""),
    ("visibility", "private"),
    ("depth", "standard"),
    ("domain", ""),
    ("entities", []),
    ("presumed_intent", ""),
    ("verdict", ""),
    ("confidence", "low"),
    ("lenz_score", None),
    ("key_finding", ""),
    ("executive_summary", ""),
    ("warnings", []),
    ("suggested_rewrite", None),
    ("created_at", ""),
    ("modified_at", None),
    ("sources", []),
    ("audit", None),
    ("language", "en"),
    ("coverage", None),
)
_AUDIT_DEFAULTS: tuple[tuple[str, Any], ...] = (
    ("adjudication_summary", ""),
    ("assessments", []),
    ("debate_pro", None),
    ("debate_con", None),
    ("panel_agreement", ""),
)
_SIDE_DEFAULTS: tuple[tuple[str, Any], ...] = (("role", ""), ("argument", ""), ("rebuttal", ""))


def _with_defaults(value: Any, defaults: tuple[tuple[str, Any], ...]) -> dict[str, Any]:
    """``value`` (a dict, else empty) with every key of ``defaults`` in that
    order, then any key it carries beyond them."""
    given = value if isinstance(value, dict) else {}
    out = {key: given.get(key, copy.deepcopy(default)) for key, default in defaults}
    out.update({k: v for k, v in given.items() if k not in out})
    return out


def _original_result(result: dict[str, Any]) -> dict[str, Any]:
    """A newer payload's ``result`` with the original's keys and defaults
    (``modified_at`` computed from ``completed_at``); keys the original did
    not have (``completed_at``) follow them."""
    out = _with_defaults(_fill_modified_at(result), _RESULT_DEFAULTS)
    audit = _with_defaults(out["audit"], _AUDIT_DEFAULTS)
    audit["debate_pro"] = _with_defaults(audit["debate_pro"], _SIDE_DEFAULTS)
    audit["debate_con"] = _with_defaults(audit["debate_con"], _SIDE_DEFAULTS)
    out["audit"] = audit
    return out


def _original_view(payload: dict[str, Any]) -> dict[str, Any]:
    """The original flat ``verification.*`` fields, for a payload in the newer
    envelope (the polled body nested under ``verification``). Any other
    payload is returned as it is."""
    body = payload.get("verification")
    if not isinstance(body, dict):
        return payload
    view = dict(payload)
    view.setdefault("task_id", body.get("task_id"))
    result = body.get("result")
    if isinstance(result, dict):
        view.setdefault("result", _original_result(result))
        view.setdefault("verification_id", result.get("verification_id"))
    failure = body.get("failure")
    if isinstance(failure, dict):
        view.setdefault("error", _old_code(failure.get("code"), "not_a_claim"))
        view.setdefault("failure_class", failure.get("failure_class"))
        view.setdefault("retryable", failure.get("retryable"))
    if body.get("status") == "needs_input":
        options = [
            {"text": o.get("claim", o.get("text")), "domain": o.get("domain", "")}
            for o in body.get("claims") or []
            if isinstance(o, dict)
        ]
        view.setdefault(
            "needs_input", {"reason": body.get("reason", ""), "claims": options, "hint": body.get("hint", "")}
        )
    view.setdefault("coverage", body.get("coverage") or (result or {}).get("coverage"))
    return view


def _build_event(raw: dict[str, Any]) -> WebhookEvent:
    """Discriminate on ``event`` and return the right typed dataclass."""
    payload = _original_view(raw)
    event = str(payload.get("event") or "")
    task_id = str(payload.get("task_id") or "")
    try:
        attempt = int(payload.get("attempt") or 1)
    except (TypeError, ValueError):
        attempt = 1
    delivered_at = str(payload.get("delivered_at") or "")
    verification_id = str(payload["verification_id"]) if payload.get("verification_id") else None
    batch_id = str(payload["batch_id"]) if payload.get("batch_id") else None
    status = str(payload.get("status") or "")
    if event == "verification.completed":
        return VerificationCompleted(
            event=event,
            task_id=task_id,
            attempt=attempt,
            delivered_at=delivered_at,
            verification_id=verification_id,
            batch_id=batch_id,
            status=status,
            raw=raw,
            result=payload.get("result") or {},
        )
    if event == "verification.failed":
        return VerificationFailed(
            event=event,
            task_id=task_id,
            attempt=attempt,
            delivered_at=delivered_at,
            verification_id=verification_id,
            batch_id=batch_id,
            status=status,
            raw=raw,
            error=str(payload.get("error") or ""),
            failure_class=str(payload.get("failure_class") or ""),
            retryable=payload.get("retryable") if isinstance(payload.get("retryable"), bool) else None,
        )
    if event == "verification.needs_input":
        needs_input = payload.get("needs_input") or {}
        return VerificationNeedsInput(
            event=event,
            task_id=task_id,
            attempt=attempt,
            delivered_at=delivered_at,
            verification_id=verification_id,
            batch_id=batch_id,
            status=status,
            raw=raw,
            needs_input=needs_input,
            hint=str(needs_input.get("hint") or "") if isinstance(needs_input, dict) else "",
        )
    if event == "certificate.timestamped":
        return CertificateTimestamped(
            event=event,
            task_id=task_id,
            attempt=attempt,
            delivered_at=delivered_at,
            verification_id=verification_id,
            batch_id=batch_id,
            status=status,
            raw=raw,
            coverage=payload.get("coverage") or {},
        )
    if event in ("review.completed", "review.failed"):
        body = payload.get("review")
        try:
            review = ReviewFull.model_validate(body) if isinstance(body, dict) else None
        except ValueError:
            review = None
        return ReviewEvent(
            event=event,
            # The newer payload carries no delivery ``task_id``: the review's
            # id stands in, so code keyed on ``task_id`` keeps one key per review.
            task_id=task_id if "task_id" in raw else str(payload.get("review_id") or ""),
            attempt=attempt,
            delivered_at=delivered_at,
            verification_id=verification_id,
            batch_id=batch_id,
            status=status,
            raw=raw,
            event_id=str(payload.get("event_id") or ""),
            review_id=str(payload.get("review_id") or ""),
            review=review,
        )
    if event in ("citecheck.completed", "citecheck.failed"):
        body = payload.get("citecheck")
        try:
            check = Citecheck.model_validate(body) if isinstance(body, dict) else None
        except ValueError:
            check = None
        return CitecheckEvent(
            event=event,
            # As on a review: the check's id stands in for the missing ``task_id``.
            task_id=task_id if "task_id" in raw else str(payload.get("citecheck_id") or ""),
            attempt=attempt,
            delivered_at=delivered_at,
            verification_id=verification_id,
            batch_id=batch_id,
            status=status,
            raw=raw,
            event_id=str(payload.get("event_id") or ""),
            citecheck_id=str(payload.get("citecheck_id") or ""),
            citecheck=check,
        )
    # Unknown event type — return generic. Future-compatible: ignore events
    # you do not handle rather than failing the delivery.
    return WebhookEvent(
        event=event,
        task_id=task_id,
        attempt=attempt,
        delivered_at=delivered_at,
        verification_id=verification_id,
        batch_id=batch_id,
        status=status,
        raw=raw,
    )


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
