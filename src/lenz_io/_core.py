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

import json
import logging
import math
import numbers
import operator
import re
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Final, Literal, TypedDict, TypeVar
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from . import __version__
from .errors import (
    INVALID_BODY_TEXT_MAX,
    MAX_RETRY_AFTER_SLEEP as MAX_RETRY_AFTER_SLEEP,
    NO_RETRY_429_CODES,
    UPSTREAM_503_CODES,
    CitecheckFailed,
    CitecheckTimeout,
    LenzAPIError,
    LenzApiVersionError,
    LenzConnectionError,
    LenzError,
    LenzInvalidKeyError,
    LenzInvalidResponseError,
    LenzMissingKeyError,
    LenzNeedsInputError,
    LenzPipelineError,
    LenzRequestTimeoutError,
    LenzTimeoutError,
    LenzUsageError,
    ResponseHeaders,
    ReviewFailed,
    ReviewTimeout,
    map_response_to_error,
    unreadable_fields_error,
)
from .models import (
    LEGACY_ALIASES,
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
    _Body,
    _legacy_view,
    _Meta,
    _no_result,
    _set_raw,
)

logger = logging.getLogger("lenz_io")

_E = TypeVar("_E", bound=LenzError)

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
    raise LenzUsageError(
        f"{where}: timeout must be a number of seconds greater than 0 and at most 2,147,483, None, an "
        f"httpx.Timeout or a (connect, read, write, pool) tuple of such numbers or None (got {value!r}).",
        code="invalid_option",
        param="timeout",
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
        raise LenzUsageError(
            f"{where}: max_retries must be a whole number, 0 or more (got {value!r}).",
            code="invalid_option",
            param="max_retries",
        )
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
        raise LenzUsageError(
            f"{where}: extra_headers must be a mapping of header names to strings (got {value!r}).",
            code="invalid_header",
            param="extra_headers",
        )
    pairs: list[tuple[str, str | None]] = []
    for name, header in value.items():
        if not isinstance(name, str) or not _HEADER_NAME.fullmatch(name):
            raise LenzUsageError(
                f"{where}: a header name must be a non-empty token of ASCII letters, digits and "
                f"!#$%&'*+-.^_`|~ (got {name!r}).",
                code="invalid_header",
                param="extra_headers",
            )
        if name.lower() in _RESERVED_HEADERS:
            raise LenzUsageError(
                f"{where}: the {name} header is set by the SDK and cannot be passed in extra_headers.",
                code="invalid_header",
                param="extra_headers",
            )
        if header is not None and (not isinstance(header, str) or not _HEADER_VALUE.fullmatch(header)):
            raise LenzUsageError(
                f"{where}: the value of header {name} must be a string of visible ASCII characters, with "
                f"spaces and tabs only between them (not at either end), or None.",
                code="invalid_header",
                param="extra_headers",
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
    send_client_timeout: bool = False,
) -> tuple[httpx.Timeout | None, int, tuple[tuple[str, str], ...]]:
    """The timeout, retry count and option headers of one request. Pure: no
    I/O, no clock.

    Per field, the first that is set wins: the call's option, the copy's
    (``with_options``), the client's (the ``httpx.Client`` in use, or the
    constructor's ``timeout`` on a borrowed client, for the timeout; the
    constructor for retries). Headers merge, the call's over the copy's.

    A ``floor`` (``extract`` / ``assess``) applies to the client's timeout
    only (since 3.2; a copy's was floored too before): when its ``read`` is
    bounded and shorter than the floor, the floor replaces the whole timeout.
    A timeout the call or a copy set is used as given. A timeout of ``None``
    means "the ``httpx.Client``'s own" (httpx's ``USE_CLIENT_DEFAULT``);
    ``send_client_timeout`` sends the client's timeout on the request instead
    (a borrowed ``http_client=`` with a constructor ``timeout=``).
    """
    timeout: httpx.Timeout | None
    if call.timeout is not None:
        timeout = httpx.Timeout(call.timeout)
    elif not isinstance(layer.timeout, NotGiven):
        timeout = httpx.Timeout(layer.timeout)
    elif floor is not None and client_timeout.read is not None and client_timeout.read < floor:
        timeout = httpx.Timeout(floor)
    else:
        timeout = httpx.Timeout(client_timeout) if send_client_timeout else None
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


def _extracted(body: Any, *, locate: bool | None, context: dict[str, Any] | None = None) -> ExtractedClaims:
    """``/extract``'s answer. A body that located every claim away answers
    ``claims: []``; the 2.x field for that was ``locations=[]``, which only the
    request (``locate=True``) can tell apart from "nothing found"."""
    out = ExtractedClaims.model_validate(body, context=context)
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


#: The page sizes ``GET /verifications`` serves (it clamps anything else).
PAGE_SIZE_MIN, PAGE_SIZE_MAX = 1, 100


def _check_page_size(page_size: int | None) -> int | None:
    """``page_size`` for ``verifications.list`` / ``iter``: ``None`` (the
    server's default, not sent) or a whole number from 1 to 100, else
    ``ValueError`` before any request (the server would clamp it silently)."""
    if page_size is None:
        return None
    if isinstance(page_size, bool) or not isinstance(page_size, int) or not PAGE_SIZE_MIN <= page_size <= PAGE_SIZE_MAX:
        raise LenzUsageError(
            f"page_size must be a whole number from {PAGE_SIZE_MIN} to {PAGE_SIZE_MAX} (got page_size={page_size!r}).",
            code="invalid_page_size",
            param="page_size",
        )
    return page_size


class _PageSizeKw(TypedDict, total=False):
    page_size: int


def _page_size_kw(page_size: int | None) -> _PageSizeKw:
    """``page_size`` as a keyword for ``list`` from ``iter``, only when given:
    a subclass whose ``list`` predates the parameter keeps working."""
    return {} if page_size is None else {"page_size": page_size}


def _verifications_params(page: int, page_size: int | None) -> dict[str, Any]:
    """The query of ``GET /verifications``: ``page_size`` only when given, so a
    call without it sends exactly what it always did."""
    params: dict[str, Any] = {"page": page}
    if page_size is not None:
        params["page_size"] = page_size
    return params


def _first_page(page: int) -> int:
    """``page`` for an iterator, refused unless it is 1 or more."""
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise LenzUsageError(f"iter starts at page 1 or later (got page={page!r}).", code="invalid_page", param="page")
    return page


def _segment(value: Any, message: str, param: str | None = None) -> str:
    """An id as ONE path segment: percent-encoded whole, so a ``/``, ``?``, ``#``
    or ``%`` in it cannot leave the intended path (httpx would otherwise cut the
    URL there). ``""``, ``"."`` and ``".."`` raise ``ValueError(message)``: a
    path normaliser eats the dots, and an empty id names the collection."""
    if not isinstance(value, str) or value in ("", ".", ".."):
        raise LenzUsageError(message, code="invalid_id", param=param)
    try:
        return quote(value, safe="")
    except UnicodeEncodeError:
        # A lone surrogate cannot be sent: the id cannot name anything.
        raise LenzUsageError(message, code="invalid_id", param=param) from None


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


def _unexpected_answer(method: str, path: str, body: Any = None) -> LenzInvalidResponseError:
    """A 200 whose body is not the thing asked for (a proxy page, another
    task's body): ``LenzInvalidResponseError`` with the answer (its status,
    request id, headers and body), never a default-valued result."""
    status = body.http_status if isinstance(body, _Body) else 0
    headers = body.headers if isinstance(body, _Body) else ResponseHeaders()
    parsed = dict(body) if isinstance(body, dict) else None
    text = json.dumps(parsed, ensure_ascii=False) if parsed is not None else ""
    err = LenzInvalidResponseError(
        message=f"{method} {path} returned an unexpected response body.",
        cause="The answer is not the shape the API documents for this call (another job's body, or a proxy's).",
        fix="Retry; if it persists, contact support (https://lenz.io/contact) with the request id.",
        doc_url="https://lenz.io/docs/errors",
        request_id=headers.get("X-Request-ID") or "",
        status_code=status,
        body=parsed,
        body_text=text if len(text) <= INVALID_BODY_TEXT_MAX else text[:INVALID_BODY_TEXT_MAX] + "\u2026",
        headers=headers,
        served_version=(headers.get(_VERSION_HEADER) or "").strip() or None,
    )
    err.msg = "Expecting this call's answer"
    err.doc = text
    err.pos = 0
    err.lineno = 1
    err.colno = 1
    return err


def _wait_budget(value: Any, default: float, where: str) -> float:
    """A wait helper's ``timeout`` (how long to wait): ``None`` (the default
    wait) or a finite number of seconds, else ``LenzUsageError`` before any
    request (``nan`` would poll in a tight loop, ``inf`` forever). A number
    ``<= 0`` keeps meaning "read once", as in 2.x."""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, numbers.Real) or not math.isfinite(value):
        raise LenzUsageError(
            f"{where}: timeout must be a finite number of seconds, or None (got {value!r}).",
            code="invalid_option",
            param="timeout",
        )
    return value  # type: ignore[return-value]  # as given: messages print it as passed


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
    failure = _failure_of(_legacy_view(check))
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
    failure = _failure_of(_legacy_view(review))
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
    """Refuse a successful answer in an API version this SDK does not read.

    A response without the header proceeds (a proxy or an old server may not
    send it). An error answer (400 or above) is never refused here: it raises
    its own error, carrying the version as ``served_version``
    (``_after_response``). Webhook payloads never pass through here.
    """
    if response.status_code >= 400:
        return
    served = (response.headers.get(_VERSION_HEADER) or "").strip()
    if not served or served == API_VERSION:
        return
    try:
        parsed = response.json() if response.content else None
    except ValueError:
        parsed = None
    raise LenzApiVersionError(
        message=f"The API answered {served}; this SDK reads {API_VERSION} only.",
        cause=f"The response carries {_VERSION_HEADER}: {served}.",
        fix=(
            "If this persists, contact support (https://lenz.io/contact) with the request id; "
            "lenz-io 2.x reads both versions."
        ),
        doc_url="https://lenz.io/docs/errors",
        request_id=response.headers.get("X-Request-ID") or "",
        status_code=response.status_code,
        body=parsed if isinstance(parsed, dict) else None,
        served_version=served,
        expected_version=API_VERSION,
        headers=ResponseHeaders(dict(response.headers)),
    )


