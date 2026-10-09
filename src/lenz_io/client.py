"""Public Lenz client — the ergonomic top-level surface.

Multi-language SDK convention:
* Request methods (verify, assess, extract, ask, …) take ``language=''``
  as their default. Sending an empty string means "do NOT include the
  field in the request body" — preserves byte-identical behavior for
  existing English callers. Set ``language='es'`` (or any of the 12
  supported codes) to receive prose fields in that language. ``assess``,
  ``verify`` (and ``verify_and_wait``) and ``ask.send`` also take
  ``language='auto'``: the answer comes back in the language of the submitted
  text. The other methods take the codes only.
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
import math
import numbers
import operator
import os
import re
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Final, Literal, TypedDict, TypeVar, overload
from urllib.parse import quote

import httpx
from typing_extensions import Self

from . import __version__
from .errors import (
    MAX_RETRY_AFTER_SLEEP,
    NO_RETRY_429_CODES,
    UPSTREAM_503_CODES,
    CitecheckFailed,
    CitecheckTimeout,
    LenzAPIError,
    LenzApiVersionError,
    LenzAuthError,
    LenzConnectionError,
    LenzError,
    LenzGoneError,
    LenzNeedsInputError,
    LenzNotFoundError,
    LenzPipelineError,
    LenzRateLimitError,
    LenzRequestTimeoutError,
    LenzTimeoutError,
    ReviewFailed,
    ReviewTimeout,
    map_response_to_error,
)
from .models import (
    AskHistory,
    AskReply,
    AssessResponse,
    BatchAccepted,
    BatchItemResult,
    CancelResult,
    Certificate,
    Citecheck,
    CitecheckStarted,
    ExtractedClaims,
    FailureBlock,
    LibraryItem,
    LibraryList,
    Progress,
    RelatedVerifications,
    ReviewFull,
    ReviewIssues,
    ReviewStarted,
    TaskAccepted,
    TaskStatus,
    Usage,
    Verification,
    VerificationList,
    VerificationListItem,
)

logger = logging.getLogger("lenz_io")

# The API version this SDK asks for, sent as ``X-Lenz-API-Version`` on every
# request: the server answers in that version's response shape, whatever the
# account. Since 3.0.0 this is the current shape (one name for each field,
# status and error code), and the SDK reads only that shape from its own
# calls (2.x asked for ``2026-05-13``). Every attribute keeps the meaning it
# had in 2.x; the old names are deprecated aliases. Webhooks are the one
# place both shapes are still parsed.
API_VERSION = "2026-10-11"
_VERSION_HEADER = "X-Lenz-API-Version"

DEFAULT_BASE_URL = "https://lenz.io/api/v1"
DEFAULT_TIMEOUT = 30.0
# ``assess`` runs framing and then a 3-model panel inside one synchronous
# request, and the server divides a single budget between them — so BOTH
# forms get the same room, not just the list one. Typical calls answer in
# ~15s, but a long text can take up to the server's 90s budget. The SDK
# waits 10s longer than that, so the server always answers (or refuses)
# before the client gives up.
#
# Applies to ``assess(claim=...)`` as well as ``assess(claims=[...])`` since
# 2.12.0. Before that the single form used the 30s client default, and a call
# whose framing was slow could time out client-side AFTER the server had
# charged it — and a retry with no idempotency key charged again.
ASSESS_TIMEOUT = 100.0
#: Deprecated alias for :data:`ASSESS_TIMEOUT`, kept for callers that imported
#: it. Same value; to be removed in a future major release.
ASSESS_LIST_TIMEOUT = ASSESS_TIMEOUT
# ``extract`` reads the whole input and enumerates its claims inside one
# synchronous request. Most calls answer in seconds, but a long input can take
# well over a minute, past the 30s client default. A client timeout makes the
# SDK re-send the call; the per-call idempotency key makes that re-send replay
# the first answer rather than run the extraction again. 150s leaves room
# above the slowest.
EXTRACT_TIMEOUT = 150.0
# Default ``timeout`` for the helpers that poll a verification to its end
# (``wait``, ``verify_and_wait``, ``verify_batch_and_wait``). A check usually
# finishes in ~90s; a slow one under load can take several minutes. A
# timeout never loses the task: it stays resumable by ``task_id``.
WAIT_TIMEOUT = 300.0
DEFAULT_MAX_RETRIES = 3
RETRY_BACKOFF = (1.0, 2.0, 4.0)
POLL_BACKOFF = (2.0, 4.0, 8.0)
POLL_BACKOFF_CAP = 10.0
# The statuses a verification poll ends on. ``cancelled`` is a task stopped
# elsewhere (the website's Stop button, another process): its own status in
# API version 2026-10-11, where 2026-05-13 said ``failed``.
_TERMINAL_STATUSES = ("completed", "needs_input", "failed", "cancelled")
# Bounds on the server's ``progress.poll_after_seconds``. A hint outside
# them is treated as garbage and the local ladder is used instead — the
# floor stops a bad value turning the poll loop into a hot loop, the
# ceiling stops it stalling a wait well inside its own timeout.
POLL_HINT_MIN = 1.0
POLL_HINT_MAX = 30.0
# ``review_and_wait`` reads the review's own ``poll_after_seconds`` (10 s while
# assessing, 15 s while deep-checking). A review takes minutes, so nothing is
# gained by polling faster than this floor, whatever the body says.
REVIEW_POLL_FLOOR = 5.0
REVIEW_POLL_DEFAULT = 10.0


class NotGiven:
    """The type of :data:`NOT_GIVEN`: "this option was not passed", as opposed
    to ``None``, which ``Lenz.with_options(timeout=None)`` reads as "no
    timeout". Only for type annotations; callers never need to pass it."""

    _instance: NotGiven | None = None

    def __new__(cls) -> NotGiven:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __bool__(self) -> Literal[False]:
        return False

    def __repr__(self) -> str:
        return "NOT_GIVEN"

    def __copy__(self) -> NotGiven:
        return self

    def __deepcopy__(self, memo: Any) -> NotGiven:
        return self


#: The default of ``Lenz.with_options``' parameters: keep what the client has.
NOT_GIVEN: Final = NotGiven()

#: Headers the request options refuse, in any casing: the SDK sets them
#: itself (``idempotency_key=`` and ``api_key=`` are the way to choose the
#: first two), or httpx computes them.
_RESERVED_HEADERS = frozenset(
    {
        "x-lenz-api-version",
        "idempotency-key",
        "authorization",
        "content-type",
        "content-length",
        "host",
        "transfer-encoding",
    }
)


#: The longest timeout a request can take, in seconds (the Node SDK's limit,
#: 2^31 - 1 ms): past it the socket layer overflows on every request. Also the
#: longest finite wait a ``Retry-After`` is read as.
MAX_TIMEOUT_SECONDS = 2_147_483


