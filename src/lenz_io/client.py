"""Public Lenz client — the ergonomic top-level surface.

Multi-language SDK convention:
* Request methods (verify, assess, extract, ask, …) take ``language=''``
  as their default. Sending an empty string means "do NOT include the
  field in the request body" — preserves byte-identical behavior for
  existing English callers. Set ``language='es'`` (or any of the 12
  supported codes) to receive prose fields in that language. ``assess``,
  ``verify`` (and ``verify_and_wait``) and ``ask.send`` also take
  ``language='auto'``: the answer comes back in the language of the submitted
  text. ``extract`` takes ``'auto'`` too and reports the language it chose in
  ``language`` on its result. The other methods take the codes only.
* Response models (``Verification``, ``AssessClaim``, ``VerificationListItem``)
  expose ``language`` populated by the server. Verdict / domain / status
  enum values stay English regardless of language; only free-form prose
  (atomic_claim, executive_summary, audit text) follows the request.
* Mixing the two (e.g. ``language='en'`` on a request) would send an
  extra ``"language": "en"`` key on every English call — breaks the
  byte-identical English path. The empty-default-then-omit convention
  exists precisely to avoid that.

Shape (six calls: the four-call ladder, ``review`` and ``citecheck``, plus
the supporting reads):

    from lenz_io import Lenz
    client = Lenz(api_key="lenz_...")

    # Marquee verbs — top-level (the four-call ladder)
    out = client.extract(text="...")                       # find claims in a document
    r = client.assess(claims=[...])                        # one fast verdict per claim, up to 20
    r = client.assess(claim="...")                         # ...or a single claim, ~15s
    v = client.verify_and_wait(claim="...")                # full multi-model pipeline, ~90s
    reply = client.ask.send(id, message="follow-up?")      # Q&A on a verification
    review = client.review_and_wait(text=draft)            # the whole ladder on a draft, 2-4 min

    # Review without waiting
    started = client.review(draft)                         # returns a review_id
    review = client.get_review(started.review_id)          # ReviewFull; view="issues" → ReviewIssues

    # Other verify-family verbs
    v = client.verify(claim="...")          # async submit; returns task_id
    result = client.wait(v)                  # block on a task_id / TaskAccepted
    batch = client.verify_batch(claims=[...])
    results = client.verify_batch_and_wait(claims=[...])   # submit + poll all
    status = client.get_status(task_id)      # single non-blocking poll
    client.select(task_id, claims=["The earth is flat."])  # pick one or more

    # Resource namespaces
    client.verifications.list()
    client.verifications.get(id) / delete(id)
    client.ask.history(id) / send(id, message=...) / reset(id)
    client.library.list()
    client.usage()

Naming: ``text`` is a document, ``claim`` is a claim. ``extract`` takes
``text``; ``assess``, ``verify``, batch items and ``select`` take ``claim`` /
``claims``. The older spellings (``text=`` on assess / verify / batch items,
``texts=`` on select) are accepted as aliases and are not going away.

Design decisions:

* Single persistent ``httpx.Client`` per ``Lenz`` instance for HTTP
  keep-alive (saves ~80ms TLS handshake per call on warm pool). Use as
  a context manager for clean shutdown, or call ``close()`` explicitly.
* Exponential backoff on transient errors (5xx, 429). 3 retry attempts
  by default. ``Retry-After`` honored on 429s.
* ``Idempotency-Key`` auto-generated per call for ``verify``,
  ``verify_and_wait``, ``verify_batch``, ``verify_batch_and_wait``,
  ``select``, ``assess``, ``extract`` and ``ask.send`` (random, reused
  across that call's own retries) so a network drop doesn't run the request
  twice. Customer can override with explicit ``idempotency_key=...`` or opt
  out with ``idempotency=False``. (2.x sent a key on ``verify_batch``,
  ``verify_batch_and_wait`` and ``ask.send`` only when you passed one.)
* ``X-Lenz-API-Version`` header pinned per SDK release (``API_VERSION``), so
  the server answers every release in the response shape it was built for.
* ``X-Request-ID`` is captured from every response onto the typed error
  for support escalation.
"""

from __future__ import annotations

import builtins
import copy
import logging
import time
import uuid  # noqa: F401  (tests patch ``lenz_io.client.uuid.uuid4``)
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager  # noqa: F401  (importable from here before the core existed)
from dataclasses import dataclass  # noqa: F401  (likewise)
from typing import Any, Final, Literal, TypedDict, TypeVar, overload  # noqa: F401  (likewise)
from urllib.parse import quote  # noqa: F401  (likewise)

import httpx
from typing_extensions import Self

# Every name below lived in this module before the shared core existed and
# stays importable from it (callers, the CLI and the tests import them here).
from ._core import (
    _CANCELLED_FAILURE as _CANCELLED_FAILURE,
    _HEADER_NAME as _HEADER_NAME,
    _HEADER_VALUE as _HEADER_VALUE,
    _NO_OPTIONS as _NO_OPTIONS,
    _RESERVED_HEADERS as _RESERVED_HEADERS,
    _TERMINAL_STATUSES as _TERMINAL_STATUSES,
    _VERSION_HEADER as _VERSION_HEADER,
    API_VERSION as API_VERSION,
    ASSESS_LIST_TIMEOUT as ASSESS_LIST_TIMEOUT,
    ASSESS_TIMEOUT as ASSESS_TIMEOUT,
    DEFAULT_BASE_URL as DEFAULT_BASE_URL,
    DEFAULT_MAX_RETRIES as DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT as DEFAULT_TIMEOUT,
    EXTRACT_TIMEOUT as EXTRACT_TIMEOUT,
    MAX_TIMEOUT_SECONDS as MAX_TIMEOUT_SECONDS,
    NOT_GIVEN as NOT_GIVEN,
    POLL_BACKOFF as POLL_BACKOFF,
    POLL_BACKOFF_CAP as POLL_BACKOFF_CAP,
    POLL_HINT_MAX as POLL_HINT_MAX,
    POLL_HINT_MIN as POLL_HINT_MIN,
    RETRY_BACKOFF as RETRY_BACKOFF,
    REVIEW_POLL_DEFAULT as REVIEW_POLL_DEFAULT,
    REVIEW_POLL_FLOOR as REVIEW_POLL_FLOOR,
    WAIT_TIMEOUT as WAIT_TIMEOUT,
    CitationPair as CitationPair,
    NotGiven as NotGiven,
    VerifyBatchItem as VerifyBatchItem,
    _aborts_on_long_stated_wait as _aborts_on_long_stated_wait,
    _after_response as _after_response,
    _after_transport_error as _after_transport_error,
    _already_deleted as _already_deleted,
    _ask_payload as _ask_payload,
    _assess_payload as _assess_payload,
    _batch_item_body as _batch_item_body,
    _batch_payload as _batch_payload,
    _batch_results as _batch_results,
    _blank_webhook_url as _blank_webhook_url,
    _body_error_code as _body_error_code,
    _call_key as _call_key,
    _call_options as _call_options,
    _CallOptions as _CallOptions,
    _carrying_key as _carrying_key,
    _check_assess_forms as _check_assess_forms,
    _check_headers as _check_headers,
    _check_library_iter_sort as _check_library_iter_sort,
    _check_retries as _check_retries,
    _check_served_version as _check_served_version,
    _check_timeout as _check_timeout,
    _citecheck_failed as _citecheck_failed,
    _citecheck_job as _citecheck_job,
    _citecheck_payload as _citecheck_payload,
    _citecheck_started_by_conflict as _citecheck_started_by_conflict,
    _client_settings as _client_settings,
    _ClientOptions as _ClientOptions,
    _default_headers as _default_headers,
    _exhausted as _exhausted,
    _extract_payload as _extract_payload,
    _extracted as _extracted,
    _failure_of as _failure_of,
    _first_page as _first_page,
    _given as _given,
    _is_cancel_body as _is_cancel_body,
    _is_citecheck_body as _is_citecheck_body,
    _is_full_review_body as _is_full_review_body,
    _job_key as _job_key,
    _json_or_none as _json_or_none,
    _key_header as _key_header,
    _library_params as _library_params,
    _merge_headers as _merge_headers,
    _names_the_job as _names_the_job,
    _poll_hint as _poll_hint,
    _poll_timeout as _core_poll_timeout,
    _prepare as _prepare,
    _request_settings as _request_settings,
    _resolve as _resolve,
    _retry_sleep as _retry_sleep,
    _review_failed as _review_failed,
    _review_job as _review_job,
    _review_params as _review_params,
    _review_payload as _review_payload,
    _review_started_by_conflict as _review_started_by_conflict,
    _seconds as _seconds,
    _segment as _segment,
    _select_texts as _select_texts,
    _snapshot as _snapshot,
    _stated_retry_after as _stated_retry_after,
    _status_path as _status_path,
    _unexpected_answer as _unexpected_answer,
    _user_agent as _user_agent,
    _verification_from_terminal as _verification_from_terminal,
    _verify_payload as _verify_payload,
    _wait_result as _wait_result,
    _wait_task_id as _wait_task_id,
    _walk as _walk,
    _with_options_layer as _with_options_layer,
)
from ._polling import JobPoll, TaskPoll, _progress_copy
from .errors import (
    MAX_RETRY_AFTER_SLEEP as MAX_RETRY_AFTER_SLEEP,
    NO_RETRY_429_CODES as NO_RETRY_429_CODES,
    UPSTREAM_503_CODES as UPSTREAM_503_CODES,
    CitecheckFailed as CitecheckFailed,
    CitecheckTimeout as CitecheckTimeout,
    LenzAPIError as LenzAPIError,
    LenzApiVersionError as LenzApiVersionError,
    LenzAuthError as LenzAuthError,
    LenzConnectionError as LenzConnectionError,
    LenzError as LenzError,
    LenzGoneError as LenzGoneError,
    LenzNeedsInputError as LenzNeedsInputError,
    LenzNotFoundError as LenzNotFoundError,
    LenzPipelineError as LenzPipelineError,
    LenzRateLimitError as LenzRateLimitError,
    LenzRequestTimeoutError as LenzRequestTimeoutError,
    LenzTimeoutError as LenzTimeoutError,
    ReviewFailed as ReviewFailed,
    ReviewTimeout as ReviewTimeout,
    map_response_to_error as map_response_to_error,
)
from .models import (
    AskHistory as AskHistory,
    AskReply as AskReply,
    AssessResponse as AssessResponse,
    BatchAccepted as BatchAccepted,
    BatchItemResult as BatchItemResult,
    CancelResult as CancelResult,
    Certificate as Certificate,
    Citecheck as Citecheck,
    CitecheckStarted as CitecheckStarted,
    ExtractedClaims as ExtractedClaims,
    FailureBlock as FailureBlock,
    LibraryItem as LibraryItem,
    LibraryList as LibraryList,
    Progress as Progress,
    RelatedVerifications as RelatedVerifications,
    ReviewFull as ReviewFull,
    ReviewIssues as ReviewIssues,
    ReviewStarted as ReviewStarted,
    TaskAccepted as TaskAccepted,
    TaskStatus as TaskStatus,
    Usage as Usage,
    Verification as Verification,
    VerificationList as VerificationList,
    VerificationListItem as VerificationListItem,
)

