"""Public Lenz client — the ergonomic top-level surface.

Multi-language SDK convention:
* Request methods (verify, assess, extract, ask, …) take ``language=''``
  as their default. Sending an empty string means "do NOT include the
  field in the request body" — preserves byte-identical behavior for
  existing English callers. Set ``language='es'`` (or any of the 12
  supported codes) to receive prose fields in that language.
* Response models (``Verification``, ``AssessClaim``, ``VerificationListItem``)
  expose ``language`` populated by the server. Verdict / domain / status
  enum values stay English regardless of language; only free-form prose
  (atomic_claim, executive_summary, audit text) follows the request.
* Mixing the two (e.g. ``language='en'`` on a request) would send an
  extra ``"language": "en"`` key on every English call — breaks the
  byte-identical English path. The empty-default-then-omit convention
  exists precisely to avoid that.

Shape (four-primitive ladder + the supporting reads):

    from lenz_io import Lenz
    client = Lenz(api_key="lenz_...")

    # Marquee verbs — top-level (the four-primitive ladder)
    out = client.extract(text="...")                       # find claims
    r = client.assess(text="...")                          # fast 3-model verdict, ~5-10s
    v = client.verify_and_wait(claim="...")                # full 8-model pipeline, ~90s
    reply = client.ask.send(id, message="follow-up?")      # Q&A on a verification

    # Other verify-family verbs
    v = client.verify(claim="...")          # async submit; returns task_id
    result = client.wait(v)                  # block on a task_id / TaskAccepted
    batch = client.verify_batch(claims=[...])
    results = client.verify_batch_and_wait(claims=[...])   # submit + poll all
    status = client.get_status(task_id)      # single non-blocking poll
    client.select(task_id, texts=["The earth is flat."])  # pick one or more

    # Resource namespaces
    client.verifications.list()
    client.verifications.get(id) / delete(id)
    client.ask.history(id) / send(id, message=...) / reset(id)
    client.library.list()
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
from typing import Any, TypedDict

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
    AskHistory,
    AskReply,
    AssessResponse,
    BatchAccepted,
    BatchItemResult,
    ExtractedClaims,
    LibraryList,
    RelatedVerifications,
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


class VerifyBatchItem(TypedDict, total=False):
    """Per-item shape for ``verify_batch``.

    All fields optional — callers can pass any subset. Type-only:
    the SDK accepts plain dicts at runtime and does no Pydantic
    coercion. The TypedDict exists purely so IDEs autocomplete the
    per-item keys (``text``, ``language``, …).

    Precedence on conflicting language: per-item ``language`` overrides
    the batch-wide ``language`` kwarg on ``verify_batch``, which overrides
    the implicit English default. The SDK forwards both verbatim; the
    server is authoritative on the merge.
    """

    text: str
    language: str
    source_url: str
    webhook_url: str
    idempotency_key: str


class _VerificationsNamespace:
    """``client.verifications.{list,get,delete,related}``."""

    def __init__(self, parent: Lenz) -> None:
        self._p = parent

    def list(self, *, page: int = 1) -> VerificationList:
        body = self._p._request("GET", "/verifications", params={"page": page})
        return VerificationList.model_validate(body)

    def get(self, verification_id: str) -> Verification:
        """Fetch a single verification by id.

        Works without an API key — the server accepts optional Bearer:
        anon callers see any public + non-hidden claim, authed callers
        additionally see their own at any visibility / status.
        """
        body = self._p._request(
            "GET",
            f"/verifications/{verification_id}",
            auth_required=False,
            auth_optional=True,  # send the key if we have one → owner sees private rows
        )
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

    def related(self, verification_id: str, *, limit: int = 5) -> RelatedVerifications:
        """Return public verifications semantically related to this one.

        Server caps ``limit`` to 10. Empty list when the verification has
        no embedding yet or no claim is close enough. Excludes the
        verification itself and editorially-hidden claims. Accessible for
        any verification the caller owns (any visibility) or any public
        library item.
        """
        body = self._p._request(
            "GET",
            f"/verifications/{verification_id}/related",
            params={"limit": limit},
        )
        return RelatedVerifications.model_validate(body)


class _AskNamespace:
    """``client.ask.{history,send,reset}``.

    The endpoint moved from ``/verifications/{id}/follow-up`` to a flat
    ``/ask/{id}`` server-side; this namespace tracks the new URL shape.
    """

    def __init__(self, parent: Lenz) -> None:
        self._p = parent

    def history(self, verification_id: str) -> AskHistory:
        body = self._p._request("GET", f"/ask/{verification_id}")
        return AskHistory.model_validate(body)

    def send(self, verification_id: str, *, message: str, language: str = "") -> AskReply:
        """Send a follow-up question on an existing verification.

        ``language`` (optional) overrides the claim's stored language for
        this single reply. Omit to let the server use the claim's
        ``language`` as default — that's the typical case.
        """
        payload: dict[str, Any] = {"message": message}
        if language:
            payload["language"] = language
        body = self._p._request(
            "POST",
            f"/ask/{verification_id}",
            json=payload,
        )
        return AskReply.model_validate(body)

    def reset(self, verification_id: str) -> bool:
        self._p._request("DELETE", f"/ask/{verification_id}")
        return True


class _LibraryNamespace:
    """``client.library.list()``. Works without an API key.

    The single-item ``library.get()`` was removed when the server merged
    ``GET /api/v1/library/{id}`` into ``GET /api/v1/verifications/{id}``.
    Use ``client.verifications.get(id)`` for single-item lookups — it
    works on a key-less client too (the server accepts an optional
    Bearer; anon callers see public + non-hidden claims).
    """

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
        curated: list[str] | None = None,
        verdict: str = "",
    ) -> LibraryList:
        """List the public verification catalog. Works without an API key.

        ``curated`` restricts to one or more named curated collections, e.g.
        ``["trivia"]`` (the pool behind the open-source quiz demo). ``verdict``
        filters by comma-separated labels, e.g. ``"True,False"``. ``sort`` also
        accepts ``"random"`` alongside ``recent`` / ``popular`` / ``most_true``
        / ``most_untrue`` / ``relevance``.
        """
        params: dict[str, Any] = {
            "page": page,
            "sort": sort,
            "search": search,
            "domain": domain,
            "entity": entity,
        }
        # Omit when default so existing callers keep byte-identical query strings.
        if curated:
            params["curated"] = ",".join(curated)
        if verdict:
            params["verdict"] = verdict
        body = self._p._request(
            "GET",
            "/library",
            params=params,
            auth_required=False,
        )
        return LibraryList.model_validate(body)


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
        user_agent: str | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("LENZ_API_KEY") or ""
        self._base_url = (base_url or os.environ.get("LENZ_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._owns_client = http_client is None
        # ``user_agent`` lets a wrapper (e.g. the CLI) override just the UA while
        # the SDK keeps ownership of every other default header — so a new
        # default header can't be silently dropped by a hand-copied client.
        self._client = http_client or httpx.Client(
            timeout=httpx.Timeout(timeout),
            headers={
                "User-Agent": user_agent or _user_agent(),
                "X-Lenz-API-Version": API_VERSION,
                "Accept": "application/json",
            },
        )

        # Resource namespaces (Stripe pattern for CRUD on past verifications,
        # follow-up conversations, and the public library)
        self.verifications = _VerificationsNamespace(self)
        self.ask = _AskNamespace(self)
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

    def verify(self, claim: str, *, language: str = "", **kwargs: Any) -> TaskAccepted:
        """Submit a claim for verification. Returns a ``task_id``; the
        pipeline runs async. For sync ergonomics use ``verify_and_wait``.

        ``language`` (optional): output language for the verification's
        prose fields. See module docstring for supported codes.
        """
        return self._verify_submit(claim=claim, language=language, **kwargs)

    def verify_batch(
        self,
        *,
        claims: list[VerifyBatchItem | dict[str, Any]],
        webhook_url: str = "",
        language: str = "",
        idempotency_key: str | None = None,
    ) -> BatchAccepted:
        """Submit multiple claims in one call. Returns a ``batch_id`` and
        per-claim ``task_id``s. Each item has its own lifecycle and webhook.

        ``language`` (optional): batch-wide output-language default. Each
        item dict may set its own ``language`` key to override the
        batch-wide value — server is authoritative on the merge.
        """
        return self._verify_batch(
            claims=claims,
            webhook_url=webhook_url,
            language=language,
            idempotency_key=idempotency_key,
        )

    def extract(self, *, text: str, language: str = "") -> ExtractedClaims:
        """Pull the verifiable claims out of any text. Sync, free, capped at
        1000 calls/key/day.

        ``language`` (optional): return extracted claims in the target
        language. Domain / status enums stay English.
        """
        return self._extract(text=text, language=language)

    def assess(self, *, text: str, language: str = "") -> AssessResponse:
        """Fast verdict via a 3-model frontier panel. Sync, ~5-10s.

        Returns ``AssessResponse`` with one ``AssessClaim`` per atomic
        claim that framing identified. Each claim has a ``verdict``
        ("True" / "Mostly True" / "Mixed" / "Mostly False" / "False" / "Error"),
        a categorical ``confidence`` ("high" / "medium" / "low"), and
        an optional ``verification_url`` pointing at the deep
        ``Verification`` when /assess found a matching stored claim.

        Use ``confidence`` to decide when to escalate: ``"low"`` claims
        are worth re-running through ``verify_and_wait`` for the deep
        8-model pipeline with citations.

        ``language`` (optional, default ``""``): set to ``'es' / 'de' / 'fr' /
        'it' / 'pt' / 'nl' / 'sv' / 'da' / 'no' / 'fi' / 'bg'`` to receive
        the claim text in that language. Verdict enums always English.
        Empty string omits the field from the request body — preserves
        byte-identical behavior for existing English callers.

        Paid quota — see ``client.usage()``. Quota debits per atomic
        claim that framing produces (multiclaim inputs consume N units).
        """
        return self._assess(text=text, language=language)

    def select(self, task_id: str, *, texts: list[str]) -> BatchAccepted:
        """Resolve a needs-input interrupt by selecting one or more claims.

        Pass ``texts=`` — the exact wording of the claim(s) you're choosing
        (entries from the prior status's ``claims`` / ``candidates``). Each
        selected claim fans out into its own pipeline; the returned
        ``BatchAccepted`` carries one ``items`` entry (each with its own
        ``task_id``) per claim. Poll each via ``get_status`` / ``wait``.

        Selection is by text, not index. Every text must match a claim that was
        offered in the prior interrupt — the server rejects anything else with
        a 422. To resolve a single claim, pass a one-element list.
        """
        if not texts:
            raise ValueError("select requires a non-empty texts=[...]")
        return self._select(task_id, texts=texts)

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
        language: str = "",
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

        Equivalent to ``wait(verify(claim, ...))`` — the idempotency-key
        handling (auto-generate / pin / disable) lives here because
        ``verify`` itself never generates one.
        """
        key = idempotency_key
        if key is None and idempotency:
            key = uuid.uuid4().hex
        accepted = self._verify_submit(
            claim=claim,
            source_url=source_url,
            webhook_url=webhook_url,
            language=language,
            idempotency_key=key,
        )
        logger.info("Submitted task: %s", accepted.task_id)
        return self.wait(accepted, timeout=timeout)

    def wait(self, task: str | TaskAccepted, *, timeout: float = 120.0) -> Verification:
        """Block until an already-submitted task terminates, then return its
        ``Verification``.

        ``task`` is a ``task_id`` string OR the ``TaskAccepted`` returned by
        ``verify`` / ``select`` — so ``client.wait(client.verify(claim=...))``
        reads naturally. Raises ``ValueError`` for an empty id,
        ``LenzNeedsInputError`` / ``LenzPipelineError`` on terminal
        non-success, and ``LenzTimeoutError`` if ``timeout`` elapses (the task
        may still finish server-side — resume via ``get_status``).
        """
        task_id = task if isinstance(task, str) else task.task_id
        if not task_id:
            raise ValueError("wait() requires a non-empty task_id (got an empty TaskAccepted.task_id).")
        terminal, timed_out = self._poll_to_terminal([task_id], timeout)
        if task_id in timed_out:
            raise LenzTimeoutError(
                message=f"wait timed out after {timeout}s",
                cause="Pipeline still running server-side.",
                fix=f"Resume via client.get_status('{task_id}') later.",
                doc_url="https://lenz.io/docs/verify#timeout",
                task_id=task_id,
            )
        return self._verification_from_terminal(terminal[task_id], task_id)

    def verify_batch_and_wait(
        self,
        *,
        claims: list[VerifyBatchItem | dict[str, Any]],
        webhook_url: str = "",
        language: str = "",
        idempotency_key: str | None = None,
        timeout: float = 180.0,
    ) -> list[BatchItemResult]:
        """Submit a batch and poll every item to a terminal state.

        Returns one ``BatchItemResult`` per task the batch accepted, **in input
        order**. Never raises on a per-item outcome — a claim that fails, pauses
        for input, or times out becomes a ``BatchItemResult`` with the matching
        ``status`` rather than an exception. (Transport/auth errors on the
        initial submit still raise.)
        """
        accepted = self._verify_batch(
            claims=claims,
            webhook_url=webhook_url,
            language=language,
            idempotency_key=idempotency_key,
        )
        ids = [it.task_id for it in accepted.items if it.task_id]
        terminal, timed_out = self._poll_to_terminal(ids, timeout)

        results: list[BatchItemResult] = []
        for it in accepted.items:  # preserve input order
            status = terminal.get(it.task_id)
            if not it.task_id or it.task_id in timed_out or status is None:
                results.append(BatchItemResult(task_id=it.task_id, claim_text=it.claim_text, status="timeout"))
            elif status.status == "completed" and status.result is not None:
                results.append(
                    BatchItemResult(
                        task_id=it.task_id,
                        claim_text=it.claim_text,
                        status="completed",
                        verification=status.result,
                        status_detail=status,
                    )
                )
            elif status.status == "needs_input":
                results.append(
                    BatchItemResult(
                        task_id=it.task_id, claim_text=it.claim_text, status="needs_input", status_detail=status
                    )
                )
            else:
                # failed, or completed-without-result (treated as failed).
                results.append(
                    BatchItemResult(task_id=it.task_id, claim_text=it.claim_text, status="failed", status_detail=status)
                )
        return results

    # ── poll engine (shared by wait + verify_batch_and_wait) ──

    def _poll_to_terminal(self, task_ids: list[str], timeout: float) -> tuple[dict[str, TaskStatus], set[str]]:
        """Round-robin poll ``task_ids`` until each reaches a terminal state
        (completed / needs_input / failed) or the deadline elapses.

        Returns ``(terminal_by_id, timed_out_ids)``. A timed-out task has no
        ``TaskStatus`` — ``"timeout"`` is a client-side concept, never a wire
        status — so it lands in the second set, not the dict.

        Ordering preserves the legacy ``verify_and_wait`` behavior: each round
        polls every still-pending id once *before* the deadline check, so after
        sleeping the remaining time we always poll once more and can succeed
        just past the nominal deadline. The timeout is therefore approximate:
        the final round finishes polling every pending id (bounded by the
        per-request timeout + retries) before any still-pending ids are marked
        timed out. Backoff reuses the existing 2/4/8/8…s sequence; the 10s cap
        is currently unreachable and kept only to preserve identical timing.

        A per-id poll that raises ``LenzError`` (e.g. a transport blip that
        outlived ``_request``'s own retries) does not abort the other ids: that
        id stays pending and is retried next round. A persistent error
        therefore surfaces as a timeout once the deadline passes.
        """
        pending = list(task_ids)
        terminal: dict[str, TaskStatus] = {}
        timed_out: set[str] = set()
        deadline = time.monotonic() + timeout
        backoff_idx = 0
        while pending:
            still_pending: list[str] = []
            for task_id in pending:
                try:
                    status = self._get_status(task_id)
                except LenzError:
                    # Don't let one id's poll failure abort the rest — retry it
                    # next round (bounded by the deadline below).
                    still_pending.append(task_id)
                    continue
                if status.status in ("completed", "needs_input", "failed"):
                    terminal[task_id] = status
                else:
                    still_pending.append(task_id)
            pending = still_pending
            if not pending:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out.update(pending)
                break
            sleep_for = min(POLL_BACKOFF[min(backoff_idx, len(POLL_BACKOFF) - 1)], POLL_BACKOFF_CAP)
            sleep_for = min(sleep_for, remaining)
            time.sleep(sleep_for)
            backoff_idx += 1
        return terminal, timed_out

    def _verification_from_terminal(self, status: TaskStatus, task_id: str) -> Verification:
        """Map a terminal ``TaskStatus`` to a ``Verification`` or raise the
        matching typed error. Shared by ``wait`` (and thus ``verify_and_wait``)."""
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
                fix="Inspect the payload, then call client.select(task_id, texts=[...]) with the chosen claim(s).",
                doc_url="https://lenz.io/docs/verify#needs-input",
                task_id=task_id,
                kind=status.reason,
                payload=status.model_dump(),
            )
        # failed. Server sends the diagnostic under ``error``; fall back to the
        # legacy fields for resilience.
        detail = status.error or status.failure_detail or status.failure_reason or "unknown"
        raise LenzPipelineError(
            message=f"Pipeline failed: {detail}",
            cause=detail,
            fix="Retry with a different claim, or check status.error for the diagnostic.",
            doc_url="https://lenz.io/docs/errors",
            task_id=task_id,
            failure_reason=status.failure_reason,
        )

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
        language: str = "",
        idempotency_key: str | None = None,
    ) -> TaskAccepted:
        payload: dict[str, Any] = {
            "text": claim or text,
            "source_url": source_url,
            "webhook_url": webhook_url,
        }
        # Omit-when-empty so existing English callers keep byte-identical
        # request bodies (no extra "language": "" key).
        if language:
            payload["language"] = language
        headers = {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        body = self._request("POST", "/verify", json=payload, headers=headers)
        return TaskAccepted.model_validate(body)

    def _verify_batch(
        self,
        *,
        claims: list[VerifyBatchItem | dict[str, Any]],
        webhook_url: str = "",
        language: str = "",
        idempotency_key: str | None = None,
    ) -> BatchAccepted:
        # ``webhook_url`` and ``language`` are batch-wide defaults; any
        # per-item value on a claim dict overrides them server-side.
        # Per-item items are validated as plain dicts at runtime — the
        # ``VerifyBatchItem`` TypedDict is purely for IDE autocompletion
        # (revised SDK plan decision 1C — no Pydantic coercion, keep the
        # runtime contract a plain dict).
        payload: dict[str, Any] = {"claims": list(claims), "webhook_url": webhook_url}
        if language:
            payload["language"] = language
        headers = {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        body = self._request("POST", "/verify/batch", json=payload, headers=headers)
        return BatchAccepted.model_validate(body)

    def _extract(self, *, text: str, language: str = "") -> ExtractedClaims:
        payload: dict[str, Any] = {"text": text}
        if language:
            payload["language"] = language
        body = self._request("POST", "/extract", json=payload)
        return ExtractedClaims.model_validate(body)

    def _assess(self, *, text: str, language: str = "") -> AssessResponse:
        payload: dict[str, Any] = {"text": text}
        if language:
            payload["language"] = language
        body = self._request("POST", "/assess", json=payload)
        return AssessResponse.model_validate(body)

    def _select(self, task_id: str, *, texts: list[str]) -> BatchAccepted:
        body = self._request("POST", f"/verify/{task_id}/select", json={"texts": texts})
        return BatchAccepted.model_validate(body)

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
        auth_optional: bool = False,
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
        # Attach the bearer on authed endpoints AND on opt-in optional-auth ones
        # (`auth_optional=True`). The server returns a caller's own private/hidden
        # rows only to the owning bearer, so `verifications.get` must send a key it
        # has (→ `lenz show` on a fresh private API claim). Purely public reads
        # (`library.list`) stay anonymous — no `auth_optional`, no key on the wire —
        # so a key never reaches an endpoint that doesn't need it.
        if self._api_key and (auth_required or auth_optional):
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


__all__ = ["API_VERSION", "DEFAULT_BASE_URL", "Lenz", "VerifyBatchItem"]