def _seconds(value: Any) -> bool:
    """A finite real number of seconds greater than 0 and at most
    :data:`MAX_TIMEOUT_SECONDS` (``bool`` is not one)."""
    return (
        not isinstance(value, bool)
        and isinstance(value, numbers.Real)
        and math.isfinite(value)
        and 0 < float(value) <= MAX_TIMEOUT_SECONDS
    )


def _snapshot(value: Any) -> Any:
    """A copy of an ``httpx.Timeout`` (they are mutable); anything else as is."""
    return httpx.Timeout(value) if isinstance(value, httpx.Timeout) else value


def _check_timeout(value: Any, where: str) -> float | httpx.Timeout | None:
    """A per-request timeout, checked and snapshotted: ``None``, a finite real
    number of seconds greater than 0, httpx's 4-tuple ``(connect, read, write,
    pool)`` whose parts are each ``None`` or such a number, or an
    ``httpx.Timeout`` (taken as given). An ``httpx.Timeout`` or a tuple comes
    back as a new ``httpx.Timeout``, so changing the caller's object later
    changes nothing here. ``ValueError`` otherwise, before any request."""
    if value is None or _seconds(value):
        return value  # type: ignore[no-any-return]
    if isinstance(value, httpx.Timeout):
        parts = (value.connect, value.read, value.write, value.pool)
        if not any(isinstance(p, (int, float)) and p > MAX_TIMEOUT_SECONDS for p in parts):
            return httpx.Timeout(value)
    elif isinstance(value, tuple) and len(value) == 4 and all(part is None or _seconds(part) for part in value):
        return httpx.Timeout(value)
    raise ValueError(
        f"{where}: timeout must be a number of seconds greater than 0 and at most 2,147,483, None, an "
        f"httpx.Timeout or a (connect, read, write, pool) tuple of such numbers or None (got {value!r})."
    )


def _check_retries(value: Any, where: str) -> int:
    """A retry count: a whole number (anything ``operator.index`` takes, but
    not ``bool``), 0 or more, returned as an ``int``. ``ValueError`` otherwise."""
    count: int | None = None
    if not isinstance(value, bool):
        try:
            count = operator.index(value)
        except TypeError:
            count = None
    if count is None or count < 0:
        raise ValueError(f"{where}: max_retries must be a whole number, 0 or more (got {value!r}).")
    return count


#: A header name is an RFC 7230 token; a value is visible ASCII, spaces and
#: tabs (no CR, LF or NUL: those would end the header or the request), or empty.
_HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
#: Spaces and tabs only inside the value: at either end httpx refuses it.
_HEADER_VALUE = re.compile(r"(?:[\x21-\x7e](?:[\t\x20-\x7e]*[\x21-\x7e])?)?")


def _check_headers(value: Any, where: str) -> tuple[tuple[str, str | None], ...]:
    """``extra_headers`` as (name, value) pairs in the order given: names
    RFC 7230 tokens outside ``_RESERVED_HEADERS``, values visible-ASCII
    strings or ``None`` (removes the header a ``with_options`` copy added).
    ``ValueError`` otherwise. The pairs are a snapshot: changing the caller's
    mapping later changes nothing here."""
    if value is None:
        return ()
    if not isinstance(value, Mapping):
        raise ValueError(f"{where}: extra_headers must be a mapping of header names to strings (got {value!r}).")
    pairs: list[tuple[str, str | None]] = []
    for name, header in value.items():
        if not isinstance(name, str) or not _HEADER_NAME.fullmatch(name):
            raise ValueError(
                f"{where}: a header name must be a non-empty token of ASCII letters, digits and "
                f"!#$%&'*+-.^_`|~ (got {name!r})."
            )
        if name.lower() in _RESERVED_HEADERS:
            raise ValueError(f"{where}: the {name} header is set by the SDK and cannot be passed in extra_headers.")
        if header is not None and (not isinstance(header, str) or not _HEADER_VALUE.fullmatch(header)):
            raise ValueError(
                f"{where}: the value of header {name} must be a string of visible ASCII characters, with "
                f"spaces and tabs only between them (not at either end), or None."
            )
        pairs.append((name, header))
    return tuple(pairs)


def _merge_headers(
    lower: Sequence[tuple[str, str]], upper: Sequence[tuple[str, str | None]]
) -> tuple[tuple[str, str], ...]:
    """``upper`` over ``lower``, names compared case-insensitively: the last
    spelling of a name and its value win, and a ``None`` value removes the
    name from ``lower``."""
    merged: dict[str, tuple[str, str]] = {name.lower(): (name, value) for name, value in lower}
    for name, value in upper:
        if value is None:
            merged.pop(name.lower(), None)
        else:
            merged[name.lower()] = (name, value)
    return tuple(merged.values())


@dataclass(frozen=True)
class _CallOptions:
    """The request options one call was given (its ``timeout``,
    ``max_retries`` and ``extra_headers`` keywords); ``None`` inherits."""

    timeout: float | httpx.Timeout | None = None
    max_retries: int | None = None
    headers: tuple[tuple[str, str | None], ...] = ()


_NO_OPTIONS = _CallOptions()


def _call_options(
    timeout: float | httpx.Timeout | None,
    max_retries: int | None,
    extra_headers: Mapping[str, str | None] | None,
    where: str,
) -> _CallOptions:
    """One call's request options, checked and snapshotted: raises
    ``ValueError`` before the call mints a key or makes a request."""
    return _CallOptions(
        timeout=_check_timeout(timeout, where),
        max_retries=None if max_retries is None else _check_retries(max_retries, where),
        headers=_check_headers(extra_headers, where),
    )


def _given(options: _CallOptions) -> dict[str, Any]:
    """``options`` as keywords for another public method, only those that were
    given: a method overridden with a 2.21 signature (which knows none of
    them) is still called the way 2.21 called it when there are none."""
    out: dict[str, Any] = {}
    if options.timeout is not None:
        out["timeout"] = options.timeout
    if options.max_retries is not None:
        out["max_retries"] = options.max_retries
    if options.headers:
        out["extra_headers"] = dict(options.headers)
    return out


@dataclass(frozen=True)
class _ClientOptions:
    """What ``Lenz.with_options`` set on a copy, already flattened over the
    copy it was made from. ``NOT_GIVEN`` inherits from the ``httpx.Client`` in
    use (timeout) or the constructor (retries)."""

    timeout: float | httpx.Timeout | NotGiven | None = NOT_GIVEN
    max_retries: int | NotGiven = NOT_GIVEN
    headers: tuple[tuple[str, str], ...] = ()