logger = logging.getLogger("lenz_io")

#: ``Lenz`` or a subclass, for ``with_options``' return type.
_Client = TypeVar("_Client", bound="Lenz")

#: An async job the poll loop waits on: a review or a citation check.
_Job = TypeVar("_Job", ReviewFull, Citecheck)


class _VerificationsNamespace:
    """``client.verifications.{list,get,delete,related}``."""

    def __init__(self, parent: Lenz) -> None:
        self._p = parent

    def list(
        self,
        *,
        page: int = 1,
        timeout: float | httpx.Timeout | None = None,
        max_retries: int | None = None,
        extra_headers: Mapping[str, str | None] | None = None,
    ) -> VerificationList:
        """One page of your verifications, newest first.

        Request options (``timeout``, ``max_retries``, ``extra_headers``):
        see :meth:`Lenz.with_options`.
        """
        options = _call_options(timeout, max_retries, extra_headers, "verifications.list()")
        body = self._p._request("GET", "/verifications", params={"page": page}, options=options)
        return VerificationList.model_validate(body)

    def iter(
        self,
        *,
        page: int = 1,
        timeout: float | httpx.Timeout | None = None,
        max_retries: int | None = None,
        extra_headers: Mapping[str, str | None] | None = None,
    ) -> Iterator[VerificationListItem]:
        """Every verification on your account, newest first, page after page
        from ``page`` (default 1).

        Fetches a page only when the items before it have been consumed, reads
        the page size from each response, and stops after a short or empty
        page, once the pages read reach the response's ``total``, or when the
        server answers another page than the one asked for. ``page`` must be
        1 or more (``ValueError``)::

            for item in client.verifications.iter():
                print(item.verification_id, item.verdict)

        Since 3.0. ``list(page=...)`` reads one page.

        Request options (``timeout``, ``max_retries``, ``extra_headers``)
        apply to every page request, and a bad one raises here, before the
        first page: see :meth:`Lenz.with_options`.
        """
        # Checked and snapshotted now: every page is read with these.
        options = _call_options(timeout, max_retries, extra_headers, "verifications.iter()")
        return _walk(lambda n: self.list(page=n, **_given(options)), _first_page(page))

    def get(
        self,
        verification_id: str,
        *,
        timeout: float | httpx.Timeout | None = None,
        max_retries: int | None = None,
        extra_headers: Mapping[str, str | None] | None = None,
    ) -> Verification:
        """Fetch a single verification by id.

        Works without an API key — the server accepts optional Bearer:
        anon callers see any public + non-hidden claim, authed callers
        additionally see their own at any visibility / status.

        With a key, also accepts the ``task_id`` that ``verify`` returned: a
        completed run returns its verification. A run with no result yet
        raises :class:`LenzVerificationNotReadyError` while it is running or
        waiting for input, and :class:`LenzPipelineError` when it failed. To
        wait for a run, use ``client.wait(task_id)``.

        Raises :class:`LenzGoneError` (HTTP 410) when the account's retention period has removed the verification.

        Request options (``timeout``, ``max_retries``, ``extra_headers``):
        see :meth:`Lenz.with_options`.
        """
        options = _call_options(timeout, max_retries, extra_headers, "verifications.get()")
        vid = _segment(verification_id, "verifications.get() needs a verification_id.")
        body = self._p._request(
            "GET",
            f"/verifications/{vid}",
            auth_required=False,
            auth_optional=True,  # send the key if we have one → owner sees private rows
            options=options,
        )
        return Verification.model_validate(body)

    def get_certificate(
        self,
        verification_id: str,
        *,
        timeout: float | httpx.Timeout | None = None,
        max_retries: int | None = None,
        extra_headers: Mapping[str, str | None] | None = None,
    ) -> Certificate:
        """Download the warranty certificate for a covered verification.

        Resolved by (verification, ACCOUNT), not by verification alone: one
        cached analysis can have several holders, each with their own
        certificate and their own cap, so this returns YOUR certificate over
        this analysis and never another customer's.

        Raises :class:`LenzNotFoundError` (a ``LenzError``, status 404) when
        this verification carries no certificate for your account — which is also what an uncovered verdict
        returns, so check ``verification.coverage.status`` first rather than
        using a 404 here to mean "not covered".

        The document is byte-identical to the public
        ``/certificate/<certificate_id>.json``, so it verifies with the
        published open-source checker without involving Lenz. A withdrawn
        certificate is still served — it is the record of what was warranted.

        Request options (``timeout``, ``max_retries``, ``extra_headers``):
        see :meth:`Lenz.with_options`.
        """
        options = _call_options(timeout, max_retries, extra_headers, "verifications.get_certificate()")
        vid = _segment(verification_id, "verifications.get_certificate() needs a verification_id.")
        body = self._p._request("GET", f"/verifications/{vid}/certificate", options=options)
        return Certificate.model_validate(body)

    def delete(
        self,
        verification_id: str,
        *,
        timeout: float | httpx.Timeout | None = None,
        max_retries: int | None = None,
        extra_headers: Mapping[str, str | None] | None = None,
    ) -> bool:
        """Idempotent. Retry-on-404 returns True ("already deleted").

        Request options (``timeout``, ``max_retries``, ``extra_headers``):
        see :meth:`Lenz.with_options`.
        """
        options = _call_options(timeout, max_retries, extra_headers, "verifications.delete()")
        vid = _segment(verification_id, "verifications.delete() needs a verification_id.")
        # Idempotent DELETE: a 404 (the row already gone, e.g. the reply to an
        # earlier delete was lost) is a success (``_already_deleted``).
        self._p._recovering(_already_deleted, "DELETE", f"/verifications/{vid}", options=options)
        return True

    def related(
        self,
        verification_id: str,
        *,
        limit: int = 5,
        timeout: float | httpx.Timeout | None = None,
        max_retries: int | None = None,
        extra_headers: Mapping[str, str | None] | None = None,
    ) -> RelatedVerifications:
        """Return public verifications semantically related to this one.

        Server caps ``limit`` to 10. Empty list when the verification has
        no embedding yet or no claim is close enough. Excludes the
        verification itself and editorially-hidden claims. Keyless like the
        library/detail reads; a key additionally unlocks the caller's own
        verifications. Accessible for
        any verification the caller owns (any visibility) or any public
        library item.

        Raises :class:`LenzGoneError` (HTTP 410) when the account's retention period has removed the verification.

        Request options (``timeout``, ``max_retries``, ``extra_headers``):
        see :meth:`Lenz.with_options`.
        """
        options = _call_options(timeout, max_retries, extra_headers, "verifications.related()")
        vid = _segment(verification_id, "verifications.related() needs a verification_id.")
        body = self._p._request(
            "GET",
            f"/verifications/{vid}/related",
            params={"limit": limit},
            auth_required=False,
            auth_optional=True,  # send the key if we have one → owner sees own rows
            options=options,
        )
        return RelatedVerifications.model_validate(body)


class _AskNamespace:
    """``client.ask.{history,send,reset}``.

    The endpoint moved from ``/verifications/{id}/follow-up`` to a flat
    ``/ask/{id}`` server-side; this namespace tracks the new URL shape.
    """

    def __init__(self, parent: Lenz) -> None:
        self._p = parent

    def history(
        self,
        verification_id: str,
        *,
        timeout: float | httpx.Timeout | None = None,
        max_retries: int | None = None,
        extra_headers: Mapping[str, str | None] | None = None,
    ) -> AskHistory:
        """The follow-up conversation on a verification.

        Raises :class:`LenzGoneError` (HTTP 410) when the account's retention period has removed the verification.

        Request options (``timeout``, ``max_retries``, ``extra_headers``):
        see :meth:`Lenz.with_options`.
        """
        options = _call_options(timeout, max_retries, extra_headers, "ask.history()")
        vid = _segment(verification_id, "ask.history() needs a verification_id.")
        body = self._p._request("GET", f"/ask/{vid}", options=options)
        return AskHistory.model_validate(body)

    def send(
        self,
        verification_id: str,
        *,
        message: str,
        language: str = "",
        idempotency_key: str | None = None,
        idempotency: bool = True,
        timeout: float | httpx.Timeout | None = None,
        max_retries: int | None = None,
        extra_headers: Mapping[str, str | None] | None = None,
    ) -> AskReply:
        """Send a follow-up question on an existing verification.

        ``language`` (optional) overrides the claim's stored language for
        this single reply. Omit to let the server use the claim's
        ``language`` as default — that's the typical case. ``'auto'`` also
        answers in the language of the claim being discussed.

        ``idempotency`` (default ``True``): send an ``Idempotency-Key`` so the
        SDK's own retry after a timeout or network drop replays the first
        reply instead of asking, and paying for, the question twice, and
        without appending the question and a second answer to the
        conversation. The key is random per call and reused across that
        call's retries; pin your own with ``idempotency_key=`` (it wins) to
        make a retry from another process replay too, or pass
        ``idempotency=False`` to send none. A retry that arrives while the
        first call is still running is answered 409 ``idempotency_conflict``:
        the SDK sends the same key again within the call's retries, and if it
        still conflicts raises it with ``retryable=True``. Every error of the
        call carries the key (``exc.idempotency_key``): resend with it, never
        as a plain new call, which would ask (and charge) again. (Since 3.0;
        2.x sent a key only when you passed one.)

        Never derived from the message: asking the same thing again is a
        normal thing to do here, and each turn also reads the history the
        previous one wrote, so a key derived from the text would replay a
        stale answer. A new call is a new key, so it is asked again.

        Paid — see ``client.usage()``.

        Raises :class:`LenzGoneError` (HTTP 410) when the account's retention period has removed the verification.

        Request options (``timeout``, ``max_retries``, ``extra_headers``):
        see :meth:`Lenz.with_options`.
        """
        options = _call_options(timeout, max_retries, extra_headers, "ask.send()")
        vid = _segment(verification_id, "ask.send() needs a verification_id.")
        payload = _ask_payload(message, language)
        key = _call_key(idempotency_key, idempotency)
        headers = _key_header(key)
        with _carrying_key(key, unreadable=True):
            body = self._p._request(
                "POST",
                f"/ask/{vid}",
                json=payload,
                headers=headers,
                options=options,
            )
            return AskReply.model_validate(body)

    def reset(
        self,
        verification_id: str,
        *,
        timeout: float | httpx.Timeout | None = None,
        max_retries: int | None = None,
        extra_headers: Mapping[str, str | None] | None = None,
    ) -> bool:
        """Clear the follow-up conversation on a verification.

        Request options (``timeout``, ``max_retries``, ``extra_headers``):
        see :meth:`Lenz.with_options`.
        """
        options = _call_options(timeout, max_retries, extra_headers, "ask.reset()")
        vid = _segment(verification_id, "ask.reset() needs a verification_id.")
        self._p._request("DELETE", f"/ask/{vid}", options=options)
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
        timeout: float | httpx.Timeout | None = None,
        max_retries: int | None = None,
        extra_headers: Mapping[str, str | None] | None = None,
    ) -> LibraryList:
        """List the public verification catalog. Works without an API key.

        ``curated`` restricts to one or more named curated collections, e.g.
        ``["trivia"]`` (the pool behind the open-source quiz demo). ``verdict``
        filters by comma-separated labels, e.g. ``"True,False"``. ``sort`` also
        accepts ``"random"`` alongside ``recent`` / ``most_true`` /
        ``most_untrue`` / ``relevance``.

        Request options (``timeout``, ``max_retries``, ``extra_headers``):
        see :meth:`Lenz.with_options`.
        """
        options = _call_options(timeout, max_retries, extra_headers, "library.list()")
        params = _library_params(
            page=page, sort=sort, search=search, domain=domain, entity=entity, curated=curated, verdict=verdict
        )
        body = self._p._request(
            "GET",
            "/library",
            params=params,
            auth_required=False,
            options=options,
        )
        return LibraryList.model_validate(body)

    def iter(
        self,
        *,
        page: int = 1,
        sort: str = "recent",
        search: str = "",
        domain: str = "",
        entity: str = "",
        # ``builtins.list``: in this class body ``list`` is the method above.
        curated: builtins.list[str] | None = None,
        verdict: str = "",
        timeout: float | httpx.Timeout | None = None,
        max_retries: int | None = None,
        extra_headers: Mapping[str, str | None] | None = None,
    ) -> Iterator[LibraryItem]:
        """Every item of the public catalog that matches the filters (the
        ones ``list`` takes), page after page from ``page``. Works without an
        API key.

        Fetches a page only when the items before it have been consumed, reads
        the page size from each response, and stops after a short or empty
        page, once the pages read reach the response's ``total``, or when the
        server answers another page than the one asked for. ``page`` must be
        1 or more, and ``sort="random"`` raises ``ValueError``: a random order is drawn
        anew for every page, so walking it neither reaches every item nor
        avoids repeats (use ``list(sort="random")`` for a sample). Since 3.0.

        Request options (``timeout``, ``max_retries``, ``extra_headers``)
        apply to every page request, and a bad one raises here, before the
        first page: see :meth:`Lenz.with_options`.
        """
        # Checked and snapshotted now: every page is read with these.
        options = _call_options(timeout, max_retries, extra_headers, "library.iter()")
        _check_library_iter_sort(sort)
        page = _first_page(page)
        return _walk(
            lambda n: self.list(
                page=n,
                sort=sort,
                search=search,
                domain=domain,
                entity=entity,
                curated=curated,
                verdict=verdict,
                **_given(options),
            ),
            page,
        )


