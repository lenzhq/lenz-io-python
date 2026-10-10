"""The shared core of the sync and async clients: every decision, no I/O.

Private. ``Lenz`` (``client.py``) and ``AsyncLenz`` (``async_client.py``) are
two thin drivers over this module: they send the requests, sleep and call the
callbacks, and nothing else. Everything a call decides lives here once:
request bodies and their validation, the request options, the headers and
timeouts of one request, the retry ladder (what to do after a response or a
transport error), the answers that settle a call (an already-deleted
verification, a 409 naming the job a resend started), path encoding, the
poll timeouts, pagination and the results of the wait helpers.

Nothing here reads the clock, sleeps or makes a request: the drivers pass the
time in (``_polling.py`` holds the poll loops' state the same way).
"""

from __future__ import annotations

import logging
import math
import numbers
import operator
import re
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Final, Literal, TypedDict
from urllib.parse import quote

import httpx

from . import __version__
from .errors import (
    MAX_RETRY_AFTER_SLEEP as MAX_RETRY_AFTER_SLEEP,
    NO_RETRY_429_CODES,
    UPSTREAM_503_CODES,
    CitecheckFailed,
    CitecheckTimeout,
    LenzAPIError,
    LenzApiVersionError,
    LenzAuthError,
    LenzConnectionError,
    LenzError,
    LenzNeedsInputError,
    LenzPipelineError,
    LenzRequestTimeoutError,
    LenzTimeoutError,
    ReviewFailed,
    ReviewTimeout,
    map_response_to_error,
)
from .models import (
    BatchAccepted,
    BatchItemResult,
    Citecheck,
    CitecheckStarted,
    ExtractedClaims,
    FailureBlock,
    LibraryList,
    ReviewFull,
    ReviewStarted,
    TaskStatus,
    Verification,
    VerificationList,
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
NOT_GIVEN: Final[NotGiven] = NotGiven()

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


_NO_OPTIONS: Final[_CallOptions] = _CallOptions()


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


def _async_user_agent() -> str:
    """``AsyncLenz``'s: the same product token (what the API reads to tell
    the Python SDK from other clients), and ``async`` in the comment."""
    return f"lenz-io-python/{__version__} (httpx {httpx.__version__}; async)"


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


def _page_step(current: VerificationList | LibraryList, page: int) -> tuple[list[Any], int | None]:
    """One page of an iterator: the items to hand out, and the next page to
    fetch after them (``None`` to stop).

    Stops after a page that is short or empty, once the pages read reach the
    response's ``total``, when a response states no usable ``page_size``, and
    (handing out nothing) when the server answers another page than the one
    asked for, which a server clamping a page past the end would repeat."""
    sent = current.model_fields_set
    if "page" in sent and current.page != page:
        return [], None
    items = list(current.items)
    size = current.page_size if "page_size" in sent else 0
    if not current.items or size <= 0 or len(current.items) < size:
        return items, None
    if "total" in sent and page * size >= current.total:
        return items, None
    return items, page + 1


def _walk(fetch: Callable[[int], VerificationList | LibraryList], page: int) -> Iterator[Any]:
    """The items of ``fetch(page)``, ``fetch(page + 1)``, ... (``_page_step``
    decides where to stop). A generator: no page is fetched before its first
    item is asked for."""
    next_page: int | None = page
    while next_page is not None:
        page = next_page
        items, next_page = _page_step(fetch(page), page)
        yield from items


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


# The public names defined here are documented and imported as
# ``lenz_io.client``'s, where they always lived.
for _public in (NotGiven, CitationPair, VerifyBatchItem):
    _public.__module__ = "lenz_io.client"


# ── the client settings ──


def _client_settings(
    api_key: str | None,
    base_url: str | None,
    timeout: Any,
    max_retries: Any,
    where: str,
) -> tuple[str, str, float | httpx.Timeout | None, int]:
    """The key, base URL, timeout and retries a client is built with: the
    environment fills a missing key (``LENZ_API_KEY``) and base URL
    (``LENZ_BASE_URL``), and a bad timeout or retry count raises
    ``ValueError`` here, before the client exists."""
    import os

    # The same rule as every request option: refused here, before the
    # client exists, rather than failing (or retrying forever) later.
    checked_timeout = _check_timeout(timeout, where)
    retries = _check_retries(max_retries, where)
    key = api_key or os.environ.get("LENZ_API_KEY") or ""
    url = (base_url or os.environ.get("LENZ_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    return key, url, checked_timeout, retries


def _default_headers(user_agent: str) -> dict[str, str]:
    """The headers a client's own ``httpx`` client is created with."""
    return {
        "User-Agent": user_agent,
        "X-Lenz-API-Version": API_VERSION,
        "Accept": "application/json",
    }


def _with_options_layer(
    layer: _ClientOptions,
    timeout: float | httpx.Timeout | NotGiven | None,
    max_retries: int | NotGiven,
    extra_headers: Mapping[str, str | None] | None,
) -> _ClientOptions:
    """The options of a ``with_options`` copy, flattened over the copy it was
    made from; a bad value raises ``ValueError`` before the copy exists."""
    if not isinstance(timeout, NotGiven):
        timeout = _check_timeout(timeout, "with_options()")
    if not isinstance(max_retries, NotGiven):
        max_retries = _check_retries(max_retries, "with_options()")
    headers = _check_headers(extra_headers, "with_options()")
    return _ClientOptions(
        # A snapshot: each copy holds its own ``httpx.Timeout``.
        timeout=_snapshot(layer.timeout) if isinstance(timeout, NotGiven) else timeout,
        max_retries=layer.max_retries if isinstance(max_retries, NotGiven) else max_retries,
        headers=_merge_headers(layer.headers, headers),
    )


# ── request bodies (argument errors raise here, before any request) ──


def _key_header(idempotency_key: str | None) -> dict[str, str]:
    """``{"Idempotency-Key": key}``, or no header when there is no key."""
    headers = {}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _verify_payload(
    *,
    claim: str,
    text: str,
    source_url: str,
    webhook_url: str,
    language: str,
    visibility: str,
    depth: str,
) -> dict[str, Any]:
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
    return payload


def _batch_payload(
    *,
    claims: Sequence[VerifyBatchItem | dict[str, Any]],
    webhook_url: str,
    language: str,
    visibility: str,
    depth: str,
) -> dict[str, Any]:
    # ``webhook_url`` and ``language`` are batch-wide defaults; any
    # per-item value on a claim dict overrides them server-side.
    # Per-item items are validated as plain dicts at runtime — the
    # ``VerifyBatchItem`` TypedDict is purely for IDE autocompletion
    # (no Pydantic coercion, keep the runtime contract a plain dict).
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
    return payload


def _extract_payload(*, text: str, language: str, focus: str, locate: bool | None) -> dict[str, Any]:
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
    return payload


def _check_assess_forms(claim: str, text: str, claims: list[str] | None) -> None:
    if claims is not None and (claim or text):
        raise ValueError("assess takes either one claim (claim=) or a list (claims=), not both")


def _assess_payload(*, text: str, claims: list[str] | None, language: str, suggest_rewrite: bool) -> dict[str, Any]:
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
    return payload


def _select_texts(claims: list[str] | None, texts: list[str] | None) -> list[str]:
    chosen = claims or texts
    if not chosen:
        raise ValueError("select requires a non-empty claims=[...]")
    return chosen


def _ask_payload(message: str, language: str) -> dict[str, Any]:
    payload: dict[str, Any] = {"message": message}
    if language:
        payload["language"] = language
    return payload


def _library_params(
    *,
    page: int,
    sort: str,
    search: str,
    domain: str,
    entity: str,
    curated: list[str] | None,
    verdict: str,
) -> dict[str, Any]:
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
    return params


def _check_library_iter_sort(sort: str) -> None:
    if sort == "random":
        raise ValueError('iter cannot walk sort="random" (each page is a fresh sample); call library.list instead.')


def _review_payload(
    *,
    text: str,
    verdicts: list[str] | None,
    confidence: list[str] | None,
    max_assessments: int | None,
    max_verifications: int | None,
    depth: str | None,
    max_citations: int | None,
    suggest_edits: bool,
    language: str,
    webhook_url: str | None,
    visibility: str,
) -> dict[str, Any]:
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
    return payload


def _citecheck_payload(
    *,
    text: str | None,
    pairs: list[CitationPair] | None,
    max_citations: int | None,
    language: str,
    webhook_url: str | None,
) -> dict[str, Any]:
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
    return payload


def _job_key(idempotency_key: str | None) -> str:
    """A review's or citation check's ``Idempotency-Key``: the caller's, else
    a new random one (these submits always send one)."""
    return idempotency_key or uuid.uuid4().hex


def _review_params(view: str) -> dict[str, Any] | None:
    """The query of ``get_review``: ``view=issues``, nothing for the full
    view; ``ValueError`` for any other view."""
    if view == "issues":
        return {"view": "issues"}
    if view != "full":
        raise ValueError(f"view must be 'full' or 'issues' (got {view!r}).")
    return None


def _status_path(task_id: str) -> str:
    """The poll path of one verification (the id already checked)."""
    return f"/verify/status/{quote(task_id, safe='')}"


# ── answers that settle a call (per-operation error recovery) ──


def _review_started_by_conflict(exc: LenzError) -> ReviewStarted | None:
    """A retried submit (same key) that lands while the first attempt's
    review is still being created answers 409 with that review's id: it
    exists, so this call started it."""
    conflict = exc.body if isinstance(exc.body, dict) else {}
    review_id = conflict.get("review_id")
    if exc.status_code == 409 and exc.code == "idempotency_conflict" and isinstance(review_id, str) and review_id:
        return ReviewStarted(review_id=review_id, status="queued")
    return None


def _citecheck_started_by_conflict(exc: LenzError) -> CitecheckStarted | None:
    """A retried submit (same key) that lands while the first attempt's
    check is still being created answers 409 naming that check: it exists,
    so this call started it."""
    conflict = exc.body if isinstance(exc.body, dict) else {}
    existing = conflict.get("citecheck_id")
    if exc.status_code == 409 and exc.code == "idempotency_conflict" and isinstance(existing, str) and existing:
        return CitecheckStarted(citecheck_id=existing, status="queued")
    return None


def _already_deleted(exc: LenzError) -> bool | None:
    """Idempotent DELETE: if the row was already gone (e.g. previous request
    succeeded but the network reply was lost), the call succeeded rather than
    surfacing a confusing 404. A 404 in another API version is not read as
    this one's. ``None``: the error stands."""
    if exc.status_code == 404 and not isinstance(exc, LenzApiVersionError):
        return True
    return None


# ── one request: its settings, headers and the retry ladder ──


def _request_settings(
    options: _CallOptions,
    layer: _ClientOptions,
    client_timeout: httpx.Timeout,
    client_retries: int,
    floor: float | None,
    timeout: float | httpx.Timeout | None,
    max_retries: int | None,
) -> tuple[httpx.Timeout | None, int, tuple[tuple[str, str], ...]]:
    """The timeout, retry count and option headers of one request
    (``_resolve``). ``timeout`` and ``max_retries`` are the SDK's own settings
    for one request (a wait's polls): when set they win over every option."""
    resolved, retries, option_headers = _resolve(options, layer, client_timeout, client_retries, floor)
    if timeout is not None:
        resolved = httpx.Timeout(timeout)
    if max_retries is not None:
        retries = max_retries
    return resolved, retries, option_headers


def _prepare(
    *,
    api_key: str,
    base_url: str,
    path: str,
    headers: dict[str, str] | None,
    option_headers: Sequence[tuple[str, str]],
    auth_required: bool,
    auth_optional: bool,
    timeout: httpx.Timeout | None,
) -> tuple[str, dict[str, str], Any]:
    """The URL, headers and timeout one request is sent with (every attempt
    of it). Raises ``LenzAuthError`` when the call needs a key and there is
    none."""
    if auth_required and not api_key:
        raise LenzAuthError(
            message="API key required",
            cause="This method requires authentication; no API key was provided.",
            fix=(
                "Pass api_key= to Lenz(), set LENZ_API_KEY env var, or get one at "
                "https://lenz.io/api-credentials. Library endpoints work without a key."
            ),
            doc_url="https://lenz.io/docs/auth",
        )

    url = f"{base_url}{path}"
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
    if api_key and (auth_required or auth_optional):
        req_headers["Authorization"] = f"Bearer {api_key}"
    req_headers.setdefault("Content-Type", "application/json")
    # The version is part of the request, not of the HTTP client: a client
    # passed as ``http_client=`` may carry no version header or a stale one.
    req_headers[_VERSION_HEADER] = API_VERSION
    # ``None``: the ``httpx`` client's own timeout.
    req_timeout = httpx.USE_CLIENT_DEFAULT if timeout is None else timeout
    return url, req_headers, req_timeout


def _after_transport_error(exc: httpx.TransportError, attempt: int, retries: int, method: str, path: str) -> float:
    """After connect / read / write failures, timeouts, a server that hung up
    (RemoteProtocolError), a proxy failure: the seconds to sleep before
    sending again, or (on the last attempt) the error to raise."""
    if attempt >= retries:
        # Subclasses of LenzAPIError, which is what 2.x raised.
        cls = LenzRequestTimeoutError if isinstance(exc, httpx.TimeoutException) else LenzConnectionError
        raise cls(
            message=f"{method} {path} failed after {attempt + 1} attempts: {exc}",
            cause=str(exc),
            fix="Check your network connection; verify base_url is reachable.",
            doc_url="https://lenz.io/docs/errors",
        ) from exc
    return _retry_sleep(attempt)


#: What ``_after_response`` says: the answer's body (the call is done) or the
#: seconds to sleep before sending again.
_Done = tuple[Literal[True], Any]
_Again = tuple[Literal[False], float]


def _after_response(
    response: httpx.Response,
    attempt: int,
    retries: int,
    method: str,
    path: str,
    req_headers: Mapping[str, str],
    conflict_settles: Callable[[Any], bool] | None,
) -> _Done | _Again:
    """What to do with one answer: ``(True, body)`` when the call is done,
    ``(False, seconds)`` to send the same request again after sleeping that
    long; an error to raise is raised."""
    _check_served_version(response)

    if response.status_code < 400:
        return True, response.json() if response.content else {}

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
        return False, stated if stated is not None and stated <= MAX_RETRY_AFTER_SLEEP else _retry_sleep(attempt)

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
            return False, stated
        if stated is None or not _aborts_on_long_stated_wait(response):
            return False, _retry_sleep(attempt)

    raise map_response_to_error(
        response.status_code,
        response.content,
        dict(response.headers),
        endpoint=(method, path),
    )


def _exhausted(last_exc: Exception | None, method: str, path: str) -> LenzAPIError:
    """The error of a retry loop that ended without an answer or an error
    (it cannot, but guard)."""
    if last_exc:
        error = LenzAPIError(message=str(last_exc), cause=str(last_exc))
        error.__cause__ = last_exc
        return error
    return LenzAPIError(message=f"{method} {path} failed without diagnostic")


def _poll_timeout(
    layer_timeout: float | httpx.Timeout | NotGiven | None, client_timeout: httpx.Timeout, remaining: float
) -> httpx.Timeout | None:
    """The timeouts of one poll request: each phase (connect, read, write,
    pool) the copy or the client in use configured, capped by what is left of
    the wait's deadline (an unbounded phase gets just that). Past the deadline
    (only a ``timeout <= 0`` wait polls then) ``None``: the copy's or the
    client's own."""
    if remaining <= 0:
        return None
    own = client_timeout if isinstance(layer_timeout, NotGiven) else httpx.Timeout(layer_timeout)

    def cap(phase: float | None) -> float:
        return remaining if phase is None else min(phase, remaining)

    return httpx.Timeout(connect=cap(own.connect), read=cap(own.read), write=cap(own.write), pool=cap(own.pool))


# ── results of the wait helpers ──


def _verification_from_terminal(status: TaskStatus, task_id: str) -> Verification:
    """Map a terminal ``TaskStatus`` to a ``Verification`` or raise the
    matching typed error."""
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


def _wait_task_id(task: str | Any) -> str:
    """The id ``wait`` polls, checked (``ValueError`` for an empty one)."""
    task_id: str = task if isinstance(task, str) else task.task_id
    _segment(task_id, "wait() requires a non-empty task_id (got an empty TaskAccepted.task_id).")
    return task_id


def _wait_timed_out(timeout: float, task_id: str) -> LenzTimeoutError:
    return LenzTimeoutError(
        message=f"wait timed out after {timeout}s",
        cause="Pipeline still running server-side.",
        fix=f"Resume via client.get_status('{task_id}') later.",
        doc_url="https://lenz.io/docs/verify#timeout",
        task_id=task_id,
    )


def _wait_result(
    task_id: str,
    timeout: float,
    terminal: dict[str, TaskStatus],
    timed_out: set[str],
    stopped: dict[str, LenzError],
) -> TaskStatus:
    """The terminal status ``wait`` maps to its result; a stopped id's error
    or the timeout is raised."""
    if task_id in stopped:
        raise stopped[task_id]
    if task_id in timed_out:
        raise _wait_timed_out(timeout, task_id)
    return terminal[task_id]


def _batch_results(
    accepted: BatchAccepted,
    terminal: dict[str, TaskStatus],
    timed_out: set[str],
    stopped: dict[str, LenzError],
) -> list[BatchItemResult]:
    """One ``BatchItemResult`` per task the batch accepted, in input order."""
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


def _review_job(
    review_id: str, timeout: float
) -> tuple[str, Callable[[Any], ReviewFull | None], Callable[[ReviewFull | None], Exception]]:
    """The poll path, body reader and timeout error of a review wait."""

    def timed_out(last: ReviewFull | None) -> Exception:
        return ReviewTimeout(
            message=f"review_and_wait timed out after {timeout}s",
            cause="The review is still running server-side.",
            fix=f"Read it later with client.get_review('{review_id}').",
            doc_url="https://lenz.io/docs/quickstart",
            review_id=review_id,
            partial=last,
        )

    return (
        f"/reviews/{_segment(review_id, '_wait_review() needs a review_id.')}",
        lambda body: ReviewFull.model_validate(body) if _is_full_review_body(body, review_id) else None,
        timed_out,
    )


def _citecheck_job(
    citecheck_id: str, timeout: float
) -> tuple[str, Callable[[Any], Citecheck | None], Callable[[Citecheck | None], Exception]]:
    """The poll path, body reader and timeout error of a citation-check wait."""

    def timed_out(last: Citecheck | None) -> Exception:
        return CitecheckTimeout(
            message=f"citecheck_and_wait timed out after {timeout}s",
            cause="The citation check is still running server-side.",
            fix=f"Read it later with client.get_citecheck('{citecheck_id}').",
            doc_url="https://lenz.io/docs/citations",
            citecheck_id=citecheck_id,
            partial=last,
        )

    return (
        f"/citechecks/{_segment(citecheck_id, '_wait_citecheck() needs a citecheck_id.')}",
        lambda body: Citecheck.model_validate(body) if _is_citecheck_body(body, citecheck_id) else None,
        timed_out,
    )