def _resolve(
    call: _CallOptions,
    layer: _ClientOptions,
    client_timeout: httpx.Timeout,
    client_retries: int,
    floor: float | None,
) -> tuple[httpx.Timeout | None, int, tuple[tuple[str, str], ...]]:
    """The timeout, retry count and option headers of one request. Pure: no
    I/O, no clock.

    Per field, the first that is set wins: the call's option, the copy's
    (``with_options``), the client's (the ``httpx.Client`` in use for the
    timeout, the constructor for retries). Headers merge, the call's over the
    copy's. A ``floor`` (``extract`` / ``assess``) applies to an inherited
    timeout only, as it always did: when its ``read`` is bounded and shorter
    than the floor, the floor replaces the whole timeout; otherwise the
    inherited timeout is kept as it is. A timeout of ``None`` means "the
    ``httpx.Client``'s own" (httpx's ``USE_CLIENT_DEFAULT``).
    """
    timeout: httpx.Timeout | None
    if call.timeout is not None:
        timeout = httpx.Timeout(call.timeout)
    else:
        own = None if isinstance(layer.timeout, NotGiven) else httpx.Timeout(layer.timeout)
        inherited = client_timeout if own is None else own
        if floor is not None and inherited.read is not None and inherited.read < floor:
            timeout = httpx.Timeout(floor)
        else:
            timeout = own
    if call.max_retries is not None:
        retries = call.max_retries
    elif not isinstance(layer.max_retries, NotGiven):
        retries = layer.max_retries
    else:
        retries = client_retries
    return timeout, retries, _merge_headers(layer.headers, call.headers)


