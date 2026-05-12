"""Public Lenz client — the ergonomic top-level surface.

Shape:

    from lenz_io import Lenz
    client = Lenz(api_key="lenz_...")

    # Marquee verbs — top-level
    v = client.verify_and_wait(claim="Sharks don't get cancer")
    v = client.verify(claim="...")          # async submit; returns task_id
    out = client.extract(text="...")
    batch = client.verify_batch(claims=[...])
    status = client.get_status(task_id)
    client.select(task_id, claim_index=0)

    # Resource namespaces
    client.verifications.list()
    client.verifications.get(id) / delete(id) / set_visibility(id, "public")
    client.followup.history(id) / send(id, message=...) / reset(id)
    client.library.list() / get(id)
    client.usage()

Design decisions:

* Single persistent ``httpx.Client`` per ``Lenz`` instance for HTTP
  keep-alive (saves ~80ms TLS handshake per call on warm pool). Use as
  a context manager for clean shutdown, or call ``close()`` explicitly.
* Exponential backoff on transient errors (5xx, 429). 3 retry attempts
  by default. ``Retry-After`` honored on 429s.
* ``Idempotency-Key`` auto-generated for ``verify_and_wait`` so a network
  drop during submit doesn't spawn duplicate tasks. Customer can override
  with explicit ``idempotency_key=...``.
* ``X-Lenz-API-Version`` header pinned at SDK release date so the server
  can route old clients to v1 handlers when v2 ships.
* ``X-Request-ID`` is captured from every response onto the typed error
  for support escalation.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any

import httpx

from . import __version__
from .errors import (
    LenzAPIError,
    LenzError,
    LenzNeedsInputError,
    LenzPipelineError,
    LenzTimeoutError,
    map_response_to_error,
)
from .models import (
    BatchAccepted,
    ExtractedClaims,
    FollowupHistory,
    FollowupReply,
    LibraryItem,
    LibraryList,
    TaskAccepted,
    TaskStatus,
    Usage,
    Verification,
    VerificationList,
)

logger = logging.getLogger("lenz_io")

# Pin the API version the SDK was built against. The server logs it on
# every request; when v2 ships, old SDKs keep getting v1 behavior.
API_VERSION = "2026-05-13"

DEFAULT_BASE_URL = "https://lenz.io/api/v1"
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RETRIES = 3
RETRY_BACKOFF = (1.0, 2.0, 4.0)
POLL_BACKOFF = (2.0, 4.0, 8.0)
POLL_BACKOFF_CAP = 10.0


def _user_agent() -> str:
    return f"lenz-io-python/{__version__} (httpx {httpx.__version__})"


class _VerificationsNamespace:
    """``client.verifications.{list,get,delete,set_visibility}``."""

    def __init__(self, parent: Lenz) -> None:
        self._p = parent

    def list(self, *, page: int = 1) -> VerificationList:
        body = self._p._request("GET", "/verifications", params={"page": page})
        return VerificationList.model_validate(body)

    def get(self, verification_id: str) -> Verification:
        body = self._p._request("GET", f"/verifications/{verification_id}")
        return Verification.model_validate(body)

    def delete(self, verification_id: str) -> bool:
        """Idempotent. Retry-on-404 returns True ("already deleted")."""
        try:
            self._p._request("DELETE", f"/verifications/{verification_id}")
            return True
        except LenzError as exc:
            # Idempotent DELETE: if the row was already gone (e.g. previous
            # request succeeded but the network reply was lost), treat as
            # success rather than surfacing a confusing 404.
            if exc.status_code == 404:
                return True
            raise

    def set_visibility(self, verification_id: str, visibility: str) -> dict[str, Any]:
        body = self._p._request(
            "PATCH",
            f"/verifications/{verification_id}/visibility",
            json={"visibility": visibility},
        )
        return body  # {"ok": True, "visibility": "public"}


class _FollowupNamespace:
    """``client.followup.{history,send,reset}``."""

    def __init__(self, parent: Lenz) -> None:
        self._p = parent

    def history(self, verification_id: str) -> FollowupHistory:
        body = self._p._request("GET", f"/verifications/{verification_id}/follow-up")
        return FollowupHistory.model_validate(body)

    def send(self, verification_id: str, *, message: str) -> FollowupReply:
        body = self._p._request(
            "POST",
            f"/verifications/{verification_id}/follow-up",
            json={"message": message},
        )
        return FollowupReply.model_validate(body)

    def reset(self, verification_id: str) -> bool:
        self._p._request("DELETE", f"/verifications/{verification_id}/follow-up")
        return True


class _LibraryNamespace:
    """``client.library.{list,get}``. Works without an API key."""

    def __init__(self, parent: Lenz) -> None:
        self._p = parent

    def list(
        self,
        *,
        page: int = 1,
        sort: str = "recent",
        search: str = "",
        domain: str = "",
        entity: str = "",
    ) -> LibraryList:
        body = self._p._request(
            "GET",
            "/library",
            params={
                "page": page,
                "sort": sort,
                "search": search,
                "domain": domain,
                "entity": entity,
            },
            auth_required=False,
        )
        return LibraryList.model_validate(body)

    def get(self, verification_id: str) -> LibraryItem:
        body = self._p._request(
            "GET",
            f"/library/{verification_id}",
            auth_required=False,
        )
        return LibraryItem.model_validate(body)


class Lenz:
    """Top-level client.

    The constructor accepts ``api_key=None`` so library endpoints work
    without authentication (sandbox path for developers exploring before
    sign-up). Auth-required methods on an un-keyed client raise
    ``LenzAuthError`` with a link to ``/api-integration``.

    Reads ``LENZ_API_KEY`` from the environment if no key is passed.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("LENZ_API_KEY") or ""
        self._base_url = (base_url or os.environ.get("LENZ_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(
            timeout=httpx.Timeout(timeout),
            headers={
                "User-Agent": _user_agent(),
                "X-Lenz-API-Version": API_VERSION,
                "Accept": "application/json",
            },
        )

        # Resource namespaces (Stripe pattern for CRUD on past verifications,
        # follow-up conversations, and the public library)
        self.verifications = _VerificationsNamespace(self)
        self.followup = _FollowupNamespace(self)
        self.library = _LibraryNamespace(self)

    # ── lifecycle ──

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Lenz:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # ── marquee verbs (top-level shortcuts) ──

    def verify(self, claim: str, **kwargs: Any) -> TaskAccepted:
        """Submit a claim for verification. Returns a ``task_id``; the
        pipeline runs async. For sync ergonomics use ``verify_and_wait``.
        """
        return self._verify_submit(claim=claim, **kwargs)

    def verify_batch(self, **kwargs: Any) -> BatchAccepted:
        """Submit multiple claims in one call. Returns a ``batch_id`` and
        per-claim ``task_id``s. Each item has its own lifecycle and webhook.
        """
        return self._verify_batch(**kwargs)

    def extract(self, **kwargs: Any) -> ExtractedClaims:
        """Pull the verifiable claims out of any text. Sync, free, capped at
        1000 calls/key/day.
        """
        return self._extract(**kwargs)

    def select(self, task_id: str, *, text: str = "", claim_index: int | None = None) -> TaskAccepted:
        """Resolve a needs-input interrupt by selecting / clarifying the claim.

        Pass either ``text=`` (the resolved claim wording) or
        ``claim_index=`` (0-based index into the prior status's claims list).
        Spawns a new pipeline task; the returned ``task_id`` is the one to
        poll going forward.
        """
        if not text and claim_index is None:
            raise ValueError("select requires either text= or claim_index=")
        return self._select(task_id, text=text, claim_index=claim_index)

    def get_status(self, task_id: str) -> TaskStatus:
        """Poll the pipeline status. Use ``verify_and_wait`` for sync ergonomics."""
        return self._get_status(task_id)

    # ── headline ergonomic ──

    def verify_and_wait(
        self,
        claim: str,
        *,
        source_url: str = "",
        webhook_url: str = "",
        timeout: float = 120.0,
        idempotency: bool = True,
        idempotency_key: str | None = None,
    ) -> Verification:
        """Submit + poll until the pipeline terminates.

        Returns the completed ``Verification`` on success. Raises:
          * ``LenzNeedsInputError`` if the pipeline pauses (multi_claim /
            clarification_required / duplicate_found). Resolve via
            ``client.select(task_id, ...)`` then re-call this helper on
            the new task.
          * ``LenzPipelineError`` on terminal failure.
          * ``LenzTimeoutError`` if ``timeout`` elapses; the task may
            still finish server-side — recover via
            ``client.get_status(task_id)``.

        ``idempotency=True`` (default) auto-generates a key per call so a
        network drop after submit doesn't spawn a duplicate verification
        on retry. Customer can pin via ``idempotency_key="..."``.
        """
        key = idempotency_key
        if key is None and idempotency:
            key = uuid.uuid4().hex
        accepted = self._verify_submit(claim=claim, source_url=source_url, webhook_url=webhook_url, idempotency_key=key)
        task_id = accepted.task_id
        logger.info("Submitted task: %s", task_id)

        deadline = time.monotonic() + timeout
        backoff_idx = 0
        while True:
            status = self._get_status(task_id)
            if status.status == "completed":
                if status.result is None:
                    raise LenzPipelineError(
                        message="Pipeline completed but the result is empty.",
                        cause="Server reported status=completed without a result block.",
                        fix="File an issue at https://github.com/lenzhq/lenz-io-python/issues with the Request ID.",
                        doc_url="https://lenz.io/docs/errors",
                        task_id=task_id,
                    )
                return status.result
            if status.status == "needs_input":
                raise LenzNeedsInputError(
                    message=f"Pipeline paused: {status.reason}",
                    cause="The verification needs caller input to proceed.",
                    fix="Inspect the payload, then call client.select(task_id, claim_index=...) (or .text=...).",
                    doc_url="https://lenz.io/docs/verify#needs-input",
                    task_id=task_id,
                    kind=status.reason,
                    payload=status.model_dump(),
                )
            if status.status == "failed":
                raise LenzPipelineError(
                    message=f"Pipeline failed: {status.failure_reason or 'unknown'}",
                    cause=status.failure_detail or status.failure_reason or "Unknown failure.",
                    fix="Retry with a different claim, or check status.failure_reason for the diagnostic.",
                    doc_url="https://lenz.io/docs/errors",
                    task_id=task_id,
                    failure_reason=status.failure_reason,
                )

            # processing — sleep + backoff
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LenzTimeoutError(
                    message=f"verify_and_wait timed out after {timeout}s",
                    cause="Pipeline still running server-side.",
                    fix=f"Resume via client.get_status('{task_id}') later.",
                    doc_url="https://lenz.io/docs/verify#timeout",
                    task_id=task_id,
                )
            sleep_for = min(POLL_BACKOFF[min(backoff_idx, len(POLL_BACKOFF) - 1)], POLL_BACKOFF_CAP)
            sleep_for = min(sleep_for, remaining)
            time.sleep(sleep_for)
            backoff_idx += 1

    # ── account ──

    def usage(self) -> Usage:
        body = self._request("GET", "/me/usage")
        return Usage.model_validate(body)

    # ── verb-level submit helpers (used by the verify namespace) ──

    def _verify_submit(
        self,
        *,
        claim: str = "",
        text: str = "",
        source_url: str = "",
        webhook_url: str = "",
        visibility: str = "",
        idempotency_key: str | None = None,
    ) -> TaskAccepted:
        payload = {
            "text": claim or text,
            "source_url": source_url,
            "webhook_url": webhook_url,
            "visibility": visibility,
        }
        headers = {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        body = self._request("POST", "/verify", json=payload, headers=headers)
        return TaskAccepted.model_validate(body)

    def _verify_batch(
        self,
        *,
        claims: list[dict[str, Any]],
        webhook_url: str = "",
        idempotency_key: str | None = None,
    ) -> BatchAccepted:
        payload = {"claims": claims, "webhook_url": webhook_url}
        headers = {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        body = self._request("POST", "/verify/batch", json=payload, headers=headers)
        return BatchAccepted.model_validate(body)

    def _extract(self, *, text: str, country: str = "", city: str = "") -> ExtractedClaims:
        body = self._request("POST", "/extract", json={"text": text, "country": country, "city": city})
        return ExtractedClaims.model_validate(body)

    def _select(self, task_id: str, *, text: str = "", claim_index: int | None = None) -> TaskAccepted:
        payload: dict[str, Any] = {}
        if text:
            payload["text"] = text
        if claim_index is not None:
            payload["claim_index"] = claim_index
        body = self._request("POST", f"/verify/{task_id}/select", json=payload)
        return TaskAccepted.model_validate(body)

    def _get_status(self, task_id: str) -> TaskStatus:
        body = self._request("GET", f"/verify/status/{task_id}")
        return TaskStatus.model_validate(body)

    # ── HTTP plumbing ──

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        auth_required: bool = True,
    ) -> dict[str, Any]:
        if auth_required and not self._api_key:
            from .errors import LenzAuthError

            raise LenzAuthError(
                message="API key required",
                cause="This method requires authentication; no API key was provided.",
                fix=(
                    "Pass api_key= to Lenz(), set LENZ_API_KEY env var, or get one at "
                    "https://lenz.io/api-integration. Library endpoints work without a key."
                ),
                doc_url="https://lenz.io/docs/auth",
            )

        url = f"{self._base_url}{path}"
        req_headers = dict(headers or {})
        if self._api_key and auth_required:
            req_headers["Authorization"] = f"Bearer {self._api_key}"
        req_headers.setdefault("Content-Type", "application/json")

        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.request(method, url, json=json, params=params, headers=req_headers)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                if attempt >= self._max_retries:
                    raise LenzAPIError(
                        message=f"{method} {path} failed after {attempt + 1} attempts: {exc}",
                        cause=str(exc),
                        fix="Check your network connection; verify base_url is reachable.",
                        doc_url="https://lenz.io/docs/errors",
                    ) from exc
                time.sleep(_retry_sleep(attempt))
                continue

            if response.status_code < 400:
                return response.json() if response.content else {}

            # Error path. Retry on 5xx and 429; otherwise raise immediately.
            if attempt < self._max_retries and (response.status_code >= 500 or response.status_code == 429):
                ra = response.headers.get("Retry-After")
                try:
                    sleep_for = int(ra) if ra else _retry_sleep(attempt)
                except ValueError:
                    sleep_for = _retry_sleep(attempt)
                time.sleep(sleep_for)
                continue

            raise map_response_to_error(
                response.status_code,
                response.content,
                dict(response.headers),
            )

        # Shouldn't reach here, but guard.
        if last_exc:
            raise LenzAPIError(message=str(last_exc), cause=str(last_exc)) from last_exc
        raise LenzAPIError(message=f"{method} {path} failed without diagnostic")


def _retry_sleep(attempt: int) -> float:
    if attempt < len(RETRY_BACKOFF):
        return RETRY_BACKOFF[attempt]
    return RETRY_BACKOFF[-1]


__all__ = ["API_VERSION", "DEFAULT_BASE_URL", "Lenz"]