class Lenz:
    """Top-level client.

    The constructor accepts ``api_key=None`` so library endpoints work
    without authentication (sandbox path for developers exploring before
    sign-up). Auth-required methods on an un-keyed client raise
    ``LenzAuthError`` with a link to ``/api-credentials``.

    Reads ``LENZ_API_KEY`` from the environment if no key is passed.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float | httpx.Timeout | None = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        http_client: httpx.Client | None = None,
        user_agent: str | None = None,
    ) -> None:
        self._api_key, self._base_url, timeout, max_retries = _client_settings(
            api_key, base_url, timeout, max_retries, "Lenz()"
        )
        self._timeout = timeout
        self._max_retries = max_retries
        self._owns_client = http_client is None
        self._options = _ClientOptions()
        # ``user_agent`` lets a wrapper (e.g. the CLI) override just the UA while
        # the SDK keeps ownership of every other default header — so a new
        # default header can't be silently dropped by a hand-copied client.
        self._client = http_client or httpx.Client(
            timeout=httpx.Timeout(timeout), headers=_default_headers(user_agent or _user_agent())
        )

        # Resource namespaces (Stripe pattern for CRUD on past verifications,
        # follow-up conversations, and the public library)
        self.verifications = _VerificationsNamespace(self)
        self.ask = _AskNamespace(self)
        self.library = _LibraryNamespace(self)

    # ── lifecycle ──

    def close(self) -> None:
        """Close the connection pool, if this client created it. A client
        given ``http_client=`` leaves it open (it is yours), and a copy made
        by :meth:`with_options` never closes the pool it shares: close the
        client you made the copies from."""
        if self._owns_client:
            self._client.close()

    def with_options(
        self: _Client,
        *,
        timeout: float | httpx.Timeout | NotGiven | None = NOT_GIVEN,
        max_retries: int | NotGiven = NOT_GIVEN,
        extra_headers: Mapping[str, str | None] | None = None,
    ) -> _Client:
        """A copy of this client with other request options, sharing its
        connection pool, key and base URL. Cheap: make one per request if you
        like. The client it was made from is not changed::

            fast = client.with_options(timeout=10, max_retries=0)
            fast.usage()

            traced = client.with_options(extra_headers={"X-Trace-Id": trace_id})
            traced.assess(claim="...")

        Every method also takes the same three options as keywords, for one
        call: ``client.assess(claim="...", timeout=20)``.

        * ``timeout``: one HTTP attempt, in seconds (a number greater than 0)
          or an ``httpx.Timeout``. httpx applies it per phase (connect, read,
          write, pool) and the read limit to each chunk, so it is an
          inactivity limit, not a bound on the whole call, and retries each get
          their own. Here ``None`` means no timeout, as on ``Lenz(timeout=None)``;
          on a single call ``timeout=None`` keeps the client's (pass
          ``httpx.Timeout(None)`` for no timeout on one call). ``extract`` and
          ``assess`` take at least 150 s / 100 s when the timeout is inherited
          from a copy or the client, as they always did; a timeout passed to the
          call itself is used as given, even below that (a shorter one can time
          out a call the server is still running: retry it with the same
          ``idempotency_key`` to get the answer).
        * ``max_retries``: how often a request that failed in a way worth
          retrying (a 5xx, a 429, a dropped connection) is sent again: a whole
          number, 0 or more.
        * ``extra_headers``: headers added to every request, merged over the
          copy's own by name, case-insensitively. ``None`` as a value removes a
          header a copy added. The SDK's own headers (``X-Lenz-API-Version``,
          ``Idempotency-Key``, ``Authorization``, ``Content-Type``,
          ``Content-Length``, ``Host``, ``Transfer-Encoding``) are refused.

        Per option, a call's keyword wins over the copy, and the copy over the
        client. A bad value raises ``ValueError`` here, before any request.
        The wait helpers keep their own ``timeout`` (how long to wait); the
        timeout of each of their requests comes from the copy or the client.

        The copy shares the pool: ``close()`` and ``with`` on a copy do
        nothing, closing the original closes the pool for every copy, and a
        copy is as safe to share across threads as the client.
        """
        layer = _with_options_layer(self._options, timeout, max_retries, extra_headers)
        clone = copy.copy(self)
        clone._owns_client = False
        clone._options = layer
        clone.verifications = _VerificationsNamespace(clone)
        clone.ask = _AskNamespace(clone)
        clone.library = _LibraryNamespace(clone)
        return clone

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # ── marquee verbs (top-level shortcuts) ──

    def verify(
        self,
        claim: str = "",
        *,
        text: str = "",
        language: str = "",
        visibility: str = "",
        depth: str = "",
        idempotency: bool = True,
        idempotency_key: str | None = None,
        source_url: str = "",
        webhook_url: str = "",
        timeout: float | httpx.Timeout | None = None,
        max_retries: int | None = None,
        extra_headers: Mapping[str, str | None] | None = None,
    ) -> TaskAccepted:
        """Submit a claim for verification. Returns a ``task_id``; the
        pipeline runs async. For sync ergonomics use ``verify_and_wait``.

        ``claim``: the statement to check. ``text=`` is accepted as an alias
        (``claim`` wins if both are given).

        ``language`` (optional): output language for the verification's
        prose fields. See module docstring for supported codes. ``'auto'``
        answers in the language of the submitted text; a concrete code always
        wins.

        ``visibility`` (optional): ``'private'`` (default, owner-only) or
        ``'unlisted'`` (readable by verification_id / at the /c/ URL, but
        never surfaced in the Library or search). Omit for private.

        ``depth`` (optional): ``'standard'`` (default) or ``'low'``. ``'low'``
        runs a shallower check — fewer sources, faster, same models — for half
        the credits (5 instead of 10). The completed ``Verification.depth`` echoes the depth the
        verdict was actually produced with, which can be ``'standard'`` for a
        ``'low'`` request served from cache.

        ``idempotency`` (default ``True``): send an ``Idempotency-Key`` so a
        retry after a network drop returns the task the first attempt started
        instead of starting — and paying for — a second one. The key is random
        per call and reused across this SDK's own retries; pin your own with
        ``idempotency_key=`` to make a retry from a different process replay
        too, or pass ``idempotency=False`` to send none. Never derived from
        the claim: the same claim sent again later is a new verification.

        ``source_url`` (optional): where the claim was found, kept with the
        verification. ``webhook_url`` (optional): where to send this
        verification's ``verification.*`` events; leave it out (or blank) for
        your key's default webhook URL.

        Request options (``timeout``, ``max_retries``, ``extra_headers``):
        see :meth:`Lenz.with_options`.
        """
        _call_options(timeout, max_retries, extra_headers, "verify()")
        return self._verify_submit(
            claim=claim,
            text=text,
            source_url=source_url,
            webhook_url=webhook_url,
            language=language,
            visibility=visibility,
            depth=depth,
            idempotency_key=_call_key(idempotency_key, idempotency),
            timeout=timeout,
            max_retries=max_retries,
            extra_headers=extra_headers,
        )

    def verify_batch(
        self,
        *,
        claims: Sequence[VerifyBatchItem | dict[str, Any]],
        webhook_url: str = "",
        language: str = "",
        visibility: str = "",
        depth: str = "",
        idempotency_key: str | None = None,
        idempotency: bool = True,
        timeout: float | httpx.Timeout | None = None,
        max_retries: int | None = None,
        extra_headers: Mapping[str, str | None] | None = None,
    ) -> BatchAccepted:
        """Submit multiple claims in one call. Returns a ``batch_id`` and
        per-claim ``task_id``s. Each item has its own lifecycle and webhook.

        ``language`` (optional): batch-wide output-language default. Each
        item dict may set its own ``language`` key to override the
        batch-wide value — server is authoritative on the merge.

        ``visibility`` (optional): batch-wide default, ``'private'`` or
        ``'unlisted'``. Each item dict may set its own ``visibility`` key
        to override the batch-wide value.

        ``depth`` (optional): batch-wide default, ``'standard'`` or ``'low'``
        (shallower check — fewer sources, faster, 5 credits instead of 10).
        Each item dict may set its own ``depth`` key to override the
        batch-wide value.

        ``idempotency`` (default ``True``): send an ``Idempotency-Key`` for
        the whole batch, so a retry after a network drop returns the tasks the
        first attempt started instead of starting, and paying for, them again.
        The key is random per call and reused across this SDK's own retries;
        pin your own with ``idempotency_key=`` (it wins), or pass
        ``idempotency=False`` to send none. (Since 3.0; 2.x sent a key only
        when you passed one.)

        Request options (``timeout``, ``max_retries``, ``extra_headers``):
        see :meth:`Lenz.with_options`.
        """
        _call_options(timeout, max_retries, extra_headers, "verify_batch()")
        return self._verify_batch(
            claims=claims,
            webhook_url=webhook_url,
            language=language,
            visibility=visibility,
            depth=depth,
            idempotency_key=_call_key(idempotency_key, idempotency),
            timeout=timeout,
            max_retries=max_retries,
            extra_headers=extra_headers,
        )

    def extract(
        self,
        *,
        text: str,
        language: str = "",
        focus: str = "",
        locate: bool | None = None,
        timeout: float | httpx.Timeout | None = None,
        idempotency: bool = True,
        idempotency_key: str | None = None,
        max_retries: int | None = None,
        extra_headers: Mapping[str, str | None] | None = None,
    ) -> ExtractedClaims:
        """Pull the verifiable claims out of any text. Sync, free, capped at
        1000 calls/account/day (shared across your API keys).

        ``language`` (optional): return extracted claims in the target
        language. Domain / status enums stay English. Leave it out for
        English. ``"auto"`` writes the claims in the language of ``text``
        (for a ``text`` that is a single URL, of the fetched page); a short
        or undetectable text, or a detector failure, gives English. A
        concrete code always wins. The result's ``language`` says which one
        the claims are written in: pass it on to ``assess`` or ``verify`` as
        ``language`` to keep a chain in one language.

        ``focus`` (optional): narrow the result to the claims it describes,
        e.g. ``"market size, growth and competitors"``. At most 300
        characters — a longer focus is rejected with a 422, never truncated.

        A focus can only SELECT from the claims the extractor found: it
        cannot add a claim, reword one, reorder them, change the output
        language, or change what counts as a claim. A claim you get back is
        one an unfocused call would have returned too, verbatim.

        When the text has claims but none fall within the focus, ``status``
        is ``"no_match"`` and ``claims`` is empty — the unfocused
        list is never substituted. Widen the focus and call again. A focused
        call costs the same single unit of the daily cap.

        ``locate`` (optional): with ``True``, only the claims that could be
        traced directly back to the text are returned, and each claim's
        ``positions`` says where the text makes it (``start``/``end`` index the
        ``text`` you sent, in code points, so ``text[start:end]`` is the
        passage). A claim found nowhere in the text, or found with a
        different figure, is left out; if that leaves no claim, ``status``
        is ``"not_a_claim"``. Locating adds a few seconds. If the claims
        cannot be located, ``positions`` is ``None`` and the list is returned
        unfiltered. Leave it ``None`` to use the server default (currently
        off); an explicit ``False`` is sent as such.

        ``timeout`` (optional): per-call HTTP timeout in seconds (or an
        ``httpx.Timeout``), overriding the client default for this one
        request, used as given. Otherwise ``extract`` uses
        ``EXTRACT_TIMEOUT`` (150s), or your client timeout when you configured
        a longer one: a long input can take more than a minute to extract.
        ``max_retries`` and ``extra_headers``: see :meth:`Lenz.with_options`.

        ``idempotency`` (default ``True``): send an ``Idempotency-Key`` so the
        SDK's own retry after a timeout or network drop replays the first
        answer instead of running the extraction — and using a unit of the
        daily cap — again. The key is random per call and reused across its
        retries; pin your own with ``idempotency_key=``, or pass
        ``idempotency=False`` to send none. Never derived from the text.
        """
        _call_options(timeout, max_retries, extra_headers, "extract()")
        return self._extract(
            text=text,
            language=language,
            focus=focus,
            locate=locate,
            timeout=timeout,
            idempotency_key=_call_key(idempotency_key, idempotency),
            max_retries=max_retries,
            extra_headers=extra_headers,
        )

    def assess(
        self,
        claim: str = "",
        *,
        text: str = "",
        claims: list[str] | None = None,
        language: str = "",
        suggest_rewrite: bool = False,
        timeout: float | httpx.Timeout | None = None,
        idempotency: bool = True,
        idempotency_key: str | None = None,
        max_retries: int | None = None,
        extra_headers: Mapping[str, str | None] | None = None,
    ) -> AssessResponse:
        """Fast verdict via a 3-model frontier panel. Sync, typically ~15s.

        Two input forms, one response shape:

        * ``claim``: one statement to check. If it contains several claims,
          each is verdicted separately (up to 20). ``text=`` is accepted as an
          alias (``claim`` wins if both are given).
        * ``claims``: a list of up to 20 statements, assessed in one call
          (one parallel wave, ~15s). Exactly one ``AssessClaim`` comes
          back per item, in the order sent. This is the step after
          ``extract`` in the ladder::

              out = client.extract(text=llm_output)
              claims = [c.claim for c in out.claims]
              quick = [row for i in range(0, len(claims), 20)  # 20 a call
                       for row in client.assess(claims=claims[i : i + 20]).claims]

          The two forms are mutually exclusive — passing ``claims`` together
          with a non-empty ``claim`` / ``text`` raises ``ValueError``.

        Each ``AssessClaim`` has a ``verdict`` ("True" / "Mostly True" /
        "Mixed" / "Mostly False" / "False" / "Error"), a categorical
        ``confidence`` ("high" / "medium" / "low"), and an optional
        ``verification_url`` pointing at the deep ``Verification`` when
        /assess found a matching stored claim.

        A row with ``status == "failed"`` could not be given a verdict (its
        ``verdict`` reads ``"Error"``). It stays in position, is not charged,
        and says why: ``failure.code`` is ``no_checkable_claim`` /
        ``framing_failed`` / ``upstream_unavailable`` / ``timeout`` (an open
        set; the last two are worth resending as-is), and ``failure.hint`` is
        one sentence on what to send next. A compound list item is assessed on
        its main claim; the other claims found in it are listed on that row in
        ``more_claims`` — send them as their own items to check the rest.
        (The deprecated ``error_code``, ``hint`` and ``identified_claims`` keep
        their original meaning.)

        Use ``confidence`` to decide when to escalate: ``"low"`` rows are
        worth re-running through ``verify_batch_and_wait`` for the deep
        multi-model pipeline with citations.

        ``language`` (optional, default ``""``): set to ``'es' / 'de' / 'fr' /
        'it' / 'pt' / 'nl' / 'sv' / 'da' / 'no' / 'fi' / 'bg'`` to receive
        the claim text in that language, or to ``'auto'`` to answer in the
        language of the submitted text (with a ``claims`` list, one language is
        chosen for the whole request: the one most items are written in, else
        English; name a code for a mixed-language list). Verdict enums always
        English.
        Empty string omits the field from the request body — preserves
        byte-identical behavior for existing English callers.

        ``suggest_rewrite=True`` also writes ``suggested_rewrite`` on each
        row: the claim with its wrong part corrected, for a claim the check
        found ``"False"`` or ``"Mostly False"`` with high confidence (``None``
        otherwise, or when there is no correction to write). Both forms, per
        row, no extra credit. It is written from the quick check's reasoning
        and is not itself verified: review it, or run it through ``verify``,
        before using it. ``False`` sends nothing.

        ``timeout`` (optional): per-call HTTP timeout in seconds (or an
        ``httpx.Timeout``), overriding the client default for this one
        request, used as given. Both forms otherwise use
        ``ASSESS_TIMEOUT`` (100s), or your client timeout when you configured a
        longer one — the server runs framing and a 3-model panel inside one
        request, and a long text can use the server's whole 90s budget.
        ``max_retries`` and ``extra_headers``: see :meth:`Lenz.with_options`.

        ``idempotency`` (default ``True``): send an ``Idempotency-Key`` so a
        retry after a network drop replays the first response instead of
        running — and paying for — the call twice. The key is random per
        invocation and reused across this SDK's own retries; pin your own with
        ``idempotency_key=`` to make a retry from a different process replay
        too, or pass ``idempotency=False`` to send none.

        Deliberately NOT derived from the claim text: an identical claim sent
        an hour later is a NEW question, and a content-derived key would
        replay the first answer for 24h — including for a claim whose verdict
        the server would otherwise refresh.

        Paid — see ``client.usage()``. 1 credit per claim assessed; a
        single-string input read as N claims (up to 20) costs N; ``Error``
        rows are free.
        """
        _call_options(timeout, max_retries, extra_headers, "assess()")
        _check_assess_forms(claim, text, claims)
        key = _call_key(idempotency_key, idempotency)
        if claims is not None:
            return self._assess(
                claims=claims,
                language=language,
                suggest_rewrite=suggest_rewrite,
                timeout=timeout,
                idempotency_key=key,
                max_retries=max_retries,
                extra_headers=extra_headers,
            )
        return self._assess(
            text=claim or text,
            language=language,
            suggest_rewrite=suggest_rewrite,
            timeout=timeout,
            idempotency_key=key,
            max_retries=max_retries,
            extra_headers=extra_headers,
        )

    def select(
        self,
        task_id: str,
        *,
        claims: list[str] | None = None,
        texts: list[str] | None = None,
        idempotency: bool = True,
        idempotency_key: str | None = None,
        timeout: float | httpx.Timeout | None = None,
        max_retries: int | None = None,
        extra_headers: Mapping[str, str | None] | None = None,
    ) -> BatchAccepted:
        """Resolve a needs-input interrupt by selecting one or more claims.

        Pass ``claims=`` — the exact wording of the claim(s) you're choosing
        (entries from the prior status's ``claims``). Each
        selected claim fans out into its own pipeline; the returned
        ``BatchAccepted`` carries one ``items`` entry (each with its own
        ``task_id``) per claim. Poll each via ``get_status`` / ``wait``.
        ``texts=`` is accepted as an alias (``claims`` wins if both are given).

        Selection is by text, not index. Every claim must match one that was
        offered in the prior interrupt — the server rejects anything else with
        a 422. To resolve a single claim, pass a one-element list.

        ``idempotency`` (default ``True``): send an ``Idempotency-Key`` so a
        retry after a network drop returns the tasks the first attempt started
        instead of starting a second set. Random per call and reused across
        this SDK's own retries; pin your own with ``idempotency_key=``, or pass
        ``idempotency=False`` to send none.

        Request options (``timeout``, ``max_retries``, ``extra_headers``):
        see :meth:`Lenz.with_options`.
        """
        _call_options(timeout, max_retries, extra_headers, "select()")
        chosen = _select_texts(claims, texts)
        return self._select(
            task_id,
            texts=chosen,
            idempotency_key=_call_key(idempotency_key, idempotency),
            timeout=timeout,
            max_retries=max_retries,
            extra_headers=extra_headers,
        )

    def get_status(
        self,
        task_id: str,
        *,
        timeout: float | httpx.Timeout | None = None,
        max_retries: int | None = None,
        extra_headers: Mapping[str, str | None] | None = None,
    ) -> TaskStatus:
        """Poll the pipeline status. Use ``verify_and_wait`` for sync ergonomics.

        Raises :class:`LenzGoneError` (HTTP 410) when the task completed and the
        account's retention period has since removed its verification; a task
        that is still running never answers 410.

        Request options (``timeout``, ``max_retries``, ``extra_headers``):
        see :meth:`Lenz.with_options`.
        """
        return self._get_status(task_id, timeout=timeout, max_retries=max_retries, extra_headers=extra_headers)

    def cancel(
        self,
        task_id: str,
        *,
        timeout: float | httpx.Timeout | None = None,
        max_retries: int | None = None,
        extra_headers: Mapping[str, str | None] | None = None,
    ) -> CancelResult:
        """Stop a verification that has not finished (``POST /verify/{task_id}/cancel``).

        A cancelled run is not charged and saves nothing; work already under
        way (a model call in flight) is not billed to you, and the run stops
        at its next step. A run waiting for ``select`` is cancelled too.

        The call is safe to repeat and answers 200 whatever the state of the
        run, so losing a race is not an error. Read the result:

        - ``cancelled`` is ``True`` and ``status`` is ``"cancelled"``: the run
          is cancelled, by this call or an earlier one, so a repeated or
          retried cancel answers ``True`` too.
        - ``cancelled`` is ``False``: the run is not cancelled, and ``status``
          is its status, normally ``"completed"`` (the verification exists and
          was charged as usual) or ``"failed"``. A task that ``select``
          already resolved answers ``False`` with ``"needs_input"``: cancel
          the task ids ``select`` returned.

        ``client.wait(task_id)`` and ``get_status`` then see ``cancelled``;
        ``wait`` raises :class:`LenzPipelineError` with
        ``failure_class == "cancelled"``.

        Raises :class:`LenzNotFoundError` (404) for an unknown task, another
        account's, or a task started on the website. A task that is a
        review's deep check raises :class:`LenzError` with
        ``code == "use_review_cancel"`` (409): cancel the review with
        :meth:`cancel_review`, which stops everything in it. A server error or
        a dropped connection is retried, as for every call that is safe to
        send twice.

        Request options (``timeout``, ``max_retries``, ``extra_headers``):
        see :meth:`Lenz.with_options`.

        Since 3.0.
        """
        options = _call_options(timeout, max_retries, extra_headers, "cancel()")
        tid = _segment(task_id, "cancel() needs a task_id.")
        path = f"/verify/{tid}/cancel"
        body = self._request("POST", path, options=options)
        if not _is_cancel_body(body, task_id):
            raise _unexpected_answer("POST", path)
        return CancelResult.model_validate(body)

    # ── headline ergonomic ──

    def verify_and_wait(
        self,
        claim: str = "",
        *,
        text: str = "",
        source_url: str = "",
        webhook_url: str = "",
        language: str = "",
        visibility: str = "",
        depth: str = "",
        timeout: float = WAIT_TIMEOUT,
        idempotency: bool = True,
        idempotency_key: str | None = None,
        on_progress: Callable[[str, Progress], None] | None = None,
        max_retries: int | None = None,
        extra_headers: Mapping[str, str | None] | None = None,
    ) -> Verification:
        """Submit + poll until the pipeline terminates.

        Returns the completed ``Verification`` on success. Raises:
          * ``LenzNeedsInputError`` if the pipeline pauses (multi_claim).
            Resolve via
            ``client.select(task_id, ...)`` then re-call this helper on
            the new task.
          * ``LenzPipelineError`` on terminal failure.
          * ``LenzTimeoutError`` if ``timeout`` elapses; the task may
            still finish server-side — recover via
            ``client.get_status(task_id)``.

        ``idempotency=True`` (default) auto-generates a key per call so a
        network drop after submit doesn't spawn a duplicate verification
        on retry. Customer can pin via ``idempotency_key="..."``.

        ``on_progress(task_id, progress)`` fires once per poll while the run
        is still going — the only way to see the stage during the ~90s wait,
        since this helper does the polling for you::

            client.verify_and_wait(
                claim="...",
                on_progress=lambda tid, p: print(f"{p.step} {p.index}/{p.total}"),
            )

        Equivalent to ``wait(verify(claim, ...))``, with the same
        idempotency-key handling (auto-generate / pin / disable).

        ``timeout`` defaults to ``WAIT_TIMEOUT`` (300s). The clock starts
        once the submit is accepted.

        ``max_retries``: the submit's retries (each poll is one request and a
        failed poll is read again on the next round). ``extra_headers``: added
        to the submit and to every poll. See :meth:`Lenz.with_options`; the
        timeout of each request comes from the copy or the client, since
        ``timeout`` here is how long to wait.
        """
        # Checked and snapshotted once: the submit and every poll use these.
        options = _call_options(None, max_retries, extra_headers, "verify_and_wait()")
        key = _call_key(idempotency_key, idempotency)
        with _carrying_key(key):
            accepted = self._verify_submit(
                claim=claim,
                text=text,
                source_url=source_url,
                webhook_url=webhook_url,
                language=language,
                visibility=visibility,
                depth=depth,
                idempotency_key=key,
                max_retries=options.max_retries,
                extra_headers=dict(options.headers),
            )
            logger.info("Submitted task: %s", accepted.task_id)
            # Only the headers reach the polls, and only when there are some, so
            # a subclass overriding ``wait`` with the 2.21 signature is still
            # called the way 2.21 called it.
            return self.wait(
                accepted, timeout=timeout, on_progress=on_progress, **_given(_CallOptions(headers=options.headers))
            )

    def wait(
        self,
        task: str | TaskAccepted,
        *,
        timeout: float = WAIT_TIMEOUT,
        on_progress: Callable[[str, Progress], None] | None = None,
        extra_headers: Mapping[str, str | None] | None = None,
    ) -> Verification:
        """Block until an already-submitted task terminates, then return its
        ``Verification``.

        ``task`` is a ``task_id`` string OR the ``TaskAccepted`` returned by
        ``verify`` / ``select`` — so ``client.wait(client.verify(claim=...))``
        reads naturally. Raises ``ValueError`` for an empty id,
        ``LenzNeedsInputError`` / ``LenzPipelineError`` on terminal
        non-success (a verification cancelled elsewhere, such as the
        website's Stop button, raises the same ``LenzPipelineError`` as a
        failed one, with ``failure_class == "cancelled"``),
        ``LenzGoneError`` if its account's retention period has
        removed the verification, and ``LenzTimeoutError`` if ``timeout``
        (default ``WAIT_TIMEOUT``, 300s) elapses (the task may still finish
        server-side — resume via ``get_status``).

        A poll answered 401 / 403 (``LenzAuthError``) or 404
        (``LenzNotFoundError``) ends the wait at once with that error: no
        later poll would answer otherwise. A 5xx, a 429 or a network failure
        is polled again on the next round. (Since 3.0; 2.x polled through
        every error until the timeout.)

        ``extra_headers``: added to every poll (see :meth:`Lenz.with_options`).
        Each poll is one request, so there is no ``max_retries`` here; its
        timeout comes from the copy or the client, capped by what is left of
        the wait.
        """
        options = _call_options(None, None, extra_headers, "wait()")
        task_id = _wait_task_id(task)
        terminal, timed_out, stopped = self._poll_to_terminal([task_id], timeout, on_progress, options=options)
        status = _wait_result(task_id, timeout, terminal, timed_out, stopped)
        return self._verification_from_terminal(status, task_id)

    def verify_batch_and_wait(
        self,
        *,
        claims: Sequence[VerifyBatchItem | dict[str, Any]],
        webhook_url: str = "",
        language: str = "",
        visibility: str = "",
        depth: str = "",
        idempotency_key: str | None = None,
        timeout: float = WAIT_TIMEOUT,
        on_progress: Callable[[str, Progress], None] | None = None,
        idempotency: bool = True,
        max_retries: int | None = None,
        extra_headers: Mapping[str, str | None] | None = None,
    ) -> list[BatchItemResult]:
        """Submit a batch and poll every item to a terminal state.

        Returns one ``BatchItemResult`` per task the batch accepted, **in input
        order**. Never raises on a per-item outcome — a claim that fails, pauses
        for input, or times out becomes a ``BatchItemResult`` with the matching
        ``status`` rather than an exception. (Transport/auth errors on the
        initial submit still raise.) An item whose poll is answered 404 or 410,
        or in another API version (``LenzApiVersionError``), is ``failed`` at
        once, with no ``status_detail``; the other items keep being polled. A
        poll answered 401 / 403 refuses the key itself, so the whole call
        raises ``LenzAuthError``.

        ``on_progress(task_id, progress)`` fires per still-running item per
        round; the ``task_id`` is what tells you which claim moved.

        ``timeout`` defaults to ``WAIT_TIMEOUT`` (300s); items still running
        then come back with ``status="timeout"`` and stay resumable by
        ``task_id``.

        ``idempotency`` / ``idempotency_key``: as on ``verify_batch``.

        ``max_retries``: the submit's retries (each poll is one request and a
        failed poll is read again on the next round). ``extra_headers``: added
        to the submit and to every poll. See :meth:`Lenz.with_options`; the
        timeout of each request comes from the copy or the client, since
        ``timeout`` here is how long to wait.
        """
        # Checked and snapshotted once: the submit and every poll use these.
        options = _call_options(None, max_retries, extra_headers, "verify_batch_and_wait()")
        key = _call_key(idempotency_key, idempotency)
        with _carrying_key(key):
            accepted = self._verify_batch(
                claims=claims,
                webhook_url=webhook_url,
                language=language,
                visibility=visibility,
                depth=depth,
                idempotency_key=key,
                max_retries=options.max_retries,
                extra_headers=dict(options.headers),
            )
            ids = [it.task_id for it in accepted.items if it.task_id]
            terminal, timed_out, stopped = self._poll_to_terminal(
                ids, timeout, on_progress, options=_CallOptions(headers=options.headers)
            )

        return _batch_results(accepted, terminal, timed_out, stopped)

    # ── /review: the whole recipe in one call ──

    def review(
        self,
        text: str,
        *,
        verdicts: list[str] | None = None,
        confidence: list[str] | None = None,
        max_assessments: int | None = None,
        max_verifications: int | None = None,
        depth: str | None = None,
        max_citations: int | None = None,
        suggest_edits: bool = False,
        language: str = "",
        webhook_url: str | None = None,
        visibility: str = "private",
        idempotency_key: str | None = None,
        timeout: float | httpx.Timeout | None = None,
        max_retries: int | None = None,
        extra_headers: Mapping[str, str | None] | None = None,
    ) -> ReviewStarted:
        """Start a review of a draft. Returns at once with a ``review_id``;
        the review takes two to four minutes. Use ``review_and_wait`` to
        block until it ends, or ``get_review`` to read it.

        ``text`` is the draft (up to 50,000 characters; longer text is cut and
        reported as ``summary.input_truncated``), or one public http(s) URL.

        Every claim in the draft gets a quick verdict (up to
        ``max_assessments``, default 20, most check-worthy first). The ones
        whose quick verdict is in ``verdicts`` (default ``["False",
        "Mostly False", "Mixed"]``) OR whose confidence is in ``confidence``
        (default ``["low"]``) get a deep check, up to ``max_verifications``
        (default 5; ``0`` makes a quick-only review) at ``depth``
        (``"standard"``, the default, or ``"low"``). ``None`` leaves a knob at
        the server default; an empty list switches that rule off.
        ``max_assessments=0`` checks no claim (with ``max_citations``:
        a review of the draft's citations only).

        Only claims traced directly back to the draft are checked: a claim
        found nowhere in it, or found with a different figure, is left out.
        Each claim row's ``positions`` says where the draft makes it (code
        point offsets into ``text``; for a URL draft, ``None`` offsets and
        the passage), and ``more_claim_locations`` does the same for
        ``more_claims`` (one ``ClaimLocation`` per string).

        ``max_citations=N`` (1-20) also checks the draft's first N citations
        (links and DOIs, read from ``text``; keep a link as a markdown link,
        ``[words](https://...)``): does each source say what the draft says
        it does? The findings are in ``citations`` and ``citation_issues``;
        the ones found past N are listed in ``more_citations``. ``None`` or
        ``0`` checks no citation and sends nothing.

        ``suggest_edits=True`` also returns, for each claim with a suggested
        rewrite (from its deep check, or from its quick check when the claim
        stayed on the quick verdict), the smallest edits to the draft that
        make it say what the rewrite says, in the draft's own language
        (``ReviewClaim.suggested_edits``, copied on its issue). They cost no
        extra credits, and the review completes once they are settled.
        ``False`` sends nothing.

        Credits: 1 per claim assessed, plus 10 (5 at ``depth="low"``) per
        deep check. ``credits.charged`` on the review says what it cost.

        ``webhook_url``: ``None`` (default) sends ``review.completed`` /
        ``review.failed`` to your credential's default webhook URL, ``""``
        sends none, a URL sends them there.

        An ``Idempotency-Key`` is generated when you pass none, so a retried
        submit cannot start a second review; a resend with the same key
        within 24 hours returns the same review, and a new key is a new
        review.

        Request options (``timeout``, ``max_retries``, ``extra_headers``):
        see :meth:`Lenz.with_options`.
        """
        options = _call_options(timeout, max_retries, extra_headers, "review()")
        payload = _review_payload(
            text=text,
            verdicts=verdicts,
            confidence=confidence,
            max_assessments=max_assessments,
            max_verifications=max_verifications,
            depth=depth,
            max_citations=max_citations,
            suggest_edits=suggest_edits,
            language=language,
            webhook_url=webhook_url,
            visibility=visibility,
        )
        headers = {"Idempotency-Key": _job_key(idempotency_key)}
        # A 409 naming the review a resend started settles the call
        # (``_review_started_by_conflict``).
        body = self._recovering(
            _review_started_by_conflict,
            "POST",
            "/review",
            json=payload,
            headers=headers,
            conflict_settles=_names_the_job("review_id"),
            options=options,
        )
        if isinstance(body, ReviewStarted):
            return body
        with _carrying_key(headers["Idempotency-Key"], unreadable=True):
            return ReviewStarted.model_validate(body)

    @overload
    def get_review(
        self,
        review_id: str,
        *,
        timeout: float | httpx.Timeout | None = None,
        max_retries: int | None = None,
        extra_headers: Mapping[str, str | None] | None = None,
    ) -> ReviewFull: ...

    @overload
    def get_review(
        self,
        review_id: str,
        *,
        view: Literal["full"],
        timeout: float | httpx.Timeout | None = None,
        max_retries: int | None = None,
        extra_headers: Mapping[str, str | None] | None = None,
    ) -> ReviewFull: ...

    @overload
    def get_review(
        self,
        review_id: str,
        *,
        view: Literal["issues"],
        timeout: float | httpx.Timeout | None = None,
        max_retries: int | None = None,
        extra_headers: Mapping[str, str | None] | None = None,
    ) -> ReviewIssues: ...

    def get_review(
        self,
        review_id: str,
        *,
        view: str = "full",
        timeout: float | httpx.Timeout | None = None,
        max_retries: int | None = None,
        extra_headers: Mapping[str, str | None] | None = None,
    ) -> ReviewFull | ReviewIssues:
        """Read a review. ``view="issues"`` returns just the issues and the
        failures (``ReviewIssues``, no ``claims``); the default returns every
        claim (``ReviewFull``).

        Raises :class:`LenzGoneError` (410) once the account's retention
        period has removed the review.

        Request options (``timeout``, ``max_retries``, ``extra_headers``):
        see :meth:`Lenz.with_options`.
        """
        options = _call_options(timeout, max_retries, extra_headers, "get_review()")
        rid = _segment(review_id, "get_review() needs a review_id.")
        params = _review_params(view)
        if params is not None:
            return ReviewIssues.model_validate(self._request("GET", f"/reviews/{rid}", params=params, options=options))
        return ReviewFull.model_validate(self._request("GET", f"/reviews/{rid}", options=options))

    def cancel_review(
        self,
        review_id: str,
        *,
        timeout: float | httpx.Timeout | None = None,
        max_retries: int | None = None,
        extra_headers: Mapping[str, str | None] | None = None,
    ) -> ReviewFull:
        """Stop a review (``POST /reviews/{review_id}/cancel``), including the
        deep checks it started. Returns the review as it stands afterwards, the
        full view: ``status`` is ``"cancelled"``.

        A review that had already ended is returned unchanged (``completed`` or
        ``failed``), and cancelling again is safe. What was not delivered is
        not charged: see ``credits.charged`` on the result for what the review
        cost.

        Raises :class:`LenzNotFoundError` (404) for an unknown review or
        another account's, and :class:`LenzGoneError` (410) once the account's
        retention period has removed it.

        Request options (``timeout``, ``max_retries``, ``extra_headers``):
        see :meth:`Lenz.with_options`.

        Since 3.0.
        """
        options = _call_options(timeout, max_retries, extra_headers, "cancel_review()")
        rid = _segment(review_id, "cancel_review() needs a review_id.")
        path = f"/reviews/{rid}/cancel"
        body = self._request("POST", path, options=options)
        if not _is_full_review_body(body, review_id):
            raise _unexpected_answer("POST", path)
        return ReviewFull.model_validate(body)

    def review_and_wait(
        self,
        text: str,
        *,
        timeout: float = 600.0,
        on_update: Callable[[ReviewFull], None] | None = None,
        verdicts: list[str] | None = None,
        confidence: list[str] | None = None,
        max_assessments: int | None = None,
        max_verifications: int | None = None,
        depth: str | None = None,
        max_citations: int | None = None,
        suggest_edits: bool = False,
        language: str = "",
        webhook_url: str | None = None,
        visibility: str = "private",
        idempotency_key: str | None = None,
        max_retries: int | None = None,
        extra_headers: Mapping[str, str | None] | None = None,
    ) -> ReviewFull:
        """Start a review (``review(text, ...)``, which documents every option
        but ``timeout`` and ``on_update``) and poll it until it ends.

        Returns the completed ``ReviewFull``: read ``outcome``, then
        ``issues``. Polls on the review's own ``poll_after_seconds`` (never
        faster than every 5 s). ``on_update(review)`` is called on every poll
        whose body changed, so you can show the quick verdicts as soon as they
        are in and each deep check as it lands; without it the helper is
        silent.

        Raises :class:`ReviewFailed` when the review ends ``failed`` (its
        ``failure`` says why), and :class:`ReviewTimeout` when
        ``timeout`` seconds pass first: the review keeps running, and the
        error carries its ``review_id`` and the last body read. The clock
        starts once the submit is accepted.

        ``max_retries``: the submit's retries (each poll is one request and a
        failed poll is read again on the next round). ``extra_headers``: added
        to the submit and to every poll. See :meth:`Lenz.with_options`; the
        timeout of each request comes from the copy or the client, since
        ``timeout`` here is how long to wait.
        """
        # Checked and snapshotted once: the submit and every poll use these.
        options = _call_options(None, max_retries, extra_headers, "review_and_wait()")
        # The key is minted here (as ``review`` would) so the wait's errors carry it too.
        key = _job_key(idempotency_key)
        with _carrying_key(key):
            started = self.review(
                text,
                verdicts=verdicts,
                confidence=confidence,
                max_assessments=max_assessments,
                max_verifications=max_verifications,
                depth=depth,
                max_citations=max_citations,
                suggest_edits=suggest_edits,
                language=language,
                webhook_url=webhook_url,
                visibility=visibility,
                idempotency_key=key,
                # Only the options given, so a 2.21 ``review`` override is still called.
                **_given(options),
            )
            logger.info("Submitted review: %s", started.review_id)
            return self._wait_review(
                started.review_id, timeout=timeout, on_update=on_update, extra_headers=dict(options.headers)
            )

    # ── /citecheck: the citation check on its own ──

    def citecheck(
        self,
        text: str | None = None,
        *,
        pairs: list[CitationPair] | None = None,
        max_citations: int | None = None,
        language: str = "",
        webhook_url: str | None = None,
        idempotency_key: str | None = None,
        timeout: float | httpx.Timeout | None = None,
        max_retries: int | None = None,
        extra_headers: Mapping[str, str | None] | None = None,
    ) -> CitecheckStarted:
        """Start a citation check. Returns at once with a ``citecheck_id``;
        use ``citecheck_and_wait`` to block until it ends, or
        ``get_citecheck`` to read it.

        Send exactly one of ``text`` and ``pairs``:

        - ``text``: a draft, up to 50,000 characters, with its links
          (markdown links, bare URLs, ``doi:`` and ``doi.org`` forms, ``[n]``
          markers with a reference list). The first ``max_citations`` (1-20,
          default 20) citations in the draft's order are checked; the rest are
          listed in ``more_citations``.
        - ``pairs``: 1 to 20 statement-source pairs (:class:`CitationPair`),
          each checked as it is. ``max_citations`` does not apply.

        Each check answers: does the cited source say what the statement
        says it does? ``language`` is the language Lenz writes the reasoning
        in; English when omitted. Hints are always in English, and the
        passage and the quote stay verbatim in the page's language.
        ``webhook_url``: ``None`` (default) sends
        ``citecheck.completed`` / ``citecheck.failed`` to your credential's
        default webhook URL, ``""`` sends none, a URL sends them there. An
        ``Idempotency-Key`` is generated when you pass none: a resend with the
        same key within 24 hours returns the same check.

        Request options (``timeout``, ``max_retries``, ``extra_headers``):
        see :meth:`Lenz.with_options`.
        """
        options = _call_options(timeout, max_retries, extra_headers, "citecheck()")
        payload = _citecheck_payload(
            text=text, pairs=pairs, max_citations=max_citations, language=language, webhook_url=webhook_url
        )
        headers = {"Idempotency-Key": _job_key(idempotency_key)}
        # A 409 naming the check a resend started settles the call
        # (``_citecheck_started_by_conflict``).
        body = self._recovering(
            _citecheck_started_by_conflict,
            "POST",
            "/citecheck",
            json=payload,
            headers=headers,
            conflict_settles=_names_the_job("citecheck_id"),
            options=options,
        )
        if isinstance(body, CitecheckStarted):
            return body
        with _carrying_key(headers["Idempotency-Key"], unreadable=True):
            return CitecheckStarted.model_validate(body)

    def get_citecheck(
        self,
        citecheck_id: str,
        *,
        timeout: float | httpx.Timeout | None = None,
        max_retries: int | None = None,
        extra_headers: Mapping[str, str | None] | None = None,
    ) -> Citecheck:
        """Read a citation check. Raises :class:`LenzGoneError` (410) once
        the account's retention period has removed it.

        Request options (``timeout``, ``max_retries``, ``extra_headers``):
        see :meth:`Lenz.with_options`.
        """
        options = _call_options(timeout, max_retries, extra_headers, "get_citecheck()")
        cid = _segment(citecheck_id, "get_citecheck() needs a citecheck_id.")
        return Citecheck.model_validate(self._request("GET", f"/citechecks/{cid}", options=options))

    def cancel_citecheck(
        self,
        citecheck_id: str,
        *,
        timeout: float | httpx.Timeout | None = None,
        max_retries: int | None = None,
        extra_headers: Mapping[str, str | None] | None = None,
    ) -> Citecheck:
        """Stop a citation check (``POST /citechecks/{citecheck_id}/cancel``).
        Returns the check as it stands afterwards: ``status`` is
        ``"cancelled"``.

        You are charged only for the citations it checked before the cancel;
        the rest are refunded (``credits.charged``). A check that had already
        ended is returned unchanged (``completed`` or ``failed``), and
        cancelling again is safe.

        Raises :class:`LenzNotFoundError` (404) for an unknown check, another
        account's, or the id of a review, and :class:`LenzGoneError` (410) once
        the account's retention period has removed it.

        Request options (``timeout``, ``max_retries``, ``extra_headers``):
        see :meth:`Lenz.with_options`.

        Since 3.0.
        """
        options = _call_options(timeout, max_retries, extra_headers, "cancel_citecheck()")
        cid = _segment(citecheck_id, "cancel_citecheck() needs a citecheck_id.")
        path = f"/citechecks/{cid}/cancel"
        body = self._request("POST", path, options=options)
        if not _is_citecheck_body(body, citecheck_id):
            raise _unexpected_answer("POST", path)
        return Citecheck.model_validate(body)

    def citecheck_and_wait(
        self,
        text: str | None = None,
        *,
        timeout: float = 600.0,
        on_update: Callable[[Citecheck], None] | None = None,
        pairs: list[CitationPair] | None = None,
        max_citations: int | None = None,
        language: str = "",
        webhook_url: str | None = None,
        idempotency_key: str | None = None,
        max_retries: int | None = None,
        extra_headers: Mapping[str, str | None] | None = None,
    ) -> Citecheck:
        """Start a citation check (``citecheck(text, ...)``, which documents
        every option but ``timeout`` and ``on_update``) and poll it until
        it ends. Returns the completed :class:`Citecheck`: read ``outcome``,
        then ``citation_issues``.

        Polls on the check's own ``poll_after_seconds`` (never faster than
        every 5 s); ``on_update(check)`` is called on every poll whose body
        changed. Raises :class:`CitecheckFailed` when the check ends
        ``failed``, and :class:`CitecheckTimeout` when ``timeout`` seconds pass
        first: the check keeps running, and the error carries its
        ``citecheck_id`` and the last body read. The clock starts once the
        submit is accepted.

        ``max_retries``: the submit's retries (each poll is one request and a
        failed poll is read again on the next round). ``extra_headers``: added
        to the submit and to every poll. See :meth:`Lenz.with_options`; the
        timeout of each request comes from the copy or the client, since
        ``timeout`` here is how long to wait.
        """
        # Checked and snapshotted once: the submit and every poll use these.
        options = _call_options(None, max_retries, extra_headers, "citecheck_and_wait()")
        # The key is minted here (as ``citecheck`` would) so the wait's errors carry it too.
        key = _job_key(idempotency_key)
        with _carrying_key(key):
            started = self.citecheck(
                text,
                pairs=pairs,
                max_citations=max_citations,
                language=language,
                webhook_url=webhook_url,
                idempotency_key=key,
                # Only the options given, so a 2.21 ``citecheck`` override is still called.
                **_given(options),
            )
            logger.info("Submitted citation check: %s", started.citecheck_id)
            return self._wait_citecheck(
                started.citecheck_id, timeout=timeout, on_update=on_update, extra_headers=dict(options.headers)
            )

    def _wait_citecheck(
        self,
        citecheck_id: str,
        *,
        timeout: float,
        on_update: Callable[[Citecheck], None] | None = None,
        extra_headers: Mapping[str, str | None] | None = None,
    ) -> Citecheck:
        """The poll loop behind ``citecheck_and_wait`` (and ``lenz citecheck --resume``)."""
        path, parse, timed_out = _citecheck_job(citecheck_id, timeout)
        return self._wait_job(
            path,
            timeout=timeout,
            on_update=on_update,
            parse=parse,
            failed=_citecheck_failed,
            timed_out=timed_out,
            options=_call_options(None, None, extra_headers, "citecheck_and_wait()"),
        )

    def _wait_review(
        self,
        review_id: str,
        *,
        timeout: float,
        on_update: Callable[[ReviewFull], None] | None = None,
        extra_headers: Mapping[str, str | None] | None = None,
    ) -> ReviewFull:
        """The poll loop behind ``review_and_wait`` (and ``lenz review --resume``)."""
        path, parse, timed_out = _review_job(review_id, timeout)
        return self._wait_job(
            path,
            timeout=timeout,
            on_update=on_update,
            parse=parse,
            failed=_review_failed,
            timed_out=timed_out,
            options=_call_options(None, None, extra_headers, "review_and_wait()"),
        )

    def _wait_job(
        self,
        path: str,
        *,
        timeout: float,
        on_update: Callable[[_Job], None] | None,
        parse: Callable[[Any], _Job | None],
        failed: Callable[[_Job], Exception],
        timed_out: Callable[[_Job | None], Exception],
        options: _CallOptions = _NO_OPTIONS,
    ) -> _Job:
        """The poll loop behind every ``*_and_wait`` of an async job (a review,
        a citation check).

        Like the verification poll, it always reads once more at the deadline
        before giving up. A failed read (a 5xx, a network error, a 429) is
        retried on the next round, after the wait the server stated if it
        stated one, rather than raised; anything else (404, 403, 410) raises.
        ``parse`` returns the job's model, or ``None`` for a 200 that is not
        this job's body (a failed poll).
        """
        poll = JobPoll(path, timeout, time.monotonic(), parse=parse, failed=failed, timed_out=timed_out)
        while True:
            # One request per poll, bounded by what is left of the deadline:
            # the client's own retry ladder inside a poll could run minutes
            # past it. A failed poll is retried on the next round instead.
            remaining = poll.before_poll(time.monotonic())
            job: _Job | None
            try:
                body = self._request("GET", path, max_retries=0, timeout=self._poll_timeout(remaining), options=options)
                job = poll.read(body)
            except Exception as exc:
                if poll.failed(exc):
                    raise
                job = None
            if job is not None:
                if poll.changed(job) and on_update is not None:
                    try:
                        on_update(job.model_copy(deep=True))
                    except Exception:
                        logger.debug("on_update callback raised for %s", path, exc_info=True)
                if poll.settle(job):
                    return job
            time.sleep(poll.next_sleep(time.monotonic()))

    # ── poll engine (shared by wait + verify_batch_and_wait) ──

    def _poll_to_terminal(
        self,
        task_ids: list[str],
        timeout: float,
        on_progress: Callable[[str, Progress], None] | None = None,
        *,
        options: _CallOptions = _NO_OPTIONS,
    ) -> tuple[dict[str, TaskStatus], set[str], dict[str, LenzError]]:
        """Round-robin poll ``task_ids`` until each reaches a terminal state
        (completed / needs_input / failed / cancelled) or the deadline elapses.

        Returns ``(terminal_by_id, timed_out_ids, stopped_by_id)``. A timed-out
        task has no ``TaskStatus`` — ``"timeout"`` is a client-side concept,
        never a wire status — so it lands in the second set, not the dict.

        No poll starts once the deadline is spent, not even within a round:
        the ids left are timed out. The one exception is ``timeout <= 0``,
        which reads every status once, as in 2.x. Each poll is
        ONE request whose timeout is what is left of the deadline, at most the
        client's own timeout (``_poll_timeout``): the client's own retry
        ladder inside a poll could run minutes past it, so a failed poll is
        retried on the next round instead. Backoff reuses the existing 2/4/8/8…s sequence; the 10s cap
        is currently unreachable and kept only to preserve identical timing.

        A per-id poll that fails with a 5xx, a 429, a network failure or any
        other error a later poll can change does not abort the other ids: that
        id stays pending and is polled next round (after the wait the server
        stated, if it stated one), so a persistent one surfaces as a timeout
        once the deadline passes. An error no later poll can change for that
        id stops it at once and lands in ``stopped_by_id``: a 410
        (``LenzGoneError``, retention removed it), a 404
        (``LenzNotFoundError``) and a version error (``LenzApiVersionError``).
        A 401 / 403 (``LenzAuthError``) refuses the key itself, so it is raised
        for the whole wait. ``wait`` raises a stopped id's error.

        ``on_progress(task_id, progress)`` fires once per still-running poll.
        It takes the id as well as the object because this loop round-robins a
        whole batch — without it a caller cannot tell which claim moved. An
        exception inside the callback must not kill the poll, so it is logged
        at DEBUG and swallowed: a library has no business writing to a caller's
        stderr for a bug in their own callback, and DEBUG keeps it silent
        under default configuration while still being findable.

        The server's ``progress.poll_after_seconds`` replaces the fixed 2/4/8…
        ladder when it is present and sane; garbage falls back to the ladder.
        """
        poll = TaskPoll(task_ids, timeout, time.monotonic())
        while poll.pending:
            for task_id in poll.start_round():
                remaining = poll.may_poll(task_id, time.monotonic())
                if remaining is None:
                    continue
                try:
                    body = self._request(
                        "GET",
                        _status_path(task_id),
                        max_retries=0,
                        timeout=self._poll_timeout(remaining),
                        options=options,
                    )
                    status = TaskStatus.model_validate(body)
                except LenzError as exc:
                    if poll.failed(task_id, exc):
                        raise
                    continue
                if poll.answered(task_id, status) and on_progress is not None:
                    try:
                        # A copy — a caller must not be able to mutate our state.
                        on_progress(task_id, _progress_copy(status))
                    except Exception:
                        logger.debug("on_progress callback raised for task %s", task_id, exc_info=True)
            if poll.round_done():
                break
            sleep_for = poll.next_sleep(time.monotonic())
            if sleep_for is None:
                break
            time.sleep(sleep_for)
        return poll.results()

    def _verification_from_terminal(self, status: TaskStatus, task_id: str) -> Verification:
        """Map a terminal ``TaskStatus`` to a ``Verification`` or raise the
        matching typed error. Shared by ``wait`` (and thus ``verify_and_wait``)."""
        return _verification_from_terminal(status, task_id)

    # ── account ──

    def usage(
        self,
        *,
        timeout: float | httpx.Timeout | None = None,
        max_retries: int | None = None,
        extra_headers: Mapping[str, str | None] | None = None,
    ) -> Usage:
        """The account's plan, credits and per-capability usage.

        Request options (``timeout``, ``max_retries``, ``extra_headers``):
        see :meth:`Lenz.with_options`.
        """
        options = _call_options(timeout, max_retries, extra_headers, "usage()")
        body = self._request("GET", "/me/usage", options=options)
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
        visibility: str = "",
        depth: str = "",
        idempotency_key: str | None = None,
        timeout: float | httpx.Timeout | None = None,
        max_retries: int | None = None,
        extra_headers: Mapping[str, str | None] | None = None,
    ) -> TaskAccepted:
        options = _call_options(timeout, max_retries, extra_headers, "verify()")
        payload = _verify_payload(
            claim=claim,
            text=text,
            source_url=source_url,
            webhook_url=webhook_url,
            language=language,
            visibility=visibility,
            depth=depth,
        )
        headers = _key_header(idempotency_key)
        with _carrying_key(idempotency_key, unreadable=True):
            body = self._request("POST", "/verify", json=payload, headers=headers, options=options)
            return TaskAccepted.model_validate(body)

    def _verify_batch(
        self,
        *,
        claims: Sequence[VerifyBatchItem | dict[str, Any]],
        webhook_url: str = "",
        language: str = "",
        visibility: str = "",
        depth: str = "",
        idempotency_key: str | None = None,
        timeout: float | httpx.Timeout | None = None,
        max_retries: int | None = None,
        extra_headers: Mapping[str, str | None] | None = None,
    ) -> BatchAccepted:
        options = _call_options(timeout, max_retries, extra_headers, "verify_batch()")
        payload = _batch_payload(
            claims=claims, webhook_url=webhook_url, language=language, visibility=visibility, depth=depth
        )
        headers = _key_header(idempotency_key)
        with _carrying_key(idempotency_key, unreadable=True):
            body = self._request("POST", "/verify/batch", json=payload, headers=headers, options=options)
            return BatchAccepted.model_validate(body)

    def _poll_timeout(self, remaining: float) -> httpx.Timeout | None:
        """The timeouts of one poll request: each phase (connect, read, write,
        pool) the copy or the client in use configured, capped by what is left of the
        wait's deadline (an unbounded phase gets just that). Past the deadline
        (only a ``timeout <= 0`` wait polls then) ``None``: the copy's or the client's own.

        Reads the client actually in use, so ``Lenz(timeout=None)``, an
        ``httpx.Timeout`` and an ``httpx.Client`` passed as ``http_client=``
        all work, and so does a copy's timeout (``with_options``)."""
        return _core_poll_timeout(self._options.timeout, self._client.timeout, remaining)

    def _extract(
        self,
        *,
        text: str,
        language: str = "",
        focus: str = "",
        locate: bool | None = None,
        timeout: float | httpx.Timeout | None = None,
        idempotency_key: str | None = None,
        max_retries: int | None = None,
        extra_headers: Mapping[str, str | None] | None = None,
    ) -> ExtractedClaims:
        options = _call_options(timeout, max_retries, extra_headers, "extract()")
        payload = _extract_payload(text=text, language=language, focus=focus, locate=locate)
        headers = _key_header(idempotency_key)
        with _carrying_key(idempotency_key, unreadable=True):
            body = self._request(
                "POST", "/extract", json=payload, headers=headers, options=options, floor=EXTRACT_TIMEOUT
            )
            return _extracted(body, locate=locate)

    def _assess(
        self,
        *,
        text: str = "",
        claims: list[str] | None = None,
        language: str = "",
        suggest_rewrite: bool = False,
        timeout: float | httpx.Timeout | None = None,
        idempotency_key: str | None = None,
        max_retries: int | None = None,
        extra_headers: Mapping[str, str | None] | None = None,
    ) -> AssessResponse:
        options = _call_options(timeout, max_retries, extra_headers, "assess()")
        payload = _assess_payload(text=text, claims=claims, language=language, suggest_rewrite=suggest_rewrite)
        headers = _key_header(idempotency_key)
        with _carrying_key(idempotency_key, unreadable=True):
            body = self._request(
                "POST", "/assess", json=payload, headers=headers, options=options, floor=ASSESS_TIMEOUT
            )
            return AssessResponse.model_validate(body)

    def _select(
        self,
        task_id: str,
        *,
        texts: list[str],
        idempotency_key: str | None = None,
        timeout: float | httpx.Timeout | None = None,
        max_retries: int | None = None,
        extra_headers: Mapping[str, str | None] | None = None,
    ) -> BatchAccepted:
        options = _call_options(timeout, max_retries, extra_headers, "select()")
        tid = _segment(task_id, "select() needs a task_id.")
        headers = _key_header(idempotency_key)
        with _carrying_key(idempotency_key, unreadable=True):
            body = self._request(
                "POST", f"/verify/{tid}/select", json={"texts": texts}, headers=headers, options=options
            )
            return BatchAccepted.model_validate(body)

    def _get_status(
        self,
        task_id: str,
        *,
        timeout: float | httpx.Timeout | None = None,
        max_retries: int | None = None,
        extra_headers: Mapping[str, str | None] | None = None,
    ) -> TaskStatus:
        options = _call_options(timeout, max_retries, extra_headers, "get_status()")
        tid = _segment(task_id, "get_status() needs a task_id.")
        body = self._request("GET", f"/verify/status/{tid}", options=options)
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
        timeout: float | httpx.Timeout | None = None,
        max_retries: int | None = None,
        conflict_settles: Callable[[Any], bool] | None = None,
        options: _CallOptions = _NO_OPTIONS,
        floor: float | None = None,
    ) -> dict[str, Any]:
        """One API call, with the retry ladder. Every ``LenzError`` it raises
        carries the ``Idempotency-Key`` it sent (``exc.idempotency_key``).

        ``options`` are the call's request options and ``floor`` the minimum
        an inherited timeout gets (``_resolve``). ``timeout`` and
        ``max_retries`` are the SDK's own settings for one request (a wait's
        polls): when set they win over every option."""
        key = (headers or {}).get("Idempotency-Key") or None
        resolved, retries, option_headers = _request_settings(
            options, self._options, self._client.timeout, self._max_retries, floor, timeout, max_retries
        )
        with _carrying_key(key, unreadable=True):
            return self._send(
                method,
                path,
                json=json,
                params=params,
                headers=headers,
                option_headers=option_headers,
                auth_required=auth_required,
                auth_optional=auth_optional,
                timeout=resolved,
                retries=retries,
                conflict_settles=conflict_settles,
            )

    def _send(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None,
        params: dict[str, Any] | None,
        headers: dict[str, str] | None,
        option_headers: Sequence[tuple[str, str]],
        auth_required: bool,
        auth_optional: bool,
        timeout: httpx.Timeout | None,
        retries: int,
        conflict_settles: Callable[[Any], bool] | None,
    ) -> dict[str, Any]:
        url, req_headers, req_timeout = _prepare(
            api_key=self._api_key,
            base_url=self._base_url,
            path=path,
            headers=headers,
            option_headers=option_headers,
            auth_required=auth_required,
            auth_optional=auth_optional,
            timeout=timeout,
        )
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                response = self._client.request(
                    method, url, json=json, params=params, headers=req_headers, timeout=req_timeout
                )
            except (httpx.UnsupportedProtocol, httpx.LocalProtocolError):
                # The request could never be sent (a bad URL scheme, a request
                # httpx refuses to write): a programming error, not a network one.
                raise
            except httpx.TransportError as exc:
                # Worth sending again, unless this was the last attempt.
                last_exc = exc
                time.sleep(_after_transport_error(exc, attempt, retries, method, path))
                continue
            done, value = _after_response(response, attempt, retries, method, path, req_headers, conflict_settles)
            if done:
                return value  # type: ignore[return-value]
            time.sleep(value)
        raise _exhausted(last_exc, method, path)

    def _recovering(self, recover: Callable[[LenzError], Any], method: str, path: str, **kwargs: Any) -> Any:
        """``_request``, where an error ``recover`` reads as an answer (not
        ``None``) settles the call with that answer: the per-operation
        recoveries of ``_core`` (an already-deleted verification, a 409 naming
        the job a resend started)."""
        try:
            return self._request(method, path, **kwargs)
        except LenzError as exc:
            settled = recover(exc)
            if settled is None:
                raise
            return settled


__all__ = ["API_VERSION", "DEFAULT_BASE_URL", "NOT_GIVEN", "Lenz", "NotGiven", "VerifyBatchItem"]