def _poll_hint(progress: Any) -> float | None:
    """The server's suggested wait before the next poll, or None.

    Distinct from ``_retry_after_seconds``, which is the error-retry ladder's
    "you errored, back off". This one means "you are fine, look again shortly"
    and rides in the body rather than a header — ``Retry-After`` on a 200 is
    off-spec enough that a proxy may drop it, and a header never appears in
    the OpenAPI schema.
    """
    value = getattr(progress, "poll_after_seconds", None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not POLL_HINT_MIN <= value <= POLL_HINT_MAX:
        return None
    return float(value)


def _user_agent() -> str:
    return f"lenz-io-python/{__version__} (httpx {httpx.__version__})"


def _blank_webhook_url(value: Any) -> bool:
    """An empty or whitespace-only ``webhook_url`` on ``verify`` /
    ``verify_batch``: it always meant the key's default webhook (the API read
    it as left out), so it is left out, which keeps that meaning on every API
    version (the current one reads a blank value as "no webhook"). ``None``
    is left out too."""
    return value is None or (isinstance(value, str) and not value.strip())


def _batch_item_body(item: Any) -> Any:
    """The wire shape of one batch item.

    An item without ``claim`` is forwarded verbatim, so every existing caller
    keeps a byte-identical request body. An item with ``claim`` is sent as
    ``text`` — the wire key the server has always accepted. An empty
    ``webhook_url`` is left out, as on the batch itself: it has always meant
    the key's default, and leaving it out keeps that meaning on every API
    version.
    """
    if isinstance(item, dict) and _blank_webhook_url(item.get("webhook_url")):
        item = {k: v for k, v in item.items() if k != "webhook_url"}
    if "claim" not in item:
        return item
    body = {k: v for k, v in item.items() if k != "claim"}
    body["text"] = item["claim"] or item.get("text", "")
    return body


def _extracted(body: Any, *, locate: bool | None) -> ExtractedClaims:
    """``/extract``'s answer. A body that located every claim away answers
    ``claims: []``; the 2.x field for that was ``locations=[]``, which only the
    request (``locate=True``) can tell apart from "nothing found"."""
    out = ExtractedClaims.model_validate(body)
    answered = isinstance(body, dict) and body.get("claims") == []
    if answered and locate and out.status == "not_a_claim" and out.locations is None:
        out.locations = []
    return out


#: ``Lenz`` or a subclass, for ``with_options``' return type.
_Client = TypeVar("_Client", bound="Lenz")

#: An async job the poll loop waits on: a review or a citation check.
_Job = TypeVar("_Job", ReviewFull, Citecheck)


class CitationPair(TypedDict, total=False):
    """One statement and the source it cites, for ``citecheck(pairs=...)``.

    ``statement`` (1 to 1,000 characters) and exactly one of ``url`` (http or
    https) and ``doi`` (the DOI alone, like ``10.1038/nature12373``).
    ``quotes``: up to 3 excerpts the statement quotes from the source, each
    15 to 500 characters and words of the statement. With a ``doi``, what the
    reference gives: ``cited_title``, ``cited_authors`` (family names),
    ``cited_year`` (four digits), ``cited_journal``. Type-only: plain dicts
    are sent as they are.
    """

    statement: str
    url: str
    doi: str
    quotes: list[str]
    cited_title: str
    cited_authors: list[str]
    cited_year: str
    cited_journal: str


class VerifyBatchItem(TypedDict, total=False):
    """Per-item shape for ``verify_batch``.

    All fields optional — callers can pass any subset. Type-only:
    the SDK accepts plain dicts at runtime and does no Pydantic
    coercion. The TypedDict exists purely so IDEs autocomplete the
    per-item keys (``claim``, ``language``, …).

    ``claim`` is the item's claim; ``text`` is accepted as an alias
    (``claim`` wins if both are given).

    Precedence on conflicting language: per-item ``language`` overrides
    the batch-wide ``language`` kwarg on ``verify_batch``, which overrides
    the implicit English default. The SDK forwards both verbatim; the
    server is authoritative on the merge.
    """

    claim: str
    text: str
    language: str
    source_url: str
    webhook_url: str
    idempotency_key: str
    # 'private' (default) or 'unlisted' (link-readable, never listed).
    # Per-item value overrides the batch-wide ``visibility`` default.
    visibility: str
    # 'standard' (default, 10 credits) or 'low' (shallower check — fewer
    # sources, faster, 5 credits). Per-item value overrides the batch-wide
    # ``depth`` default.
    depth: str


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
        try:
            self._p._request("DELETE", f"/verifications/{vid}", options=options)
            return True
        except LenzError as exc:
            # Idempotent DELETE: if the row was already gone (e.g. previous
            # request succeeded but the network reply was lost), treat as
            # success rather than surfacing a confusing 404. A 404 in another
            # API version is not read as this one's.
            if exc.status_code == 404 and not isinstance(exc, LenzApiVersionError):
                return True
            raise

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
        payload: dict[str, Any] = {"message": message}
        if language:
            payload["language"] = language
        headers = {}
        key = _call_key(idempotency_key, idempotency)
        if key:
            headers["Idempotency-Key"] = key
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
        if sort == "random":
            raise ValueError('iter cannot walk sort="random" (each page is a fresh sample); call library.list instead.')
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


def _walk(fetch: Callable[[int], VerificationList | LibraryList], page: int) -> Iterator[Any]:
    """The items of ``fetch(page)``, ``fetch(page + 1)``, ... A generator: no
    page is fetched before its first item is asked for.

    Stops after a page that is short or empty, once the pages read reach the
    response's ``total``, when a response states no usable ``page_size``, and
    (without yielding it) when the server answers another page than the one
    asked for, which a server clamping a page past the end would repeat."""
    while True:
        current = fetch(page)
        sent = current.model_fields_set
        if "page" in sent and current.page != page:
            return
        yield from current.items
        size = current.page_size if "page_size" in sent else 0
        if not current.items or size <= 0 or len(current.items) < size:
            return
        if "total" in sent and page * size >= current.total:
            return
        page += 1


def _first_page(page: int) -> int:
    """``page`` for an iterator, refused unless it is 1 or more."""
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ValueError(f"iter starts at page 1 or later (got page={page!r}).")
    return page


def _segment(value: Any, message: str) -> str:
    """An id as ONE path segment: percent-encoded whole, so a ``/``, ``?``, ``#``
    or ``%`` in it cannot leave the intended path (httpx would otherwise cut the
    URL there). ``""``, ``"."`` and ``".."`` raise ``ValueError(message)``: a
    path normaliser eats the dots, and an empty id names the collection."""
    if not isinstance(value, str) or value in ("", ".", ".."):
        raise ValueError(message)
    return quote(value, safe="")


def _call_key(idempotency_key: str | None, idempotency: bool) -> str | None:
    """The ``Idempotency-Key`` for one call: the caller's own, else a random
    one generated once per call (so every retry of that call carries it), else
    none when the caller opted out.

    Never derived from the request body: the same body sent again later is a
    new request, and a body-derived key would replay the first answer.
    """
    if idempotency_key is not None:
        return idempotency_key
    return uuid.uuid4().hex if idempotency else None


@contextmanager
def _carrying_key(key: str | None, *, unreadable: bool = False) -> Iterator[None]:
    """Put ``key`` on any ``LenzError`` raised inside (``exc.idempotency_key``)
    that does not carry one yet: every error of a call that sent a key says
    which, so a resend can reuse it. With ``unreadable``, also on a
    ``ValueError`` (an answer whose body is not JSON), whose class stays the
    one 2.x raised."""
    try:
        yield
    except LenzError as exc:
        if key and exc.idempotency_key is None:
            exc.idempotency_key = key
        raise
    except ValueError as exc:
        if unreadable and key and getattr(exc, "idempotency_key", None) is None:
            exc.idempotency_key = key  # type: ignore[attr-defined]
        raise


def _names_the_job(field: str) -> Callable[[Any], bool]:
    """Whether a 409 ``idempotency_conflict`` body names the job the first
    request created (``review_id`` / ``citecheck_id``): that answer settles the
    call, so it is not sent again."""

    def check(body: Any) -> bool:
        value = body.get(field) if isinstance(body, dict) else None
        return isinstance(value, str) and bool(value)

    return check


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
        # The same rule as every request option: refused here, before the
        # client exists, rather than failing (or retrying forever) later.
        timeout = _check_timeout(timeout, "Lenz()")
        max_retries = _check_retries(max_retries, "Lenz()")
        self._api_key = api_key or os.environ.get("LENZ_API_KEY") or ""
        self._base_url = (base_url or os.environ.get("LENZ_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._owns_client = http_client is None
        self._options = _ClientOptions()
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
        if not isinstance(timeout, NotGiven):
            timeout = _check_timeout(timeout, "with_options()")
        if not isinstance(max_retries, NotGiven):
            max_retries = _check_retries(max_retries, "with_options()")
        headers = _check_headers(extra_headers, "with_options()")
        layer = self._options
        clone = copy.copy(self)
        clone._owns_client = False
        clone._options = _ClientOptions(
            # A snapshot: each copy holds its own ``httpx.Timeout``.
            timeout=_snapshot(layer.timeout) if isinstance(timeout, NotGiven) else timeout,
            max_retries=layer.max_retries if isinstance(max_retries, NotGiven) else max_retries,
            headers=_merge_headers(layer.headers, headers),
        )
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
        language. Domain / status enums stay English.

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
        if claims is not None and (claim or text):
            raise ValueError("assess takes either one claim (claim=) or a list (claims=), not both")
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
        chosen = claims or texts
        if not chosen:
            raise ValueError("select requires a non-empty claims=[...]")
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
        task_id = task if isinstance(task, str) else task.task_id
        _segment(task_id, "wait() requires a non-empty task_id (got an empty TaskAccepted.task_id).")
        terminal, timed_out, stopped = self._poll_to_terminal([task_id], timeout, on_progress, options=options)
        if task_id in stopped:
            raise stopped[task_id]
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

        results: list[BatchItemResult] = []
        for it in accepted.items:  # preserve input order
            status = terminal.get(it.task_id)
            if it.task_id in stopped:
                # An error no later poll could change: removed by the
                # account's retention period (410), an unknown task (404) or
                # an answer in another API version. Terminal, with no status.
                results.append(BatchItemResult(task_id=it.task_id, claim_text=it.claim_text, status="failed"))
            elif not it.task_id or it.task_id in timed_out or status is None:
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
        if not text or not text.strip():
            raise ValueError("review() needs the draft text, or one public http(s) URL.")
        payload: dict[str, Any] = {"text": text}
        if language:
            payload["language"] = language
        if webhook_url is not None:
            payload["webhook_url"] = webhook_url
        if visibility:
            payload["visibility"] = visibility
        escalate: dict[str, Any] = {}
        for name, selector in (("verdicts", verdicts), ("confidence", confidence)):
            if selector is None:
                continue
            if isinstance(selector, str):
                raise ValueError(f"{name} is a list of strings, e.g. {name}=[{selector!r}].")
            escalate[name] = list(selector)
        for name, value in (
            ("max_assessments", max_assessments),
            ("max_verifications", max_verifications),
            ("depth", depth),
        ):
            if value is not None:
                escalate[name] = value
        # 0 means no citation check, the server default: sent as nothing, so
        # the body (and its idempotency hash) is what it is without the option.
        if max_citations:
            escalate["max_citations"] = max_citations
        # Sent only when asked, for the same reason.
        if suggest_edits:
            escalate["suggest_edits"] = True
        if escalate:
            payload["escalate"] = escalate
        headers = {"Idempotency-Key": idempotency_key or uuid.uuid4().hex}
        try:
            body = self._request(
                "POST",
                "/review",
                json=payload,
                headers=headers,
                conflict_settles=_names_the_job("review_id"),
                options=options,
            )
        except LenzError as exc:
            # A retried submit (same key) that lands while the first attempt's
            # review is still being created answers 409 with that review's id:
            # it exists, so this call started it.
            conflict = exc.body if isinstance(exc.body, dict) else {}
            review_id = conflict.get("review_id")
            if (
                exc.status_code == 409
                and exc.code == "idempotency_conflict"
                and isinstance(review_id, str)
                and review_id
            ):
                return ReviewStarted(review_id=review_id, status="queued")
            raise
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
        if view == "issues":
            body = self._request("GET", f"/reviews/{rid}", params={"view": "issues"}, options=options)
            return ReviewIssues.model_validate(body)
        if view != "full":
            raise ValueError(f"view must be 'full' or 'issues' (got {view!r}).")
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
        key = idempotency_key or uuid.uuid4().hex
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
        has_text = bool(text and text.strip())
        if has_text == (pairs is not None):
            raise ValueError("citecheck() needs exactly one of text and pairs.")
        if pairs is not None and max_citations is not None:
            raise ValueError("max_citations goes with text: every pair is checked.")
        payload: dict[str, Any] = {"text": text} if has_text else {"pairs": [dict(p) for p in pairs or []]}
        if max_citations is not None:
            payload["max_citations"] = max_citations
        if language:
            payload["language"] = language
        if webhook_url is not None:
            payload["webhook_url"] = webhook_url
        headers = {"Idempotency-Key": idempotency_key or uuid.uuid4().hex}
        try:
            body = self._request(
                "POST",
                "/citecheck",
                json=payload,
                headers=headers,
                conflict_settles=_names_the_job("citecheck_id"),
                options=options,
            )
        except LenzError as exc:
            # A retried submit (same key) that lands while the first attempt's
            # check is still being created answers 409 naming that check: it
            # exists, so this call started it.
            conflict = exc.body if isinstance(exc.body, dict) else {}
            existing = conflict.get("citecheck_id")
            if exc.status_code == 409 and exc.code == "idempotency_conflict" and isinstance(existing, str) and existing:
                return CitecheckStarted(citecheck_id=existing, status="queued")
            raise
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
        key = idempotency_key or uuid.uuid4().hex
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

        def timed_out(last: Citecheck | None) -> Exception:
            return CitecheckTimeout(
                message=f"citecheck_and_wait timed out after {timeout}s",
                cause="The citation check is still running server-side.",
                fix=f"Read it later with client.get_citecheck('{citecheck_id}').",
                doc_url="https://lenz.io/docs/citations",
                citecheck_id=citecheck_id,
                partial=last,
            )

        return self._wait_job(
            f"/citechecks/{_segment(citecheck_id, '_wait_citecheck() needs a citecheck_id.')}",
            timeout=timeout,
            on_update=on_update,
            parse=lambda body: Citecheck.model_validate(body) if _is_citecheck_body(body, citecheck_id) else None,
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

        def timed_out(last: ReviewFull | None) -> Exception:
            return ReviewTimeout(
                message=f"review_and_wait timed out after {timeout}s",
                cause="The review is still running server-side.",
                fix=f"Read it later with client.get_review('{review_id}').",
                doc_url="https://lenz.io/docs/quickstart",
                review_id=review_id,
                partial=last,
            )

        return self._wait_job(
            f"/reviews/{_segment(review_id, '_wait_review() needs a review_id.')}",
            timeout=timeout,
            on_update=on_update,
            parse=lambda body: ReviewFull.model_validate(body) if _is_full_review_body(body, review_id) else None,
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
        deadline = time.monotonic() + timeout
        last: _Job | None = None
        last_dump: dict[str, Any] | None = None
        first = timeout <= 0  # ``timeout <= 0`` reads once, as in 2.x
        while True:
            # One request per poll, bounded by what is left of the deadline:
            # the client's own retry ladder inside a poll could run minutes
            # past it. A failed poll is retried on the next round instead.
            # Once the deadline is spent no poll starts (``timeout <= 0``
            # still reads once, as in 2.x).
            stated_wait: float | None = None
            job: _Job | None = None
            remaining = deadline - time.monotonic()
            if remaining <= 0 and not first:
                raise timed_out(last)
            first = False
            try:
                body = self._request("GET", path, max_retries=0, timeout=self._poll_timeout(remaining), options=options)
                job = parse(body)
                if job is None:
                    raise ValueError("not this job's body")
            except UnicodeEncodeError:
                # The request could not be built (nothing was sent): no later
                # poll would do better, so it is not read as an unreadable body.
                raise
            except ValueError:
                # A body this release cannot read (pydantic's ValidationError
                # is a ValueError): read again next round, as for a 5xx.
                logger.debug("unreadable body for %s", path, exc_info=True)
                job = None
            except (LenzAPIError, LenzRateLimitError) as exc:
                # A stated wait paces the next poll, capped like the retry
                # ladder caps it: an untyped proxy 503 can state an hour.
                wait = getattr(exc, "retry_after", None)
                stated_wait = float(min(wait, MAX_RETRY_AFTER_SLEEP)) if isinstance(wait, int) and wait > 0 else None
            if job is not None:
                dump = job.model_dump()
                if dump != last_dump:
                    last_dump = dump
                    if on_update is not None:
                        try:
                            on_update(job.model_copy(deep=True))
                        except Exception:
                            logger.debug("on_update callback raised for %s", path, exc_info=True)
                last = job
                if job.status == "completed":
                    return job
                if job.status in ("failed", "cancelled"):
                    raise failed(job)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise timed_out(last)
            if stated_wait is not None:
                sleep_for = max(REVIEW_POLL_FLOOR, stated_wait)
            else:
                hint = last.poll_after_seconds if last is not None else None
                sleep_for = REVIEW_POLL_DEFAULT if hint is None else max(REVIEW_POLL_FLOOR, float(hint))
            time.sleep(min(sleep_for, remaining))

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
        pending = list(task_ids)
        stopped: dict[str, LenzError] = {}
        terminal: dict[str, TaskStatus] = {}
        timed_out: set[str] = set()
        deadline = time.monotonic() + timeout
        backoff_idx = 0
        # ``timeout <= 0`` reads every id once, as in 2.x; otherwise no poll
        # starts once the deadline is spent.
        one_shot = timeout <= 0
        first_round = True
        while pending:
            still_pending: list[str] = []
            server_hint: float | None = None
            stated_wait: float | None = None
            for task_id in pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0 and not (one_shot and first_round):
                    # The deadline is spent: no poll starts past it.
                    still_pending.append(task_id)
                    continue
                try:
                    body = self._request(
                        "GET",
                        f"/verify/status/{quote(task_id, safe='')}",
                        max_retries=0,
                        timeout=self._poll_timeout(remaining),
                        options=options,
                    )
                    status = TaskStatus.model_validate(body)
                except LenzAuthError:
                    # The key itself is refused: every other id would answer
                    # the same, so the whole wait ends with it.
                    raise
                except (LenzGoneError, LenzNotFoundError, LenzApiVersionError) as exc:
                    # Terminal for this id: no later poll will say otherwise.
                    # (The API never answers 410 for a task that is still
                    # running; a version error answers the same again.)
                    stopped[task_id] = exc
                    continue
                except LenzError as exc:
                    # Don't let one id's poll failure abort the rest — retry it
                    # next round (bounded by the deadline below), after the
                    # wait it stated, capped like the retry ladder caps it.
                    wait = getattr(exc, "retry_after", None)
                    if isinstance(wait, int) and not isinstance(wait, bool) and wait > 0:
                        wait_s = float(min(wait, MAX_RETRY_AFTER_SLEEP))
                        stated_wait = wait_s if stated_wait is None else max(stated_wait, wait_s)
                    still_pending.append(task_id)
                    continue
                if status.status in _TERMINAL_STATUSES:
                    terminal[task_id] = status
                else:
                    still_pending.append(task_id)
                    hint = _poll_hint(status.progress)
                    # The shortest hint wins: with a batch in flight, waiting
                    # the longest one would starve the fastest claim.
                    if hint is not None and (server_hint is None or hint < server_hint):
                        server_hint = hint
                    if on_progress is not None:
                        try:
                            # A copy — a caller must not be able to mutate our state.
                            on_progress(task_id, status.progress.model_copy(deep=True))
                        except Exception:
                            logger.debug("on_progress callback raised for task %s", task_id, exc_info=True)
            pending = still_pending
            first_round = False
            if not pending:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out.update(pending)
                break
            if server_hint is not None:
                sleep_for = server_hint
            else:
                sleep_for = min(POLL_BACKOFF[min(backoff_idx, len(POLL_BACKOFF) - 1)], POLL_BACKOFF_CAP)
            if stated_wait is not None:
                sleep_for = max(sleep_for, stated_wait)
            sleep_for = min(sleep_for, remaining)
            time.sleep(sleep_for)
            backoff_idx += 1
        return terminal, timed_out, stopped

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
                hint=status.hint,
                payload=status.model_dump(),
            )
        # failed, or cancelled elsewhere (the same outcome, as in 2.x, where the
        # original shape said ``failed`` with ``failure_class`` ``cancelled``).
        # ``error`` is the 2.x sentence, rebuilt from ``failure``.
        detail = status.error or status.failure_detail or status.failure_reason or "unknown"
        if status.retryable:
            fix = "Transient provider outage — retry the same request after a short wait."
        else:
            fix = "Retry with a different claim, or check status.error for the diagnostic."
        raise LenzPipelineError(
            message=f"Pipeline failed: {detail}",
            cause=detail,
            fix=fix,
            doc_url="https://lenz.io/docs/errors",
            task_id=task_id,
            failure_reason=status.failure_reason,
            failure_class=status.failure_class,
            # Coerce like the Node SDK: only a real boolean is a retry signal;
            # anything else (a stringy "true", a future enum) reads as unknown.
            retryable=status.retryable if isinstance(status.retryable, bool) else None,
            # Parity with the Node SDK, which has always carried it.
            hint=status.hint or "",
        )

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
        payload: dict[str, Any] = {
            "text": claim or text,
            "source_url": source_url,
        }
        # Omit-when-empty: no ``webhook_url`` means the key's default webhook.
        # An empty string is never sent, so a request keeps that meaning on
        # every API version (a newer one reads ``""`` as "no webhook").
        if not _blank_webhook_url(webhook_url):
            payload["webhook_url"] = webhook_url
        # Omit-when-empty so existing English callers keep byte-identical
        # request bodies (no extra "language": "" key).
        if language:
            payload["language"] = language
        # Omit-when-empty: the server defaults to 'private'.
        if visibility:
            payload["visibility"] = visibility
        # Omit-when-empty: the server defaults to 'standard'.
        if depth:
            payload["depth"] = depth
        headers = {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
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
        # ``webhook_url`` and ``language`` are batch-wide defaults; any
        # per-item value on a claim dict overrides them server-side.
        # Per-item items are validated as plain dicts at runtime — the
        # ``VerifyBatchItem`` TypedDict is purely for IDE autocompletion
        # (revised SDK plan decision 1C — no Pydantic coercion, keep the
        # runtime contract a plain dict).
        payload: dict[str, Any] = {"claims": [_batch_item_body(c) for c in claims]}
        # Omit-when-empty, as on ``verify``: no ``webhook_url`` means the key's default.
        if not _blank_webhook_url(webhook_url):
            payload["webhook_url"] = webhook_url
        if language:
            payload["language"] = language
        if visibility:
            payload["visibility"] = visibility
        if depth:
            payload["depth"] = depth
        headers = {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
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
        if remaining <= 0:
            return None
        layer = self._options.timeout
        own = self._client.timeout if isinstance(layer, NotGiven) else httpx.Timeout(layer)

        def cap(phase: float | None) -> float:
            return remaining if phase is None else min(phase, remaining)

        return httpx.Timeout(connect=cap(own.connect), read=cap(own.read), write=cap(own.write), pool=cap(own.pool))

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
        payload: dict[str, Any] = {"text": text}
        if language:
            payload["language"] = language
        # No client-side length check on `focus`: the server's 422 is the
        # contract, and a cap duplicated here would drift from it.
        if focus:
            payload["focus"] = focus
        # Sent only when set, so an explicit False reaches the server and an
        # omitted value leaves the server default in charge.
        if locate is not None:
            payload["locate"] = locate
        headers = {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
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
        # The single form keeps its historical wire body (`text`). The list
        # form sends `claims` and never `text` — the server rejects a body
        # carrying both with a 422. No client-side count / length checks on
        # the list: the server's 422 is the contract, and a cap duplicated
        # here would drift from it.
        payload: dict[str, Any]
        if claims is not None:
            payload = {"claims": list(claims)}
        else:
            payload = {"text": text}
        if language:
            payload["language"] = language
        # Sent only when asked, so a request without the option (and its
        # idempotency body) is exactly what it was before.
        if suggest_rewrite:
            payload["suggest_rewrite"] = True
        headers = {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
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
        headers = {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
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
        resolved, retries, option_headers = _resolve(
            options, self._options, self._client.timeout, self._max_retries, floor
        )
        if timeout is not None:
            resolved = httpx.Timeout(timeout)
        if max_retries is not None:
            retries = max_retries
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
        if auth_required and not self._api_key:
            raise LenzAuthError(
                message="API key required",
                cause="This method requires authentication; no API key was provided.",
                fix=(
                    "Pass api_key= to Lenz(), set LENZ_API_KEY env var, or get one at "
                    "https://lenz.io/api-credentials. Library endpoints work without a key."
                ),
                doc_url="https://lenz.io/docs/auth",
            )

        url = f"{self._base_url}{path}"
        req_headers = dict(headers or {})
        # The request options' headers, over the method's own (case-insensitive;
        # none is added when no option was given). httpx then puts the
        # ``httpx.Client``'s own headers under them, so an option header also
        # replaces a default one like ``User-Agent``.
        for name, value in option_headers:
            for same in [k for k in req_headers if k.lower() == name.lower()]:
                del req_headers[same]
            req_headers[name] = value
        # Attach the bearer on authed endpoints AND on opt-in optional-auth ones
        # (`auth_optional=True`). The server returns a caller's own private/hidden
        # rows only to the owning bearer, so `verifications.get` must send a key it
        # has (→ `lenz show` on a fresh private API claim). Purely public reads
        # (`library.list`) stay anonymous — no `auth_optional`, no key on the wire —
        # so a key never reaches an endpoint that doesn't need it.
        if self._api_key and (auth_required or auth_optional):
            req_headers["Authorization"] = f"Bearer {self._api_key}"
        req_headers.setdefault("Content-Type", "application/json")
        # The version is part of the request, not of the HTTP client: a client
        # passed as ``http_client=`` may carry no version header or a stale one.
        req_headers[_VERSION_HEADER] = API_VERSION
        # ``None``: the ``httpx.Client``'s own timeout.
        req_timeout = httpx.USE_CLIENT_DEFAULT if timeout is None else timeout

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
                # Connect / read / write failures, timeouts, a server that
                # hung up (RemoteProtocolError), a proxy failure: worth sending
                # again.
                last_exc = exc
                if attempt >= retries:
                    # Subclasses of LenzAPIError, which is what 2.x raised.
                    cls = LenzRequestTimeoutError if isinstance(exc, httpx.TimeoutException) else LenzConnectionError
                    raise cls(
                        message=f"{method} {path} failed after {attempt + 1} attempts: {exc}",
                        cause=str(exc),
                        fix="Check your network connection; verify base_url is reachable.",
                        doc_url="https://lenz.io/docs/errors",
                    ) from exc
                time.sleep(_retry_sleep(attempt))
                continue

            _check_served_version(response)

            if response.status_code < 400:
                return response.json() if response.content else {}

            # A 409 ``idempotency_conflict``: the first request with this key is
            # still running. Send the SAME key and body again after the stated
            # wait (capped) or the backoff, inside this call's retry budget; a
            # new key would run the work twice. A conflict that names the job
            # the first request created settles the call (``conflict_settles``).
            if (
                attempt < retries
                and response.status_code == 409
                and req_headers.get("Idempotency-Key")
                and _body_error_code(response) == "idempotency_conflict"
                and not (conflict_settles is not None and conflict_settles(_json_or_none(response)))
            ):
                stated = _stated_retry_after(response)
                time.sleep(stated if stated is not None and stated <= MAX_RETRY_AFTER_SLEEP else _retry_sleep(attempt))
                continue

            # Error path. Retry on 5xx and 429; otherwise raise immediately.
            #
            # A stated wait is honored only up to MAX_RETRY_AFTER_SLEEP. Past
            # that, whether we abort or keep retrying is decided by the typed
            # body ``code`` — NOT by the status number:
            #
            #  * 429 — raise. The /extract daily cap sends
            #    seconds-until-UTC-midnight, so sleeping it blocks the call for
            #    most of a day, three times over. The caller gets the true
            #    retry_after and can schedule the work.
            #  * 503 carrying a Lenz code in UPSTREAM_503_CODES
            #    (``upstream_unavailable`` / ``capacity``) — raise, same
            #    reasoning. These are the server's own shed/exhaustion
            #    responses; they state an honest 90-120s and burning the
            #    1/2/4s ladder against them is the opposite of what the header
            #    asks (map_response_to_error types them
            #    LenzUpstreamUnavailableError, carrying the true retry_after).
            #  * every other 5xx, including an UNTYPED 503 — keep retrying on
            #    our own backoff. A Cloud Run / CDN / load-balancer
            #    maintenance-or-overload 503 states a long wait and carries no
            #    Lenz code; the server is down, not pacing us, so an hour-long
            #    Retry-After must become backoff — not an hour-long sleep, and
            #    not an abort of a request our ladder might still satisfy.
            #  * a 429 whose code is in NO_RETRY_429_CODES — raise at once,
            #    whatever the wait (see its definition).
            if (
                attempt < retries
                and (response.status_code >= 500 or response.status_code == 429)
                and not (response.status_code == 429 and _body_error_code(response) in NO_RETRY_429_CODES)
            ):
                stated = _stated_retry_after(response)
                if stated is not None and stated <= MAX_RETRY_AFTER_SLEEP:
                    time.sleep(stated)
                    continue
                if stated is None or not _aborts_on_long_stated_wait(response):
                    time.sleep(_retry_sleep(attempt))
                    continue

            raise map_response_to_error(
                response.status_code,
                response.content,
                dict(response.headers),
                endpoint=(method, path),
            )

        # Shouldn't reach here, but guard.
        if last_exc:
            raise LenzAPIError(message=str(last_exc), cause=str(last_exc)) from last_exc
        raise LenzAPIError(message=f"{method} {path} failed without diagnostic")


def _unexpected_answer(method: str, path: str) -> LenzAPIError:
    """A 200 whose body is not the thing asked for (a proxy page, another
    task's body): an error, never a default-valued result."""
    return LenzAPIError(
        message=f"{method} {path} returned an unexpected response body.",
        cause="The answer is not the shape the API documents for this call.",
        fix="Retry; if it persists, contact support (https://lenz.io/contact) with the request.",
        doc_url="https://lenz.io/docs/errors",
    )


def _is_cancel_body(body: Any, task_id: str) -> bool:
    """Whether a 200 is this task's cancel result: its id, a boolean
    ``cancelled`` and a status."""
    return (
        isinstance(body, dict)
        and body.get("task_id") == task_id
        and isinstance(body.get("cancelled"), bool)
        and isinstance(body.get("status"), str)
    )


def _is_full_review_body(body: Any, review_id: str) -> bool:
    """Whether a 200 is this review's full view: its id, a status, and the
    three lists. A proxy page or another review's body is a failed poll, never
    a result (an empty ``{"status": "completed"}`` would otherwise end the
    wait with no issues)."""
    return (
        isinstance(body, dict)
        and body.get("review_id") == review_id
        and isinstance(body.get("status"), str)
        and all(isinstance(body.get(k), list) for k in ("issues", "failures", "claims"))
    )


def _is_citecheck_body(body: Any, citecheck_id: str) -> bool:
    """Whether a 200 is this citation check: its id, a status and the three
    lists. Anything else is a failed poll, never a result."""
    return (
        isinstance(body, dict)
        and body.get("citecheck_id") == citecheck_id
        and isinstance(body.get("status"), str)
        and all(isinstance(body.get(k), list) for k in ("citations", "citation_issues", "citation_failures"))
    )


#: What the original response shape said of a task cancelled elsewhere, which
#: the current one states as the status ``cancelled`` and no failure block.
_CANCELLED_FAILURE = {
    "failure_reason": "cancelled",
    "failure_class": "cancelled",
    "retryable": False,
    "docs_url": "https://lenz.io/docs/errors#cancelled",
}


def _failure_of(job: Citecheck | ReviewFull) -> FailureBlock | None:
    """Why a job ended without a result: its failure block, or the cancelled
    one for a job cancelled elsewhere."""
    if job.failure is None and job.status == "cancelled":
        return FailureBlock.model_validate(_CANCELLED_FAILURE)
    return job.failure


def _citecheck_failed(check: Citecheck) -> CitecheckFailed:
    failure = _failure_of(check)
    reason = (failure.failure_reason if failure else None) or ""
    hint = (failure.hint if failure else None) or ""
    retryable = failure.retryable if failure is not None and isinstance(failure.retryable, bool) else None
    return CitecheckFailed(
        message=f"Citation check failed: {reason or 'the server sent no failure block'}",
        cause=hint or reason or "No failure block on the failed check.",
        fix=hint or ("Retry the same request after a short wait." if retryable else "Check the request and resubmit."),
        doc_url=(failure.docs_url if failure else "") or "https://lenz.io/docs/errors",
        citecheck_id=check.citecheck_id,
        error_code=reason,
        failure_reason=reason,
        failure_class=(failure.failure_class if failure else "") or "",
        retryable=retryable,
        hint=hint,
        citecheck=check,
    )


def _review_failed(review: ReviewFull) -> ReviewFailed:
    failure = _failure_of(review)
    reason = (failure.failure_reason if failure else None) or ""
    hint = (failure.hint if failure else None) or ""
    retryable = failure.retryable if failure is not None and isinstance(failure.retryable, bool) else None
    return ReviewFailed(
        message=f"Review failed: {reason or 'the server sent no failure block'}",
        cause=hint or reason or "No failure block on the failed review.",
        fix=hint or ("Retry the same draft after a short wait." if retryable else "Check the draft and resubmit."),
        doc_url=(failure.docs_url if failure else "") or "https://lenz.io/docs/errors",
        review_id=review.review_id,
        error_code=reason,
        failure_reason=reason,
        failure_class=(failure.failure_class if failure else "") or "",
        retryable=retryable,
        hint=hint,
        review=review,
    )


def _check_served_version(response: httpx.Response) -> None:
    """Refuse an answer in an API version this SDK does not read.

    A response without the header proceeds (a proxy or an old server may not
    send it). Webhook payloads never pass through here.
    """
    served = (response.headers.get(_VERSION_HEADER) or "").strip()
    if not served or served == API_VERSION:
        return
    try:
        parsed = response.json() if response.content else None
    except ValueError:
        parsed = None
    raise LenzApiVersionError(
        message=f"The API answered in version {served}; lenz-io 3.x reads {API_VERSION} only.",
        cause=f"The response carries {_VERSION_HEADER}: {served}.",
        fix=(
            "If this persists, contact support (https://lenz.io/contact) with the request id; "
            "lenz-io 2.x reads both versions."
        ),
        doc_url="https://lenz.io/docs/errors",
        request_id=response.headers.get("X-Request-ID") or "",
        status_code=response.status_code,
        body=parsed if isinstance(parsed, dict) else None,
        api_version=served,
    )


def _stated_retry_after(response: httpx.Response) -> int | None:
    """Seconds the server says to wait, or None if it didn't say.

    Reads the ``Retry-After`` header first, then the body's
    ``reset_in_seconds`` (429 shapes), then the body's ``retry_after``
    (the 503 shapes carry the wait under that key). Returns None (rather
    than 0) on an absent or unparseable value so the caller can tell
    "server stated no wait" apart from "server said wait 0 seconds" and
    fall back to its own backoff.
    """
    raw = response.headers.get("Retry-After")
    if raw is None or str(raw).strip() == "":
        try:
            body = response.json()
        except Exception:
            # A non-JSON body is not exceptional — fall back to backoff.
            return None
        if not isinstance(body, dict):
            return None
        raw = body.get("reset_in_seconds")
        if raw is None or str(raw).strip() == "":
            raw = body.get("retry_after")
        if raw is None or str(raw).strip() == "":
            raw = body.get("retry_after_seconds")
    if raw is None or str(raw).strip() == "":
        return None
    try:
        # Floored at zero: `Retry-After: -5` is malformed, and time.sleep()
        # raises ValueError on a negative — which would escape the retry
        # ladder as a bare ValueError, defeating the typed-exception contract.
        seconds = float(raw)
        if not math.isfinite(seconds):
            # ``inf``, ``nan``, ``1e999``: no stated wait (the backoff ladder
            # runs), as in the Node SDK; never an OverflowError.
            return None
        # A huge finite wait is clamped (still past every cap).
        return int(min(max(seconds, 0.0), MAX_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        return None


def _json_or_none(response: httpx.Response) -> Any:
    try:
        return response.json()
    except Exception:
        return None


def _body_error_code(response: httpx.Response) -> str:
    """The server's machine-readable ``code`` from the body, or ``""``.

    Reads it exactly the way ``map_response_to_error`` does — string-typed
    only, so a malformed ``code: 42`` reads as ``""`` rather than ``"42"``
    and nothing branches on a value the server never meant as a code.
    """
    try:
        body = response.json()
    except Exception:
        return ""
    if not isinstance(body, dict):
        return ""
    code = body.get("code")
    return code if isinstance(code, str) else ""


def _aborts_on_long_stated_wait(response: httpx.Response) -> bool:
    """Whether a stated wait past the cap should abort instead of back off.

    True for 429 (always) and for a 503 the server typed as its own
    shed/exhaustion response. An untyped 503 — the ordinary proxy /
    maintenance shape — is deliberately False: it keeps the ladder.
    """
    if response.status_code == 429:
        return True
    return response.status_code == 503 and _body_error_code(response) in UPSTREAM_503_CODES


def _retry_sleep(attempt: int) -> float:
    if attempt < len(RETRY_BACKOFF):
        return RETRY_BACKOFF[attempt]
    return RETRY_BACKOFF[-1]


__all__ = ["API_VERSION", "DEFAULT_BASE_URL", "NOT_GIVEN", "Lenz", "NotGiven", "VerifyBatchItem"]