def _stated_retry_after(response: httpx.Response) -> int | None:
    """Seconds the server says to wait, or None if it didn't say.

    Reads the ``Retry-After`` header first, then the body's
    ``reset_in_seconds`` (429 shapes), then the body's ``retry_after``
    (the 503 shapes carry the wait under that key). A typed 503
    (``upstream_unavailable`` / ``capacity``) reads its body's
    ``retry_after`` / ``retry_after_seconds`` first, as its error does. Returns None (rather
    than 0) on an absent or unparseable value so the caller can tell
    "server stated no wait" apart from "server said wait 0 seconds" and
    fall back to its own backoff.
    """
    raw = response.headers.get("Retry-After")
    if response.status_code == 503 and _body_error_code(response) in UPSTREAM_503_CODES:
        # The API's own 503 states its wait in the body, which wins over the
        # header, as on ``LenzUpstreamUnavailableError.retry_after``.
        body = _json_or_none(response)
        stated = body.get("retry_after") if isinstance(body, dict) else None
        if stated is None or str(stated).strip() == "":
            stated = body.get("retry_after_seconds") if isinstance(body, dict) else None
        if stated is not None and str(stated).strip() != "":
            raw = stated
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
    ``ValueError`` here, before the client exists.

    Only an omitted key (``None``) reads ``LENZ_API_KEY``. An empty or
    whitespace-only ``api_key`` is no key: it never falls back to the
    environment, which on a server holding several tenants' keys would send
    one tenant's call with the process's key. A call that needs a key then
    raises ``LenzAuthError``, as on a client given none.

    Whitespace around the key (a trailing newline read from a file) is
    dropped; what is left must be printable ASCII without spaces (no
    whitespace, control or non-ASCII character inside it): ``LenzAuthError``
    here, before any request (since 3.2)."""
    import os

    # The same rule as every request option: refused here, before the
    # client exists, rather than failing (or retrying forever) later.
    # Left out (``NOT_GIVEN``): the 30 s default.
    checked_timeout = _check_timeout(DEFAULT_TIMEOUT if isinstance(timeout, NotGiven) else timeout, where)
    retries = _check_retries(max_retries, where)
    from_env = api_key is None
    key = ((os.environ.get("LENZ_API_KEY") or "") if api_key is None else api_key).strip(_ASCII_SPACE)
    _check_api_key(key, where, from_env=from_env)
    url = (base_url or os.environ.get("LENZ_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    return key, url, checked_timeout, retries


def _borrowed_user_agent(client_headers: Mapping[str, str], user_agent: str | None, sdk_agent: str) -> str:
    """The User-Agent a request through a borrowed ``http_client=`` carries:
    the caller's ``user_agent=`` when given; else the borrowed client's own
    when it set one; else (httpx's ``python-httpx/...`` default, or none) the
    SDK's. Sent per request, so the borrowed client's headers never change."""
    if user_agent:
        return user_agent
    own = client_headers.get("User-Agent", "")
    if own and not own.startswith("python-httpx/"):
        return own
    return sdk_agent


def _check_user_agent(value: Any, where: str) -> str | None:
    """``user_agent=``: ``None`` / ``""`` (the SDK's), or a header value
    (visible ASCII, spaces and tabs only inside), else ``LenzUsageError``
    before the client exists (httpx would fail to encode it on every call)."""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not _HEADER_VALUE.fullmatch(value):
        raise LenzUsageError(
            f"{where}: user_agent must be a string of visible ASCII characters, with spaces and tabs only "
            f"between them (got {value!r}).",
            code="invalid_header",
            param="user_agent",
        )
    return value


def _check_legacy_aliases(value: Any, where: str) -> bool:
    """``legacy_aliases``: ``True`` or ``False``, else ``ValueError``."""
    if not isinstance(value, bool):
        raise LenzUsageError(
            f"{where}: legacy_aliases must be True or False (got {value!r}).",
            code="invalid_option",
            param="legacy_aliases",
        )
    return value


#: The whitespace dropped around a key: ASCII only (not a BOM, a no-break
#: space or U+0085, which ``str.strip`` would also drop), as in the Node SDK.
_ASCII_SPACE = " \t\n\r\f\v"

#: What an API key or access token can hold: printable ASCII, no spaces.
_API_KEY = re.compile(r"[\x21-\x7e]+")


def _check_api_key(key: str, where: str, *, from_env: bool = False) -> None:
    """Refuse a key (already stripped of surrounding whitespace) that cannot
    be sent as a bearer token (a non-ASCII character, a space, a newline or
    any other control character inside it) with ``LenzInvalidKeyError`` (a
    ``LenzAuthError``), before
    any request: httpx would otherwise fail to encode it, or refuse the
    header, on every call. ``""`` (no key) passes. The key itself is never
    put in the message."""
    if not key or _API_KEY.fullmatch(key):
        return
    source = "The LENZ_API_KEY environment variable" if from_env else "The api_key"
    raise LenzInvalidKeyError(
        message=f"{where}: the API key is not valid.",
        cause=(
            f"{source} contains a character a key never has (a space, a line break, another control "
            "character or a non-ASCII character)."
        ),
        fix=(
            "Pass the key exactly as https://lenz.io/api-credentials shows it: printable ASCII, no "
            "spaces or line breaks inside it."
        ),
        doc_url="https://lenz.io/docs/auth",
    )


def _copy_api_key(value: Any) -> str:
    """The key a ``with_options`` copy is given. Never the environment: an
    empty or whitespace-only key, or ``None``, is no key. Any other key is
    checked like the constructor's (``_check_api_key``)."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise LenzUsageError(
            f"with_options(): api_key must be a string or None (got {type(value).__name__}).",
            code="invalid_option",
            param="api_key",
        )
    key = value.strip(_ASCII_SPACE)
    _check_api_key(key, "with_options()")
    return key


def _results_context(legacy_aliases: bool) -> dict[str, Any] | None:
    """The validation context results are read with: ``None`` (2.x aliases
    filled in) or ``legacy_aliases=False``'s, read as sent."""
    return None if legacy_aliases else {LEGACY_ALIASES: False}


#: A UTF-16 surrogate code point: in a Python ``str`` only ever half of a pair
#: written out as two code points, or a lone one, which UTF-8 cannot encode.
_SURROGATE = re.compile("[\ud800-\udfff]")


def _well_formed(value: Any) -> Any:
    """``value`` with every string (dict keys included, at any depth) made
    encodable: a surrogate pair becomes its one character and each lone
    surrogate U+FFFD, as JavaScript's ``TextEncoder`` (the Node SDK) does, so
    both SDKs send the same bytes and none raises ``UnicodeEncodeError``.
    Anything without a surrogate is returned as is."""
    if isinstance(value, str):
        if _SURROGATE.search(value) is None:
            return value
        return value.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    if isinstance(value, dict):
        return {_well_formed(k): _well_formed(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        # A plain list: any sequence goes out as a JSON array, and a
        # namedtuple's constructor would not take one iterable.
        return [_well_formed(v) for v in value]
    return value


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

#: A blank input or an empty list is refused with the sentence the API's 422
#: (``2026-10-11``) gives for the same request, so the message reads the same
#: whether the SDK or the API refused it (a blank claim is ``claim is
#: required.``, a blank ``assess`` item ``claims[i] is blank.``).
_API_ASSESS_EMPTY_LIST = "claim: Field required"
_API_SELECT_EMPTY = "claims is required."
_API_ASK_BLANK = "Message cannot be empty."
_API_REVIEW_BLANK = "text: send the draft, or one public http(s) URL."
_API_CITECHECK_BLANK = "payload: Value error, send exactly one of text and pairs"


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
    chosen = _one_input(claim, text)
    if not chosen or _blank(chosen):
        raise LenzUsageError("claim is required.", code="blank_input", param="claim")
    payload: dict[str, Any] = {"text": chosen}
    # Omit-when-empty (since 3.2; earlier releases sent ``"source_url": ""``):
    # an empty value means no source, which is what leaving it out says.
    if source_url:
        payload["source_url"] = source_url
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


def _blank(value: Any) -> bool:
    """An empty or whitespace-only string: only characters ``str.strip``
    removes, the API's own rule."""
    return isinstance(value, str) and not value.strip()


def _one_input(claim: Any, text: Any) -> Any:
    """The input of ``claim=`` / ``text=`` (aliases): the one with content,
    ``claim`` first, as the API resolves the two; else ``claim or text``."""
    if claim and not _blank(claim):
        return claim
    if text and not _blank(text):
        return text
    return claim or text


def _check_assess_forms(claim: str, text: str, claims: list[str] | None) -> None:
    """The two forms of ``assess``, checked before any request (since 3.2 a
    blank claim, an empty list and a blank item raise here, as on ``select``,
    instead of a 422 from the server)."""
    if claims is not None and (claim or text):
        raise LenzUsageError(
            "assess takes either one claim (claim=) or a list (claims=), not both",
            code="conflicting_input",
            param="claims",
        )
    if claims is not None:
        if not claims:
            # The API reads ``"claims": []`` as no input at all.
            raise LenzUsageError(_API_ASSESS_EMPTY_LIST, code="empty_list", param="claims")
        for index, item in enumerate(claims):
            if not isinstance(item, str):
                raise LenzUsageError(
                    f"claims[{index}] must be a string (got {type(item).__name__}).",
                    code="invalid_argument",
                    param=f"claims[{index}]",
                )
            if _blank(item):
                raise LenzUsageError(f"claims[{index}] is blank.", code="blank_item", param=f"claims[{index}]")
        return
    chosen = _one_input(claim, text)
    if not chosen or _blank(chosen):
        raise LenzUsageError("claim is required.", code="blank_input", param="claim")


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
        raise LenzUsageError(_API_SELECT_EMPTY, code="empty_list", param="claims")
    for index, item in enumerate(chosen):
        # Since 3.2 a blank item is refused, as ``assess`` refuses one (the
        # API drops it silently, and answers ``claims is required.`` only
        # when every item is blank).
        if _blank(item):
            raise LenzUsageError(f"claims[{index}] is blank.", code="blank_item", param=f"claims[{index}]")
    return chosen


def _ask_payload(message: str, language: str) -> dict[str, Any]:
    # ``None`` too: the API reads a ``null`` message as an empty one.
    if message is None or _blank(message):
        raise LenzUsageError(_API_ASK_BLANK, code="blank_input", param="message")
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
        raise LenzUsageError(
            'iter cannot walk sort="random" (each page is a fresh sample); call library.list instead.',
            code="invalid_argument",
            param="sort",
        )


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
        raise LenzUsageError(_API_REVIEW_BLANK, code="blank_input", param="text")
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
            raise LenzUsageError(
                f"{name} is a list of strings, e.g. {name}=[{selector!r}].", code="invalid_argument", param=name
            )
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
    if not has_text and pairs is None:
        raise LenzUsageError(_API_CITECHECK_BLANK, code="blank_input", param="text")
    if has_text and pairs is not None:
        raise LenzUsageError("citecheck() needs exactly one of text and pairs.", code="conflicting_input", param="text")
    if pairs is not None and max_citations is not None:
        raise LenzUsageError(
            "max_citations goes with text: every pair is checked.", code="conflicting_input", param="max_citations"
        )
    payload: dict[str, Any] = {"text": text} if has_text else {"pairs": [dict(p) for p in pairs or []]}
    if max_citations is not None:
        payload["max_citations"] = max_citations
    if language:
        payload["language"] = language
    if webhook_url is not None:
        payload["webhook_url"] = webhook_url
    return payload


def _no_key(idempotency: bool) -> dict[str, Any]:
    """``idempotency=False`` as a keyword for ``review`` / ``citecheck`` from
    their wait helpers, only when given: an override with the 3.1 signature
    is still called the way 3.1 called it."""
    return {} if idempotency else {"idempotency": False}


def _job_key(idempotency_key: str | None, idempotency: bool = True) -> str | None:
    """A review's or citation check's ``Idempotency-Key``: the caller's, else
    a new random one, else (``idempotency=False``, since 3.2) none. An empty
    ``idempotency_key=""`` sends none, as on every other call (3.1 generated
    one here)."""
    if idempotency_key is not None:
        return idempotency_key or None
    return uuid.uuid4().hex if idempotency else None


def _review_params(view: str) -> dict[str, Any] | None:
    """The query of ``get_review``: ``view=issues``, nothing for the full
    view; ``ValueError`` for any other view."""
    if view == "issues":
        return {"view": "issues"}
    if view != "full":
        raise LenzUsageError(f"view must be 'full' or 'issues' (got {view!r}).", code="invalid_argument", param="view")
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
        started = ReviewStarted(review_id=review_id, status="queued")
        _settled_by(started, exc, conflict)
        return started
    return None


def _citecheck_started_by_conflict(exc: LenzError) -> CitecheckStarted | None:
    """A retried submit (same key) that lands while the first attempt's
    check is still being created answers 409 naming that check: it exists,
    so this call started it."""
    conflict = exc.body if isinstance(exc.body, dict) else {}
    existing = conflict.get("citecheck_id")
    if exc.status_code == 409 and exc.code == "idempotency_conflict" and isinstance(existing, str) and existing:
        check = CitecheckStarted(citecheck_id=existing, status="queued")
        _settled_by(check, exc, conflict)
        return check
    return None


def _settled_by(started: ReviewStarted | CitecheckStarted, exc: LenzError, conflict: dict[str, Any]) -> None:
    """``started`` as the 409 that settled the call: its ``raw`` is that
    answer's body, ``http_status`` 409, ``headers`` its headers, and
    ``settled_by_conflict`` ``True``."""
    _set_raw(started, conflict, _Meta(exc.status_code, ResponseHeaders(exc.headers)))
    started._settled_by_conflict = True


def _already_deleted(exc: LenzError) -> bool | None:
    """Idempotent DELETE: if the row was already gone (e.g. previous request
    succeeded but the network reply was lost), the call succeeded rather than
    surfacing a confusing 404. A 404 in another API version is not read as
    this one's. ``None``: the error stands."""
    if exc.status_code == 404 and exc.served_version in (None, API_VERSION):
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
    send_client_timeout: bool = False,
) -> tuple[httpx.Timeout | None, int, tuple[tuple[str, str], ...]]:
    """The timeout, retry count and option headers of one request
    (``_resolve``). ``timeout`` and ``max_retries`` are the SDK's own settings
    for one request (a wait's polls): when set they win over every option."""
    resolved, retries, option_headers = _resolve(
        options, layer, client_timeout, client_retries, floor, send_client_timeout
    )
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
    user_agent: str | None = None,
    has_body: bool = True,
) -> tuple[str, dict[str, str], Any]:
    """The URL, headers and timeout one request is sent with (every attempt
    of it). Raises ``LenzAuthError`` when the call needs a key and there is
    none (``LenzMissingKeyError``). ``user_agent``: a User-Agent to send on this request (a client
    given ``http_client=``), under the request options' headers."""
    if auth_required and not api_key:
        raise LenzMissingKeyError(
            message="API key required",
            cause="This method requires authentication; no API key was provided.",
            fix=(
                "Pass api_key= to Lenz(), set LENZ_API_KEY env var, or get one at "
                "https://lenz.io/api-credentials. Library endpoints work without a key."
            ),
            doc_url="https://lenz.io/docs/auth",
        )

    url = f"{base_url}{path}"
    # First, where httpx puts a client's default User-Agent.
    req_headers = {"User-Agent": user_agent} if user_agent else {}
    req_headers.update(headers or {})
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
    # Only on a request that has a body (since 3.2: a GET, a body-less POST
    # or DELETE sends none).
    if has_body:
        req_headers.setdefault("Content-Type", "application/json")
    # The version is part of the request, not of the HTTP client: a client
    # passed as ``http_client=`` may carry no version header or a stale one.
    req_headers[_VERSION_HEADER] = API_VERSION
    # ``None``: the ``httpx`` client's own timeout.
    req_timeout = httpx.USE_CLIENT_DEFAULT if timeout is None else timeout
    return url, req_headers, req_timeout


#: httpx errors a request can raise that no retry can change: the request
#: could not be built or sent (a bad URL or scheme, a header httpx refuses to
#: write), or its answer could not be decoded or followed.
NOT_SENDABLE = (
    httpx.UnsupportedProtocol,
    httpx.LocalProtocolError,
    httpx.InvalidURL,
    httpx.DecodingError,
    httpx.TooManyRedirects,
    httpx.StreamError,
)


def _not_sendable(exc: Exception, method: str, path: str) -> LenzConnectionError:
    """A ``LenzConnectionError`` (``__cause__`` the httpx error) for an httpx
    error no retry can change: ``retryable`` ``False``."""
    err = LenzConnectionError(
        message=f"{method} {path} could not be completed: {exc}",
        cause=str(exc) or type(exc).__name__,
        fix="Check base_url (an http or https URL) and any header or transport you configured.",
        doc_url="https://lenz.io/docs/errors",
    )
    err.retryable = False
    err.__cause__ = exc
    return err


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


def _invalid_answer(response: httpx.Response, message: str, cause: str, msg: str) -> LenzInvalidResponseError:
    """``LenzInvalidResponseError`` for an answer that is not the API's JSON
    object (its decode fields describe ``msg`` at the start of the body)."""
    text = response.text
    err = LenzInvalidResponseError(
        message=message,
        cause=cause,
        fix="Check base_url and any proxy between you and the API; quote the request id to support.",
        doc_url="https://lenz.io/docs/errors",
        request_id=response.headers.get("X-Request-ID") or "",
        status_code=response.status_code,
        body=None,
        body_text=text if len(text) <= INVALID_BODY_TEXT_MAX else text[:INVALID_BODY_TEXT_MAX] + "\u2026",
        headers=ResponseHeaders(dict(response.headers)),
        served_version=(response.headers.get(_VERSION_HEADER) or "").strip() or None,
    )
    err.msg = msg
    err.doc = text
    err.pos = 0
    err.lineno = 1
    err.colno = 1
    return err


def _success_body(response: httpx.Response, method: str, path: str) -> Any:
    """A success's body: its JSON object. A redirect (the API never
    redirects, and httpx does not follow one), an empty body (since 3.2 a
    204, a 205 or ``Content-Length: 0`` too: no endpoint answers with none),
    a body that is not JSON, or JSON that is not an object raises
    ``LenzInvalidResponseError`` with the real status (a proxy answering in
    the API's place, typically), never a bare decode error."""
    if 300 <= response.status_code < 400:
        raise _invalid_answer(
            response,
            f"{method} {path} answered HTTP {response.status_code}, a redirect"
            + (f" to {response.headers['Location']}." if response.headers.get("Location") else "."),
            "The Lenz API never redirects: something between you and it (a proxy, a load balancer, a "
            "wrong base_url) answered in its place.",
            "Expecting value",
        )
    if not response.content.strip():
        raise _invalid_answer(
            response,
            f"{method} {path} answered HTTP {response.status_code} with an empty body.",
            "Every Lenz API answer has a JSON body: something else answered in its place.",
            "Expecting value",
        )
    try:
        parsed = response.json()
    except ValueError as exc:
        text = response.text
        err = LenzInvalidResponseError(
            message=f"{method} {path} answered HTTP {response.status_code} with a body that is not JSON.",
            cause="Something other than the Lenz API answered (a proxy, a captive portal, a load balancer).",
            fix="Check base_url and any proxy between you and the API; quote the request id to support.",
            doc_url="https://lenz.io/docs/errors",
            request_id=response.headers.get("X-Request-ID") or "",
            status_code=response.status_code,
            body=None,
            body_text=text if len(text) <= INVALID_BODY_TEXT_MAX else text[:INVALID_BODY_TEXT_MAX] + "\u2026",
            headers=ResponseHeaders(dict(response.headers)),
            served_version=(response.headers.get(_VERSION_HEADER) or "").strip() or None,
        )
        decode = exc if isinstance(exc, json.JSONDecodeError) else None
        err.msg = decode.msg if decode else str(exc)
        err.doc = decode.doc if decode else text
        err.pos = decode.pos if decode else 0
        err.lineno = decode.lineno if decode else 1
        err.colno = decode.colno if decode else 1
        raise err from exc
    if not isinstance(parsed, dict):
        # Valid JSON, but not the object every endpoint answers with
        # (``null``, a list, a number, a string): unreadable as a result.
        raise _invalid_answer(
            response,
            f"{method} {path} answered HTTP {response.status_code} with JSON that is not an object "
            f"({type(parsed).__name__}).",
            "The answer is not the JSON object the API documents for this call.",
            "Expecting a JSON object",
        )
    return _Body(
        parsed,
        http_status=response.status_code,
        headers=ResponseHeaders(dict(response.headers)),
        unreadable=lambda exc: _unreadable_fields(response, method, path, parsed, exc),
    )


def _unreadable_fields(
    response: httpx.Response, method: str, path: str, parsed: dict[str, Any], exc: ValidationError
) -> LenzInvalidResponseError:
    """``LenzInvalidResponseError`` for a JSON object whose fields this
    release cannot read (``{"claims": "x"}``, ``{"claims": [42]}``): what the
    null-tolerance rules leave unreadable. ``body`` is the object as parsed;
    the pydantic error is the ``__cause__``."""
    return unreadable_fields_error(
        exc,
        body=parsed,
        status_code=response.status_code,
        headers=ResponseHeaders(dict(response.headers)),
        what=f"{method} {path} answered HTTP {response.status_code}",
        text=response.text,
    )


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
    legacy_aliases: bool = True,
) -> _Done | _Again:
    """What to do with one answer: ``(True, body)`` when the call is done,
    ``(False, seconds)`` to send the same request again after sleeping that
    long; an error to raise is raised. With ``legacy_aliases`` off, an
    error's ``code`` is the body's own."""
    if 300 <= response.status_code < 400:
        _success_body(response, method, path)  # raises: not an API answer
    _check_served_version(response)

    if response.status_code < 400:
        return True, _success_body(response, method, path)

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

    err = map_response_to_error(
        response.status_code,
        response.content,
        dict(response.headers),
        endpoint=(method, path),
    )
    if not legacy_aliases:
        # The code exactly as sent: no 2.x rewrite, none left out.
        err.code = _body_error_code(response)
    raise err


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
            # The run ended, but there is nothing to read (since 3.2; 3.1
            # raised ``LenzPipelineError``).
            raise _no_result(status)
        return status.result
    # The error is built from the 2.x reading, whatever ``legacy_aliases``.
    status = _legacy_view(status)
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
    _segment(task_id, "wait() requires a non-empty task_id (got an empty TaskAccepted.task_id).", "task_id")
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
    or the timeout is raised (told when the last polls could not be read:
    ``timed_out`` is then a ``TimedOut`` carrying the last such answer)."""
    if task_id in stopped:
        raise stopped[task_id]
    if task_id in timed_out:
        unreadable: Mapping[str, LenzInvalidResponseError] = getattr(timed_out, "unreadable", {})
        raise _note_unreadable(_wait_timed_out(timeout, task_id), unreadable.get(task_id))
    return terminal[task_id]


class TimedOut(set[str]):
    """The ids a verification poll timed out on, with ``unreadable``: per id,
    the last poll answer that could not be read, when no readable one came
    after it."""

    def __init__(self, ids: set[str], unreadable: Mapping[str, LenzInvalidResponseError]) -> None:
        super().__init__(ids)
        self.unreadable = dict(unreadable)


def _note_unreadable(err: _E, unreadable: LenzInvalidResponseError | None) -> _E:
    """``err`` (a wait's timeout) told that the last polls could not be read:
    ``unreadable`` (the last such answer) becomes its ``__cause__``, and the
    message and cause say so instead of "still running"."""
    if unreadable is None:
        return err
    err.message = f"{err.message}; the last poll's answer could not be read"
    err.cause = (
        "The last poll's answer could not be read (see __cause__), so whether the run is still going is unknown."
    )
    err.args = (err.message,)
    err.__cause__ = unreadable
    return err


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
            # account's retention period (410), an unknown task (404), an
            # answer in another API version, or an unreadable answer whose
            # status says the run ended. Terminal, with no status; the
            # error is the item's ``error``.
            item = BatchItemResult(task_id=it.task_id, claim_text=it.claim_text or it.claim, status="failed")
            item._error = stopped[it.task_id]
            results.append(item)
        elif not it.task_id or it.task_id in timed_out or status is None:
            results.append(BatchItemResult(task_id=it.task_id, claim_text=it.claim_text or it.claim, status="timeout"))
        elif status.status == "completed" and status.result is not None:
            results.append(
                BatchItemResult(
                    task_id=it.task_id,
                    claim_text=it.claim_text or it.claim,
                    status="completed",
                    verification=status.result,
                    status_detail=status,
                )
            )
        elif status.status == "needs_input":
            results.append(
                BatchItemResult(
                    task_id=it.task_id, claim_text=it.claim_text or it.claim, status="needs_input", status_detail=status
                )
            )
        else:
            # failed, or completed-without-result (treated as failed: the run
            # ended, but its result cannot be read, which ``error`` says).
            item = BatchItemResult(
                task_id=it.task_id, claim_text=it.claim_text or it.claim, status="failed", status_detail=status
            )
            if status.status == "completed":
                item._error = _no_result(status)
            results.append(item)
    return results


def _review_job(
    review_id: str, timeout: float, context: dict[str, Any] | None = None
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
        f"/reviews/{_segment(review_id, '_wait_review() needs a review_id.', 'review_id')}",
        lambda body: (
            ReviewFull.model_validate(body, context=context) if _is_full_review_body(body, review_id) else None
        ),
        timed_out,
    )


def _citecheck_job(
    citecheck_id: str, timeout: float, context: dict[str, Any] | None = None
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
        f"/citechecks/{_segment(citecheck_id, '_wait_citecheck() needs a citecheck_id.', 'citecheck_id')}",
        lambda body: (
            Citecheck.model_validate(body, context=context) if _is_citecheck_body(body, citecheck_id) else None
        ),
        timed_out,
    )
