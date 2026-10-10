# Changelog

All notable changes to this SDK are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[SemVer](https://semver.org/).

## [Unreleased]

### Added

- `review` / `review_and_wait` accept `language="auto"`: the review comes back in the language of the draft (one language for the whole review). Needs the API release that accepts it on `/review`; before that the API answers 422.
- `extract` accepts `language="auto"` (on `Lenz` and `AsyncLenz`): the claims are written in the language of the text, or of the fetched page when `text` is a single URL. A short or undetectable text, or a detector failure, gives English; leaving `language` out is still English and a concrete code always wins. `verify_batch`, `citecheck` and `review` still take the codes only.
- `ExtractedClaims.language`: the ISO 639-1 code the claims are written in. Pass it on to `assess` or `verify` as `language` to keep a chain in one language. It is `None` on a replayed response stored before the API sent it. `lenz extract` takes `--language` (a code or `auto`) and prints the language it got back.
- `page_size=` on `verifications.list()` and `verifications.iter()` (both clients): items per page, a whole number from 1 to 100; anything else raises `ValueError` before any request (the API would clamp it silently). Omitted, nothing changes: the request is sent exactly as before and the server's default (20) applies. `iter()` sends it on every page. `library.list()` / `iter()` do not take it: the public catalog serves a fixed page size.
- `with_options(api_key=...)` (both clients): a copy with its own key, on the same connection pool, for a server acting for several users (each with a Lenz API key or an OAuth access token, `lat_...`; any string is sent as given). An empty or whitespace-only key, or `None`, gives a copy with no key (calls that need one raise `LenzAuthError` before sending). A copy never reads `LENZ_API_KEY`, and leaving `api_key` out keeps the key the copy was made from.
- `legacy_aliases=False` on `Lenz()` / `AsyncLenz()`: results carry exactly what the API sent in the `2026-10-11` shape, with no deprecated 2.x field filled in (a failed `assess` row keeps `verdict` / `confidence` `None`, picker options carry `claim` and no `text`, the `/me/usage` per-capability blocks stay `None` unless sent, `extract`'s `status` reads `no_checkable_claim`, and so on; the README has the full table). The default, `True`, changes nothing. Copies made with `with_options` read with their client's setting, which they cannot change. Errors and webhook parsing are the same either way.
- `LenzInvalidResponseError` (a `LenzAPIError`): see Changed.

### Changed

- **`api_key=""` (or a whitespace-only key) is no key.** It used to fall back to the `LENZ_API_KEY` environment variable, so a server choosing a key per tenant could send one tenant's call with the process's key when that tenant's key was empty. Now only an omitted key (`api_key=None`) reads the environment; an empty one behaves like a client given no key: the public library works, and a call that needs a key raises `LenzAuthError`. If you relied on `api_key=""` reading `LENZ_API_KEY`, pass `None` (or nothing).
- **A 2xx whose body is not JSON raises `LenzInvalidResponseError`** (a `LenzAPIError`) carrying the real `status_code`, the `request_id`, `body_text` (the first 1,000 characters, plus `…` when longer) and `retryable` `None`. 3.1 let a bare `json.JSONDecodeError` escape, with no status. The new class is also a `json.JSONDecodeError`, so `except json.JSONDecodeError` keeps catching it. A 204, a 205 or a 2xx with `Content-Length: 0` still reads as `{}`; any other empty or whitespace-only 2xx body raises it (3.1 read an empty body as `{}` whatever its headers, the rule the Node SDK has always followed).
- `LenzApiVersionError`'s message now reads "The API answered 2026-05-13; this SDK reads 2026-10-11 only." The class, its attributes and when it is raised are unchanged.

### Fixed

- A lone UTF-16 surrogate (half of a pair, `"\ud800"`) in any request string, dict keys and query values included, now goes out as U+FFFD instead of raising `UnicodeEncodeError` before the request. A pair written as two surrogates goes out as its one character. Both SDKs now send the same bytes. Valid text is sent unchanged.

- `user_agent=` now reaches the wire when the client is given `http_client=` (it was ignored there), and such a client sends the SDK's `lenz-io-python/...` User-Agent instead of httpx's default `python-httpx/...`. It is set on each request, so the client you passed is not changed; a User-Agent you set on that client yourself is kept unless you pass `user_agent=`, and a per-call `extra_headers={"User-Agent": ...}` still wins over both.

## [3.1.0] - 2026-10-10

### Added

- **`AsyncLenz`**, the client for asyncio: every method of `Lenz` with the same parameters, defaults, results and errors, as coroutines (`iter()` returns an async iterator), per-call options and `with_options` included. `async with AsyncLenz() as client: await client.assess(...)`. Both clients share one core, so they send the same requests, retry and wait the same way. What differs: `aclose()` / `async with`, an `httpx.AsyncClient` as `http_client=`, callbacks that may be `async def`, and `cancel_on_abort=True` on the waits (`wait`, `verify_and_wait`, `verify_batch_and_wait`, `review_and_wait`, `citecheck_and_wait`): when the awaiting task is cancelled, the server is also asked to stop the job (best effort, at most 5 s; call `aclose()` before the event loop ends so it goes out), and then the `CancelledError` is re-raised. Without it a cancelled await only stops waiting and the job runs on, charged as usual. The User-Agent ends `; async)`. Nothing changes for `Lenz`. See "Using Lenz from async code" in the README.
- `snippet_language` on a verification's sources: the language of the quote as an ISO 639-1 code (e.g. `uk`) when it is not English (needs the API change that adds it; older responses read as None/null).

### Internal

- The request helpers moved from `lenz_io.client` to the private `lenz_io._core` (shared by both clients). They are still importable from `lenz_io.client`, but patching a private name there (for example `lenz_io.client._retry_sleep`) no longer changes what the client does. Patching `lenz_io.client.time` (its sleep and clock) still works.

## [3.0.0] - 2026-10-10

### What's new in 3.0

- **Stop a run**: `cancel(task_id)`, `cancel_review(review_id)` and
  `cancel_citecheck(citecheck_id)`. A stopped verification is not charged;
  a stopped review or citation check refunds what it had not delivered.
- **`cancelled` is a status of its own**, with a `verification.cancelled`
  webhook event (`VerificationCancelled`); a polling loop must treat it as final.
- **Per-call options**: `timeout`, `max_retries` and `extra_headers` on every
  method, and `client.with_options(...)` for a copy with other defaults.
- **Errors say whether a retry can help** (`retryable`) and carry the
  `idempotency_key` the call sent.
- **The SDK reads the API's `2026-10-11` response shape.** Every 2.x name
  keeps working as a deprecated alias.
- **`dissent` on assess rows is deprecated** and always `None`.
- **Before you upgrade**, see the box below: move webhook receivers to
  2.21+ first, and re-record tests that replay 2.x response bodies.

> **Upgrading from 2.x.** Must do: (1) upgrade every service that *receives*
> your webhooks to lenz-io 2.21+ before the sending service moves to 3.0 (a
> 2.21 receiver sees the `*.cancelled` events of 3.0-submitted work as a plain
> `WebhookEvent`: branch on `event.event` there);
> (2) code reading raw bodies (`exc.body`, `event.raw`) reads the current
> shape; (3) re-record tests that replay recorded 2.x response bodies; (4) an
> idempotent request first sent before the switch and replayed after it raises
> `LenzApiVersionError`: finish that work with 2.x, never change the key to
> get past it; (5) a hand-written polling loop must treat `cancelled` as
> final: `get_status`, `get_review` and `get_citecheck` return it where 2.21
> returned `failed`, so a loop waiting for `failed` polls until its own
> timeout. May do: move off the deprecated names (they keep working).
> Details under Migration.

Major release (3.0.0). The SDK asks for the API's current response shape
(`2026-10-11`) and reads only that shape from its own calls. Every attribute,
exception and CLI rendering keeps the value it had in 2.x (computed from the
current shape), with the exceptions listed under Breaking and "What reads
differently". The raw bodies are the current shape: `exc.body` and a webhook
event's `raw` hold the body as sent; `model_dump()` and the CLI's `--json`
hold the 2.x-compatible fields plus the current-shape keys the server sent
(see Migration). Every 2.x name is kept as a deprecated alias (see
Deprecated).

### Breaking

- **3.0 reads only the API's `2026-10-11` response shape for its own calls**
  (lenz.io serves it from 2026-10-11). The SDK no longer detects or fills in
  a 2.x-shaped response body, a 2.x-shaped error body, or a `/me/usage` body
  from before the credit pool.
- **`LenzApiVersionError` (new, a `LenzError`) is raised when a response
  names another version** in its `X-Lenz-API-Version` header, in practice
  `2026-05-13`: a server still on the older version, or a reply replayed from
  an idempotent request stored before the change. It carries `api_version`,
  `status_code` and `body` (as sent), applies to success and error responses
  of client calls, and never to webhook payloads. A response without the
  header is read as usual. If it persists, contact support with the request
  id; lenz-io 2.x reads both versions.
- **Values the API no longer sends**, which no client can rebuild:
  - `TaskAccepted.chain_id` reads `""` (use `task_id`).
  - The `task_id` of a `review.*` or `citecheck.*` webhook event is the
    review or citation-check id (use `event_id` to deduplicate, and the
    event's `review_id` / `citecheck_id`); these ids were never pollable.
  - Some failure sentences and hints are worded anew (listed under "What
    reads differently"); compare on `failure.code` / `code`, never on the
    text.
  - An `/extract` on an input the API first read as not a claim and then
    found a claim in says `status == "ready"` (2.x: `"not_a_claim"`); branch
    on `claims`.
- **A task cancelled elsewhere (the website's Stop button, another process)
  is its own status, `cancelled`**, where 2.x read `failed` with
  `failure_class` `cancelled`.
  - `TaskStatus.status`, `ReviewFull.status` and `Citecheck.status` can read
    `"cancelled"`. A hand-written polling loop must treat `cancelled` as
    terminal or it polls until its own timeout. A cancelled `TaskStatus`
    (from `get_status`, a batch item's `status_detail`, or a
    `verification.cancelled` event's `verification`) reads the failure block
    2.x read for it (`failure.code` and `failure_class` `"cancelled"`,
    `detail` `"Cancelled."`, `retryable` `False`) and the 2.x fields (`error`
    `"Cancelled."`, `failure_class`, `failure_reason`, `retryable`,
    `docs_url`). A cancelled review or citation check has `failure` `None`, as
    the API sends it.
  - The wait helpers (`wait`, `verify_and_wait`, `verify_batch_and_wait`,
    `review_and_wait`, `citecheck_and_wait`) end on it at once instead of
    polling to their timeout. They raise the same error class and failure
    fields (`failure_class`, `failure_reason`, `retryable`, doc URL) as 2.x did
    for the original shape: `LenzPipelineError`, `ReviewFailed`,
    `CitecheckFailed`. For a task cancelled while running the message reads
    "Cancelled." (2.x said "Pipeline stopped at: cancelled"). A batch item is
    a `failed` row; `get_status`, `get_review` and `get_citecheck` return the
    cancelled status without raising.
  - Webhooks for work submitted with this release arrive as
    `verification.cancelled` (`VerificationCancelled`), `review.cancelled` and
    `citecheck.cancelled`, not `*.failed`. A receiver that branches only on
    `*.failed` misses them. A 2.21 receiver reads them as a plain
    `WebhookEvent`: branch on `event.event` there.
- **Webhooks of both shapes are still parsed** (`parse_webhook`,
  `LenzWebhooks.parse` and the event models): work submitted by an older
  client on the same account is delivered in the 2.x shape.

### Changed

- **`dissent` on `/assess` rows and review assessments is deprecated and
  always null; the CLI no longer prints it.** The field stays on
  `AssessClaim` and `ReviewAssessment` (optional, `None`) so code that reads
  it keeps working.
- **`verify_signature` refuses an empty secret** with `ValueError`, as
  `LenzWebhooks(secret="")` always did. 2.x accepted a body signed with the
  empty key.
- **A whitespace-only `webhook_url` is no longer sent** on `verify`,
  `verify_batch` (the batch-wide value and each item's) and the `*_and_wait`
  helpers that submit them; 2.x sent it as given. It means the key's default
  webhook either way, but the request body (and so its idempotency hash)
  differs: a request first sent by 2.x with a pinned `Idempotency-Key` and a
  whitespace-only `webhook_url`, and resent by 3.0 with the same key, is
  refused with a 422 (`idempotency_body_mismatch`). Finish such a request
  with 2.x, or resend it with a new key.
- **`TaskAccepted.model_dump()` has no `chain_id` key**: 2.x carried it only
  because the API sent it, and the API no longer does. The
  `TaskAccepted.chain_id` attribute is kept and reads `""` (deprecated).
- **A `Retry-After` the SDK cannot use is read safely.** A non-finite value
  (`inf`, `-inf`, `nan`, `1e999`) reads as no stated wait, so the normal
  backoff runs, as in the Node SDK, where it raised `OverflowError`. A huge
  finite wait is clamped to 2,147,483 seconds, still past every cap. The
  error's `retry_after` follows the same rule: a non-finite or unparseable
  value is not a stated wait (`None`; on a `LenzRateLimitError` the next
  stated wait, else `0`, as before), and a larger one reads 2,147,483.
- **A cancel's error body is read as sent**: its `code` (a 422's
  `validation_error` too), `detail` and `errors`.
- **One rule for every timeout and retry count of a request, checked before
  anything is sent.** A timeout must be `None` (no timeout), a finite real
  number of seconds greater than 0, httpx's `(connect, read, write, pool)`
  tuple of such numbers or `None`, or an `httpx.Timeout`; a retry count a
  whole number, 0 or more. A timeout is at most 2,147,483 seconds (about 24.8
  days, the Node SDK's limit): a longer one overflowed in the socket layer on
  every request. Anything else raises `ValueError`: in
  `Lenz(timeout=..., max_retries=...)` when the client is built, and in the
  `timeout=` of `extract` / `assess` before the call mints a key or sends a
  request. Newly refused: a timeout of 0 or less, NaN, infinity or past the
  limit, a negative
  retry count (none of these worked: such a timeout failed every request, a
  negative retry count sent none), and booleans: `True` / `False` as a
  timeout or a retry count, which 2.21 read as 1 / 0 (`Lenz(timeout=True)`
  was a 1-second timeout; pass the number). Real numbers of any type (`Fraction`, numpy
  scalars), the tuple form and integer-like retry counts keep working. The
  wait helpers' `timeout` (how long to wait) is not affected: `0` or less
  still reads once.
- **The private `_request` / `_send` methods take other arguments.** Code that
  overrode them to add headers or change timeouts should use the
  `extra_headers` / `timeout` / `max_retries` options or `with_options`
  instead.
- **An id goes into the URL path as one segment.** Every method that puts an
  id in a path (`get_status`, `select`, `get_review`, `get_citecheck`, the
  cancels, `verifications.*`, `ask.*`, the waits) percent-encodes it whole, so
  an id holding `/`, `?`, `#` or `..` can no longer send the request to another
  path. An ordinary id is on the wire byte for byte as before. An empty id, `"."`
  and `".."` raise `ValueError` before any request (the methods that took an
  empty id now raise as `get_review` and `wait` already did).
- **The SDK asks for the API's current response shape, and reads only that.** Every request sends
  `X-Lenz-API-Version: 2026-10-11` (`lenz_io.API_VERSION`). 2.x sent
  `2026-05-13` from the client it built, and no version header at all through
  an `http_client=` of your own (the server then answered in the account's
  version); 3.0 sends it on every request either way. In that shape each field, status and error code has one name
  across every endpoint.
- **Every attribute keeps the value it had in 2.x** (except the `status` of a
  cancelled task, see Breaking), computed from the
  current shape where the server now sends it under another name or not at
  all. Among them:
  - A failed `/assess` row still reads `verdict == "Error"` and
    `confidence == "low"`, with `error_code` and `hint`; a verdict row that
    found other claims carries its `hint` again. `AssessResponse.error` reads
    `"No verifiable claim detected"` on an input with no claim.
  - "Nothing checkable" keeps each field's old spelling: `no_claim` on
    `/assess` rows, `AssessResponse.error_code`, review rows and the review's
    own failure; `not_a_claim` on `/extract`, a verification
    (`TaskStatus.failure_reason` and the `failure_reason` of its `failure`
    block, `LenzPipelineError.failure_reason`, the `verification.failed`
    webhook's `error` and the `failure_reason` of its `failure` block) and a
    deep check inside a review. `failure.code` is `no_checkable_claim`
    everywhere.
  - `modified_at` (set only when a verification completed on a later UTC
    calendar day than it was created), `claim_text` on receipts, `text` on
    `needs_input` options, `ExtractedClaims.claim` / `identified_claims` /
    `locations`, `claim_limit_reached` / `citation_limit_reached`.
  - `Usage.quota_resets_at`, `credits.bonus` and the `verify` / `ask` /
    `assess` blocks, which the current `/me/usage` leaves out: projected from
    `credits` and `costs` the way the API projected them.
  - A `verification.completed` webhook's `result` has every key it had, with
    the same defaults, plus `completed_at`.
  - A failed `get_status` / `wait`: `error` reads its 2.x sentence, rebuilt
    from the failure code (the fixed sentence for `cancelled`, `task_stuck`,
    `task_error` and `not_a_claim`, else "Pipeline stopped at: <code>"), so
    the `LenzPipelineError` message reads as before; `failure_reason`,
    `failure_class`, `retryable`, `docs_url` and `hint` too.
  - Errors: every class and attribute as 2.x set it. `code` is `""` where the
    2.x error carried none (the API now sends one on every error:
    `not_authenticated`, `not_found`, `validation_error`, `malformed_body`,
    `invalid_request`, `internal_error`, `verification_not_ready` from
    `ask.send`, ...), per endpoint; a 422 keeps
    its 2.x `code` (`blank_item` for a blank `/assess` item,
    `validation_error` on `/review`), `message` (the list of field errors
    for a request-schema failure; the parameter path on `/review` and
    `/citecheck`; `claims[<n>].` before a `verify_batch` item's language
    error) and `errors`; `reset_in_seconds` and `retry_after` on a
    429; `LenzQuotaExceededError.credit_balance` on a citation-check 402.
- `map_response_to_error` takes an optional `endpoint=(method, path)`, which
  the client passes: an error's original `code` and wording depend on it.
- **Batch submit and `ask.send` send an automatic `Idempotency-Key`.**
  `verify_batch`, `verify_batch_and_wait` and `ask.send` generate one random
  key per call, reused across that call's own retries (as `verify`, `assess`
  and `extract` already did), so a retried batch or question replays the
  first answer instead of being charged twice. A key you pass wins;
  `idempotency=False` sends none. Asking the same question again is a new
  call with a new key, so it is asked again. 2.x sent a key there only when
  you passed one.
- **A 409 `idempotency_conflict` is sent again with the same key.** When a
  call that sent an `Idempotency-Key` (its own or yours) meets the first
  request with that key still running, it sends the same key and body again
  after the wait the server states (at most 60s; a longer one falls back to
  the usual backoff), within the call's retries. Still conflicting, it raises
  the error 2.x raised (same class, `code` and message) with
  `retryable=True`. It never mints a second key to get past it. A `review` /
  `citecheck` conflict that names its job still returns that job at once.
  2.x raised the first 409.
- **`wait`, `verify_and_wait` and `verify_batch_and_wait` stop at once on a
  401, 403 or 404** (raising `LenzAuthError` / `LenzNotFoundError`; in a
  batch, that item is `failed` with no `status_detail` and the others keep
  going) instead of polling to a misleading `LenzTimeoutError`. A 5xx, a 429
  or a network failure is still polled again, after the wait it stated. In
  `verify_batch_and_wait` a 404, a 410 or an answer in another API version
  fails that item only (`failed`, no `status_detail`), while a 401 / 403
  refuses the key itself and raises from the call; a single wait raises all
  of them. Each poll is one request bounded by what is left of the deadline
  and by the client's own timeout (`Lenz(timeout=)` as a number, `None` or
  an `httpx.Timeout`, or an `http_client=`'s own); the client's own retry
  ladder no longer runs inside a poll.
- **No poll starts once a wait's deadline is spent** (`wait`,
  `verify_and_wait`, `verify_batch_and_wait`, `review_and_wait`,
  `citecheck_and_wait`): the ids left are timed out. 2.x polled once more at
  the deadline, past it. A `timeout=0` (or below) still reads each status
  once, as in 2.x. Each poll keeps the client's connect, read, write and
  pool timeouts, each capped by what is left of the deadline.
- **Network failures and transport timeouts raise subclasses of the class
  they raised before**: `LenzConnectionError` and `LenzRequestTimeoutError`
  (a `LenzConnectionError`), both `LenzAPIError`s, with the same message and
  the `httpx` exception as `__cause__`. Every `httpx.TransportError` worth
  sending again (a server that hung up mid-response, a proxy failure, a read
  or write error) is now retried and raised this way, and a wait polls again
  after one; 2.x let these escape as the raw `httpx` exception. For those
  (`httpx.RemoteProtocolError`, `httpx.ProxyError`, `httpx.ReadError`,
  `httpx.WriteError` and the like) the new `LenzConnectionError` is **not** a
  subclass of what 2.21 raised: code that caught `httpx.HTTPError` /
  `httpx.TransportError` for them no longer catches them; catch
  `LenzConnectionError` (or `LenzAPIError`) instead. A request
  that could never be sent (`httpx.UnsupportedProtocol`,
  `httpx.LocalProtocolError`) still raises the `httpx` exception.
- **A 404 raises `LenzNotFoundError`** (a `LenzError`, as before) and its
  `fix` reads "Check the id or key the call names: nothing with it is
  visible to this credential. Retrying will not help." (2.x advised
  retrying). The message, the
  other fields and every other error's text are unchanged.

### Added

- **Per-call request options and `with_options`.** Every method takes three
  keyword-only options for that call: `timeout` (one HTTP attempt, seconds or
  an `httpx.Timeout`; `None` keeps the client's), `max_retries` and
  `extra_headers` (added to every request the call makes; the SDK's own
  headers are refused). On the wait helpers `timeout` stays how long to wait,
  `max_retries` is the submit's, and `wait` takes `extra_headers` only.
  `client.with_options(timeout=..., max_retries=..., extra_headers=...)`
  returns a copy with other defaults that shares the connection pool; closing
  a copy does nothing. Per option the call wins over the copy and the copy
  over the client; headers merge, and `None` removes one a copy added. The
  `extract` / `assess` floors (150 s / 100 s) apply to an inherited timeout
  only. A call that passes no option sends exactly the request it sent before.
  `NOT_GIVEN` / `NotGiven` (the default of `with_options`) are exported for
  type annotations. The `timeout=` of `extract` / `assess` now also takes an
  `httpx.Timeout`. See "Per-call options" in the README.
- **Stopping a run: `cancel`, `cancel_review` and `cancel_citecheck`.**
  `client.cancel(task_id)` stops a verification and returns a `CancelResult`
  (`task_id`, `cancelled`, `status`); `client.cancel_review(review_id)` stops a
  review, its deep checks and its citation checks, and returns the full
  `ReviewFull` (what `get_review` returns); `client.cancel_citecheck(citecheck_id)`
  returns the `Citecheck`. All three answer 200 whatever the state of the run:
  `cancelled=True` whenever the run is cancelled, by this call or an earlier
  one, so a repeated or retried cancel answers True; `cancelled=False` means
  the run is not cancelled and `status` is its status, normally `completed`
  or `failed`. A task that `select` already resolved answers `cancelled=False`
  with `needs_input`: cancel the task ids `select` returned. A review or check
  that had ended comes back unchanged. They send no body and no `Idempotency-Key`
  (cancelling twice is safe), and are retried on a 5xx or a dropped
  connection like any call that is safe to repeat. An unknown id, another
  account's, or (for `cancel`) a task started on the website raises
  `LenzNotFoundError`. A task that is a review's deep check raises a
  `LenzError` with `code == "use_review_cancel"` (409): cancel the review
  instead; it is sent once, never waited on or resent. A cancelled
  verification is not charged and saves nothing; a cancelled review or citation
  check keeps charged what it delivered before the cancel (quick checks served,
  deep checks that finished, citations checked) and the rest is refunded or
  never charged. A `wait` on a cancelled run raises the failed error with
  `failure_class == "cancelled"`. Needs the API to serve version `2026-10-11`.
- **`VerificationCancelled`** (new webhook event class, `verification.cancelled`;
  its `verification` property reads the payload as `get_status` returns it,
  `status` `"cancelled"`), and `review.cancelled` / `citecheck.cancelled` typed as `ReviewEvent` /
  `CitecheckEvent`. They are sent only for work submitted with `2026-10-11`;
  a cancellation of older work keeps arriving as `*.failed`. See Breaking.
- `LenzNotFoundError` (404), `LenzConnectionError` and
  `LenzRequestTimeoutError` (see Changed).
- **`retryable` on every error**, set when the error is built: whether sending
  the same request again can succeed. `True` for a connection failure, a
  request timeout, a 429, a 5xx and a 409 `idempotency_conflict` or
  `verification_not_ready` (read from the body as sent, so also where the 2.x
  `code` is `""`); `False` for any other 4xx and a
  `LenzApiVersionError`; `None` when there was no HTTP status (a missing key,
  a `*_and_wait` timeout, a needs-input pause, a bad webhook signature). A
  failed verification, review or citation check keeps the server's value
  (`None` when it sent none), and a boolean `retryable` in a response's
  `failure` block, else at its top level, always wins. A
  `retryable=` passed to an error's constructor wins too.
- **`idempotency_key` on every error**: the `Idempotency-Key` the failed call
  sent (yours or the automatic one), `None` when it sent none; set on every
  error of that call, the wait of a `*_and_wait` helper included, and on the
  `json.JSONDecodeError` or pydantic `ValidationError` an unreadable answer
  raises (their classes unchanged). A resend is safe only
  with the same key: pass `idempotency_key=exc.idempotency_key` back and the
  server replays the first answer instead of running it again. A plain new
  call sends a new key and can run (and charge) the work twice.
- `verify_batch` and `verify_batch_and_wait` take any `Sequence` of claims
  (a `list[dict[str, str]]` now type-checks); nothing changes at run time.
- `ReviewFailedError`, `ReviewTimeoutError`, `CitecheckFailedError` and
  `CitecheckTimeoutError`: the job errors under the names the Node SDK uses
  (the same classes as `ReviewFailed`, `ReviewTimeout`, `CitecheckFailed`,
  `CitecheckTimeout`).
- **`verifications.iter()` and `library.iter(**filters)`**: every item, page
  after page from `page`, fetched lazily (a page only when its first item is
  asked for), the page size read from each response, ending after a short or
  empty page, once the pages read reach the response's `total`, when a
  response states no positive `page_size`, or (without yielding it) when the
  server answers another page than the one asked for. A start page below 1
  raises `ValueError`. `library.iter` takes `list`'s filters and refuses
  `sort="random"` (`ValueError`), which is not exhaustive.
- **`.verification` on the `verification.completed`, `verification.failed`
  and `verification.needs_input` webhook events**: the verification as
  `client.get_status` returns it (a `TaskStatus`; on a completed event the
  verdict is `.verification.result`, a typed `Verification`), built from
  either payload shape, `None` when a payload cannot be read as one or its
  nested status is not the event's. A key a sparse `result` leaves out reads
  as `event.result` reads it (`visibility` `"private"`, `depth` `"standard"`,
  `created_at` `""`, ...). A
  property, so the events' fields and `repr` are unchanged; the dict
  `result` stays (prefer `.verification`). A recognised event whose nested
  `result` is not an object no longer crashes `parse_webhook`.
- `Verdict`, `VerdictLabel`, `Confidence` and `Depth`: `Literal` aliases of
  the accepted values, for comparisons and exhaustive matching. Fields and
  arguments stay `str`.
- `verify`, `review_and_wait` and `citecheck_and_wait` list every option they
  forward (`source_url` and `webhook_url`; every `review` / `citecheck`
  option) as keyword-only parameters with the same defaults, instead of
  `**kwargs`, so editors complete them and type checkers check them. Request
  bodies are unchanged, and an unknown option is still a `TypeError`.
- A "First call" at the top of the README, and runnable review and
  citation-check examples (`examples/core/review_draft.py`,
  `examples/core/citecheck_draft.py`); CI type-checks every example. The
  quickstarts no longer fail when no claim needed escalating, read failed
  rows by `status == "failed"`, and give `depth="low"`'s price (5 credits, not
  10) everywhere.

### Deprecated

Kept in 3.x with their 2.x values, so 2.x code runs unchanged; they will be
removed in a future major release. Move to the newer names when convenient.
Reading them emits no new warning (the ones that already warned still do);
the deprecated properties carry a PEP 702 `@deprecated` marker, so editors
strike them through and type checkers report them. `typing_extensions>=4.5` is
now a declared dependency (pydantic already installs it).

| 2.x name | Use instead |
|---|---|
| `ExtractedClaims.claim` | `claims[0].claim` |
| `ExtractedClaims.identified_claims` | `claims` (each `.claim`) |
| `ExtractedClaims.locations` | `claims` (each `.positions`) |
| `ExtractedClaims.candidate_claims`, `AssessClaim.candidate_claims`, `AssessResponse.candidate_claims`, `TaskStatus.candidates`, `TaskStatus.similar_claims` | none: always empty |
| `AssessClaim.verdict == "Error"` (with `confidence == "low"`) | `status == "failed"` |
| `AssessClaim.dissent`, `ReviewAssessment.dissent` | none: always `None` |
| `AssessClaim.error_code` | `failure.code` (`no_checkable_claim` where it reads `no_claim`) |
| `AssessClaim.hint` | on a failed row `failure.hint`; on a completed row that found other claims, `more_claims` (the sentence is no longer sent; the attribute still reads it) |
| `AssessClaim.identified_claims`, `ReviewAssessment.identified_claims` | `more_claims` |
| `ReviewAssessment.error_code`, `ReviewAssessment.hint` | `failure.code`, `failure.hint` on a failed quick check; `more_claims` on a completed one that found other claims (`hint` still reads "This text holds more than one claim.") |
| `AssessResponse.error`, `AssessResponse.error_code` | `status` and `failure` |
| `CandidateClaim.text` | `claim` |
| `TaskAccepted.claim_text`, `BatchItemResult.claim_text` | `claim` |
| `TaskAccepted.chain_id` | none: no longer sent, reads `""` |
| `TaskStatus.error` | `failure.detail` |
| `TaskStatus.failure_reason` | `failure.code` (`no_checkable_claim` where it reads `not_a_claim`) |
| `TaskStatus.failure_class`, `.retryable`, `.docs_url` | `failure.failure_class`, `.retryable`, `.docs_url` |
| `TaskStatus.hint` on a failed run | `failure.hint` |
| `TaskStatus.failure_detail` | none: always `""` |
| `FailureBlock.failure_reason` | `code` |
| `Verification.modified_at`, `VerificationListItem.modified_at`, `ReviewVerification.modified_at` | `completed_at` |
| `ReviewSummary.claim_limit_reached` | `claim_limit_exceeded` (some claims were left out; `reached` also held at exactly the limit) |
| `ReviewSummary.citation_limit_reached`, `CitecheckSummary.citation_limit_reached` | `citation_limit_exceeded` |
| `Usage.verify`, `Usage.ask`, `Usage.assess` | `credits.remaining // costs[<capability>]` (and `credits.total`, `credits.extra`) |
| `Usage.quota_resets_at` | `credits.resets_at` |
| `UsageCredits.bonus` | `extra` (warns) |
| `UsageCapacity.credits` | `UsageCapacity.bonus` (warns); the block itself is deprecated, see above |
| `LenzQuotaExceededError.credits_remaining` | `remaining` (warns); `credit_balance` is the credit pool |
| `Progress` mapping access (`p["step"]`, `p.get`, `in`, `keys()`, `values()`, `items()`) | attributes (`p.step`) |
| `ASSESS_LIST_TIMEOUT` | `ASSESS_TIMEOUT` |

`LenzQuotaExceededError.credits_remaining` and the `Progress` mapping access
were announced for removal in 3.0: they are kept instead, with their warnings
(where they had one), and will now be removed in a future major release, like
the rest of this list.

The status value `not_a_claim` on `/extract` reads where the API now sends
`no_checkable_claim`; there is no other spelling to read it by.

### What reads differently

The API now words or sends a few things differently, which no client can
rebuild:

- A failed check's `error` (and the `LenzPipelineError` message built from
  it) reads "Pipeline stopped at: <code>" or its fixed sentence, as a running
  check's failure did; a failure read back from storage said "Pipeline
  stopped: <code>." in 2.x. A `task_error` reads "Pipeline failed." and a
  `task_stuck` "The task was never completed and has been marked failed.",
  where 2.x said one of several sentences for each (e.g. "Unexpected
  result.", "Unexpected pipeline step: <step>", "We hit a snag finalizing
  your result. Please try submitting again.", "The task was never picked up
  and has been marked failed.").
- The 409 `verification_failed` from `verifications.get` carries the run's
  own `hint` (and so `fix`, and the CLI's message), where 2.x sometimes
  carried a generic one; a failed poll read back from storage can carry a
  `hint` 2.x left out.
- Some other hints and 4xx messages are worded anew (the `message` / `cause`
  of a blank input other than a blank claim or an unparseable body, a failed
  review's hint).
- The values of a `failure` block (`TaskStatus.failure`, `AssessClaim.failure`,
  `AssessResponse.failure`) are the server's, where 2.21 rebuilt them from the
  2.x fields: `failure.detail` is the API's sentence ("No sources about the
  claim were found.", where 2.21 read "Pipeline stopped at: research_empty",
  and on an /assess row a sentence where 2.21 read `None`); on a failed
  /assess row `failure.docs_url`, `failure.failure_class` and
  `failure.retryable` are filled (2.21: `""`, `""`, `None`), and
  `failure.hint` too where the API sends one. `failure.code` reads the same.
  The 2.x attributes (`error`, `error_code`, `hint`, ...) keep their 2.x
  values.
- Reviews: a review row stored without a failure block (a quick-check row or
  a `ReviewFailure`) reads one, where 2.x read `failure` `None`; a deep
  check's `modified_at` is computed from its completion time by the 2.x rule
  instead of read as stored; a completed quick-check row that found other claims reads the fixed 2.x
  `hint` ("This text holds more than one claim."); a failed one has none.
- An extraction the API first read as not a claim and then found one in says
  `status == "ready"` (2.x: `"not_a_claim"`).
- `verify`'s receipt has no `chain_id` (`TaskAccepted.chain_id` reads `""`);
  the `task_id` of a `review.*` / `citecheck.*` webhook event is the review /
  citation-check id (deduplicate on `event_id`); a repeated `verify` answered
  from the first one is a 202 (the SDK returns the same receipt either way).

### Migration

**Required**

- **Webhook receivers.** Webhooks follow the version of the request that
  submitted the work: a `verify`, `verify_batch`, `select`, `review` or
  `citecheck` call made with 3.0 gets its webhooks in the current shape (one
  envelope: `event`, `event_id`, the work's id, `status`, and the polled body
  under `verification` / `review` / `citecheck`). A service that RECEIVES
  your webhooks and parses them with lenz-io 2.20 or older, or reads the raw
  JSON, must be upgraded to 2.21 or later (which reads both shapes) before
  the service that SENDS requests moves to 3.0. Work submitted with 2.x keeps
  sending the original shape.
- **The API must answer `2026-10-11`.** 3.0 raises `LenzApiVersionError`
  from any call whose response names another version; until the API serves
  `2026-10-11` for your account, stay on 2.21.
- **Code that reads raw bodies**: `exc.body` and `event.raw` are the body as
  sent, in the current shape (`failure` blocks, `claims` lists,
  `completed_at`, `more_claims`, `docs_url`, `retry_after`); read the
  attributes instead, or stay on 2.21 until you move.
- **Tests with recorded 2.x response bodies** must be re-recorded: the SDK
  reads the current shape only, so a stored 2.x body no longer parses into
  the values it did.
- **Replays of requests made before the switch.** An idempotent request
  first sent before lenz.io served `2026-10-11` and replayed with the same
  `Idempotency-Key` afterwards answers with its stored reply in
  `2026-05-13`, which 3.x refuses with `LenzApiVersionError`. Replays are
  kept for 24 hours and replies stored since 2026-10-09 are kept in both
  versions, so in practice none remain at release. If you meet one, finish
  that work with 2.x; never change the key to get past it, which would run
  (and charge) the request again.
- **The values the API no longer sends** (see Breaking): `chain_id`, the
  review / citecheck webhook `task_id`, reworded failure sentences and hints,
  and an `/extract` status on a mixed input.

**Optional**

- Move off the deprecated names (see Deprecated): they keep working with
  their 2.x values, so nothing breaks if you do not.
- `model_dump()` and the CLI's `--json` return the 2.x-compatible fields
  (computed from the response) plus the current-shape keys the server sent;
  they are not the wire body. If a script parses `--json` output, it keeps
  finding every 2.x key but `chain_id` (see Changed) and gains the new ones.
- `webhook_url` keeps its meaning, though not always its bytes: on `verify`
  and `verify_batch` an empty or whitespace-only value (the default is `""`)
  means your key's default webhook and is not sent (2.x sent a
  whitespace-only one; see Changed for its effect on a pinned
  `Idempotency-Key`); on `review` and `citecheck`, `None` means your
  key's default and `""` means no webhook. In the current shape the API reads
  a missing `webhook_url` as the key's default and `""` as no webhook on
  every endpoint.

## [2.21.0] - 2026-10-09

Minor release. Existing code keeps working unchanged; nothing to do on
upgrade.

### Added

- **`language="auto"`** on `assess`, `verify` / `verify_and_wait` and
  `ask.send`: the answer comes back in the language of the submitted text (on
  `ask.send`, the language of the claim being discussed). A concrete code
  always wins, and leaving `language` out still means English. On `assess`
  with a `claims` list one language is chosen for the whole request (the one
  most items are written in, else English); name a code to answer a
  mixed-language list in one language. `extract`, `verify_batch`, `citecheck`
  and `review` do not take `auto`. The SDK sends `language` as given, so no
  code changed; this entry is documentation and tests.
- **Reads both response shapes.** The API is adding a newer, dated response
  shape that gives each field one name across every endpoint. This release
  still asks for the original shape (it sends the same `X-Lenz-API-Version`
  as before), and every model now reads either shape. A body is read as the
  newer shape only when it carries something only that shape has; every other
  body is parsed exactly as before (same fields, values, dump, `repr`,
  `exclude_unset`, schema and pickle). The newer names are read-only
  properties, computed from whichever shape arrived, not model fields:
  - `ExtractedClaims.claims`: every claim found, always a list, each an
    `ExtractedClaim` with `claim` and (with `locate=True`) `positions`.
  - `AssessClaim.status` (`"completed"` / `"failed"`), `AssessClaim.failure`
    (`code`, `detail`, `hint`, `failure_class`, `retryable`, `docs_url`) and
    `AssessClaim.more_claims`; `AssessResponse.status` (`ok` /
    `no_checkable_claim` / `error`, the `AssessStatus` constant) and
    `AssessResponse.failure`.
  - `TaskStatus.failure`, the same block, on a failed verification.
  - `TaskAccepted.claim`, `BatchItemResult.claim`, `CandidateClaim.claim`.
  - `completed_at` on `Verification`, `VerificationListItem` and
    `ReviewVerification`.
  - `FailureBlock.code` and `FailureBlock.detail`;
    `ReviewAssessment.more_claims`; `ReviewSummary.claims_found`,
    `claim_limit_exceeded` (``None`` from the original shape, which says only
    that the limit was reached: read `more_claims` there) and
    `citation_limit_exceeded`;
    `CitecheckSummary.citation_limit_exceeded`.
  - Webhooks: an `event_id` property on every event,
    `VerificationFailed.failure`, `VerificationNeedsInput.reason` and
    `.claims` (properties: `dataclasses.asdict` and `repr` are unchanged). A review or citation-check
    event in the newer shape carries no `task_id`; it then reads the
    `review_id` / `citecheck_id`, so code keyed on `task_id` keeps one key per
    review (deduplicate deliveries on `event_id`, as before). `parse_webhook` and
    `LenzWebhooks.parse` read the newer envelope too (`event`, `event_id`,
    the work's id, `status`, and the polled body under `verification` /
    `review` / `citecheck`).
  - Errors: a 409 for a failed run, a 429 and a 422 are read in either shape
    (`failure` block, `retry_after`, `errors` list with a sentence `detail`).

### Deprecated

- The older names, kept with the meaning they always had, whichever shape
  arrives: `ExtractedClaims.claim` / `identified_claims` / `locations`;
  `AssessClaim.error_code` / `hint` / `identified_claims` (a failed row still
  reads `verdict == "Error"` and `confidence == "low"`); `AssessResponse.error`
  / `error_code`; `TaskStatus.error` / `failure_reason` / `failure_detail`;
  `claim_text` and `CandidateClaim.text`; `modified_at` (computed from
  `completed_at` with its original rule: set only when the verification
  completed on a later UTC calendar day than it was created);
  `FailureBlock.failure_reason`; `ReviewAssessment.error_code` / `hint` /
  `identified_claims`; `claim_limit_reached` / `citation_limit_reached`;
  `Usage.quota_resets_at` and the `verify` / `ask` / `assess` blocks (computed
  from `credits` and `costs` when a response leaves them out). Reading them
  does not warn, and the JSON schema is unchanged. "Nothing
  checkable" keeps its old spelling in the old fields (`not_a_claim`,
  `no_claim`) and reads `no_checkable_claim` in the new ones;
  `ExtractedClaims.status` keeps `not_a_claim`.

### Changed

- **`verify`, `verify_and_wait`, `verify_batch` and `verify_batch_and_wait`
  no longer send `webhook_url: ""`** when no webhook URL was given (nor an
  empty per-item `webhook_url`). The API has always read an empty value there
  as "use the key's default webhook", the same as leaving it out, so nothing
  changes now; leaving it out keeps that meaning on later API versions, where
  `""` means "no webhook". `review` and `citecheck` send `webhook_url` exactly as
  before (there `""` means "no webhook").
- A newer-shape body's `model_dump()` holds what the server sent plus the
  original fields filled in from it.

### Before the SDK asks for the newer shape

This release only reads the newer shape; it keeps asking for the original
one. Two things to settle before a release sends the newer date:

- A 422's `code` there is the server's new one (`blank_input` where the
  original said `blank_item`, `validation_error` where it said nothing), and
  its `message` is the server's sentence, not the field list.
- `lenz verify --json` on a `needs_input` run prints each option as the model
  dumps it: from the newer shape that is `{claim, domain, text}`, not
  `{text, domain}`.

### Correction

- **The /me/usage fields are not removed on 2026-11-29.** Earlier entries
  (2.9.0 and 2.14.0) said `UsageCredits.bonus`, `UsageCapacity.credits` and the
  per-capability `verify` / `ask` / `assess` blocks would go on that date.
  They are deprecated and kept for existing callers; the API keeps sending
  them to integrations built against its original shape. Their deprecation
  warnings no longer name a date.
- **`TaskStatus.candidates` and `similar_claims` are not removed on
  2026-11-29** either (2.18.0 said so). They are deprecated, always empty, and
  kept.

### Docs

- `openapi.json` resynced from the API: the `X-Lenz-API-Version` request
  header and response header, `language: "auto"`, a documented error body on
  every operation, and the `/verify/batch` and `/select` receipts as `202`.

## [2.20.0] - 2026-10-05

### Added

- **`AssessResponse.more_claims`**: `assess(claim=...)` on a text that makes
  more than 20 claims checks the 20 most check-worthy and lists the rest here,
  unchecked and free, most check-worthy first. Send them back with
  `assess(claims=...)`, 20 a call. `[]` on the list form, on a text with 20
  claims or fewer, and from older servers.

### Changed

- **`extract` finds up to 100 claims** (an API change; was 20). The README's
  extract → assess example now sends them to `assess` 20 a call: a list of
  more than 20 is refused with a 422.
- **Dropped Python 3.9** (EOL 2025-10-07): `requires-python` is now `>=3.10`.
  No patched release of `anyio` (a critical CVE), `pytest`, `requests` or
  `urllib3` exists for 3.9, so there was no vulnerability-free dependency
  set below 3.10. `uv.lock` now resolves each to a single current version
  (`anyio` 4.15.1, `pytest` 9.1.1, `requests` 2.34.2, `urllib3` 2.8.0) with
  no Python-version split.

### Docs

- `openapi.json` resynced from the API: besides `more_claims`, it picks up
  the copy that changed since the last sync (a cached answer is free; extract
  finds up to 100 claims).

## [2.19.0] - 2026-09-30

### Changed

- **A verdict served from the server's cache is free** (an API change; the
  SDK code is unchanged). A `verify`, `assess` or `review` claim that gets back
  a verdict checked in the last hour is no longer charged, so a tool that
  resends the same request pays once. A citation judged from the server's
  judgment cache is refunded like an unchecked one. The exception is a
  `verify` that issues a business plan a new warranty certificate, still
  charged at the requested depth.
- **`assess` waits up to 100s** (`ASSESS_TIMEOUT`, was 45s; the deprecated
  alias `ASSESS_LIST_TIMEOUT` follows it). The API now gives a long text up to
  90s to be assessed instead of refusing it early, and the SDK waits 10s longer
  than the server works. A longer client timeout you configured is still kept.
- **`extract` waits up to 150s** (`EXTRACT_TIMEOUT`, was 90s), for long inputs.
- **The polling helpers wait longer by default**: `wait` and
  `verify_and_wait` 300s (was 120s), `verify_batch_and_wait` 300s (was 180s),
  and `lenz verify --timeout` 300s (was 180s). A timeout behaves as before:
  `LenzTimeoutError` (or a `status="timeout"` row) carrying the `task_id` to
  resume from.

### Added

- **Automatic idempotency keys on `extract`, `select` and `verify`.** Each call
  sends a random `Idempotency-Key`, generated once and reused across that
  call's own retries, so a retry after a timeout or network drop gets the first
  attempt's answer (or task) instead of running the request again. Like
  `assess`, each method takes `idempotency_key=` to pin your own and
  `idempotency=False` to send none. The key is never derived from the request
  body, so the same text sent again later is still a new request. `ask.send`
  is unchanged and generates no key.

## [2.18.0] - 2026-09-30

### Changed

- **Review: `suggested_rewrite` also from the quick check.** An issue's
  `suggested_rewrite` comes from the claim's deep check when it has one;
  otherwise, when the review asked for suggested edits (`suggest_edits=True`),
  from the quick check, for a claim found `False` or `Mostly False` with high
  confidence. Likewise `suggested_edits` now also appears on claim rows that
  stayed on the quick verdict. The issue's `source` (`assessment` |
  `verification`) says which check it came from. The quick check's rewrite is
  also on each claim row as `ReviewAssessment.suggested_rewrite` (new;
  `None` when not asked, when there is none, and from a server that predates
  it).
- **`suggested_rewrite` on a verification answers the same question the
  claim answers** (an API change; the SDK code is unchanged). It may replace
  the claim's subject when the subject is the wrong part ("Venus is the
  closest planet" becomes "Mercury is the closest planet"), negates the claim
  when the evidence establishes it is false but names no right answer, and
  stays `None` when the evidence only finds no support.
- **`partly_supported` is no longer a citation issue** (an API change; the SDK
  code is unchanged). The row stays in `citations` with `is_issue` false, and
  is left out of `citation_issues` and `summary.citation_issues`, so on its own
  it no longer makes `outcome` `issues_found`.

- **`lenz review FILE` checks the draft's sources by default**: the first 20,
  at 1 credit per checked citation, stated in `--help` and in the run's
  opening line ("Checking claims and up to 20 citations…").
  `--max-citations N` checks the first N, `--max-citations 0` none. This is
  the CLI's default only: `client.review()` still checks no citation unless
  `max_citations` is passed, as the API does. The full report stays the
  default view; `--issues` is the issues-only one.
- **`lenz review --issues` is a complete editor's view.** Every source issue
  prints its reviewer's note, "Not in the source" included (it has no passage
  to quote, so its reason used to be dropped). The sources Lenz could not
  check are listed under "Check these by hand", each with why. Claims whose
  final verdict is not an issue but has low confidence get their own "Low
  confidence" section. Links print on one line, never wrapped by the CLI, so
  a terminal keeps them clickable.

### Added

- **`assess(..., suggest_rewrite=True)`**, and `AssessClaim.suggested_rewrite`
  on every row: the claim with its wrong part corrected, when the check found
  it `False` or `Mostly False` with high confidence; `None` when not asked,
  outside that, or when there is no correction to write. Both forms (`claim`
  and `claims=[...]`), per row, no extra credit. It is written from the quick
  check's reasoning and is not itself verified: review it, or run it through
  `verify`, before using it. Leave it out and the request, and its idempotency
  key's body, are exactly as before; a row without the key parses with it at
  `None`. Needs a server that knows the option: an older one refuses it with
  a 422.
- **`review(..., suggest_edits=True)`**: for each claim whose deep check
  suggests a rewrite, the smallest edits to the draft that make it say what
  the rewrite says, in the draft's own language, as
  `ReviewClaim.suggested_edits` (copied on `ReviewIssue.suggested_edits`): a
  `SuggestedEdits` block (`status`, `edits`) of `SuggestedEdit` spans
  (`position`, `start`, `end`, `text`, `replacement`) of the text you sent.
  `review.policy.suggest_edits` (`EscalationPolicy`) echoes the option. Leave it out and the request,
  and its idempotency key's body, are exactly as before; a body without the
  keys parses with them at `None` / `False`. Needs a server that knows the
  option: an older one refuses it with a 422.
- **`lenz review --language CODE`** and **`lenz citecheck --language CODE`**:
  the language the results are written in (ISO 639-1, e.g. `de`). Default
  English.


`review` can check a draft's citations, and `citecheck` runs that check on
its own, on a draft or on statement-source pairs: does each linked source (a
URL or a DOI) say what the draft says it does? Nothing the SDK already sends changes:
leave `max_citations` out and the request, and its idempotency key's body, are
exactly what 2.17.0 sends. A review body without the citation keys still
parses, with the new keys at their defaults.

### Added

- **`client.review(text, max_citations=N)`** (1-20), sent inside the API's
  `escalate` object as `escalate.max_citations`: the draft's first N
  citations are checked. `None` or `0` sends nothing and checks none.
  `review_and_wait` passes it through.
- **`max_assessments=0`**: a review that checks no claim, e.g. a review of the
  draft's citations only.
- **The citation models.** `ReviewFull.citations` (one `ReviewCitation` per
  citation: `reference`, `cited_url`, `doi`, `statement`, `quotes`,
  `position`, the derived `result` and the `check`), and on both views
  `citation_issues` (`ReviewCitationIssue`, most serious first) and
  `citation_failures` (`ReviewCitationFailure`). On a quote finding,
  `missing_quote` (on the check and on the issue) is the excerpt that was not
  found. `summary` gains
  `citations_found`, `citations_selected`, `citation_limit`,
  `citation_limit_reached`, `citation_checks` (`checked`, `unchecked`,
  `failed`), `citation_issues` and `citations_skipped`; `policy` gains
  `max_citations`. `more_claims` and `more_citations` (`ReviewMoreCitation`:
  `index`, `reference`, `cited_url`, `doi`, `sentence`, `position`) list what
  the draft holds past `max_assessments` and `max_citations`: found but not
  checked; `None` until the draft is read. Every new key has a default (`[]`,
  `None`, `0`), and every finding, reason and status is a plain
  string, so a value the API adds later passes through. The nested shapes
  (`ReviewCitationCheck`, `ReviewCitationResult`, `ReviewCitationRecord`,
  `ReviewCitationDifference`, `ReviewCitationCheckCounts`) live in
  `lenz_io.models`. A citation's `position` (on `ReviewCitation`,
  `ReviewCitationIssue` and `ReviewMoreCitation`) is a `Position`, the same
  model as a claim's: `start` and `end` set, `text` `None` (the row carries
  the `statement`).
- **`ReviewEvent.review`** and `parse_webhook` carry the same keys.
- **`lenz review --max-citations N`** (0-20): after the claims, the
  count ("23 sources cited in your draft"), the key numbers ("8 checked, 7
  with a problem. 2 could not be checked."), then each source issue with the
  draft's sentence, the link and the passage from the source. A source issue
  exits `1`, like a claim issue; a failed source check exits `2`. Then one
  line for what was found and not checked ("2 more claims and 13 more
  citations were found but not checked.").
- **`client.citecheck(text | pairs=..., max_citations=..., language=...,
  webhook_url=..., idempotency_key=...)`**, **`client.get_citecheck(id)`**
  and **`client.citecheck_and_wait(...)`** for `POST /citecheck` and
  `GET /citechecks/{citecheck_id}`: the citation check on its own. Send a
  draft (its first `max_citations`, 1-20, are checked) or 1 to 20
  statement-source pairs (`CitationPair`: `statement` and one of `url` or
  `doi`, with optional `quotes` and, for a DOI, what the reference gives).
  Exactly one of the two; `max_citations` with pairs raises `ValueError`.
  `language` is the language Lenz writes the reasoning in (English when
  omitted); hints are always in English. The
  body is a `Citecheck`, with the review's citation rows, `summary`,
  `credits` and `more_citations`. `citecheck_and_wait` raises
  `CitecheckFailed` (a `LenzPipelineError`) or `CitecheckTimeout` (a
  `LenzTimeoutError`, with `partial`). `citecheck.completed` /
  `citecheck.failed` webhooks parse into a `CitecheckEvent`.
- **`lenz citecheck FILE`** and **`lenz citecheck --pairs FILE.json`**, with
  `--max-citations N`, `--detach`, `--resume ID` and `--json`; the exit code
  follows the outcome, as for `lenz review`.
- **`lenz review --max-assessments N`** (0-20): how many of the draft's claims
  get a quick verdict. `lenz review draft.md --max-citations 20 --max-assessments 0`
  checks the draft's sources and no claim.
- **`client.extract(text, locate=True)`**: only the claims that could be
  traced directly back to the text are returned, and the new
  `ExtractedClaims.locations` says where the text makes each one. A claim
  found nowhere in the text, or found with a different figure, is left out;
  a list that ends up empty answers `status: "not_a_claim"`. Locating adds a
  few seconds. `locate` defaults to false: leave it `None` and nothing is
  sent (the server default governs); an explicit `False` is sent.
- **`Position`** (`start`, `end`, `text`): one model for every place in the
  text you sent, a claim's passage or a citation's statement. `start`/`end`
  index the text as sent in Unicode code points, half-open, so
  `text[start:end]` is the passage (`end` exclusive); both are `None` when
  the input was a URL. `text` is the passage (for a URL input, the only
  pointer to it), and `None` on a citation's `position`, whose row carries
  the statement. Exported from `lenz_io`.
- **`ExtractedClaims.locations`** (`list[ClaimLocation] | None`): one
  `ClaimLocation` (`claim`, `positions: list[Position] | None`) per returned
  claim, in the order of `identified_claims` (one entry for a single
  `claim`). `positions` is `None` only when that claim could not be placed;
  on `/extract` every returned claim is placed. `locations` is `[]`
  when every claim was left out, and `None` when `locate` was not set, when
  the extraction found no claims, or when the
  claims could not be located (the list is then returned unfiltered). A body
  from an older server without the key parses as `None`. `ClaimLocation` is
  exported from `lenz_io`.
- **`lenz extract --locate / --no-locate`**: unset by default (the server
  decides). The pretty output prints each claim's passages, with their
  `start`-`end` span when there is one.
- **`/review` checks only the claims traced directly back to the draft**, as
  `extract(locate=True)` does: a claim found nowhere in the draft, or found
  with a different figure, is left out.
- **`ReviewClaim.positions`** (`list[Position] | None`): every place the
  draft makes the claim, in text order, at most 10. `start`/`end` index the
  `text` as sent in Unicode code points (`text[start:end]`, `end`
  exclusive), the same coordinates as a citation's `position`. For a URL
  draft, `start`/`end` are `None` and `text` carries the passage, as
  `/extract` does for a URL. `None` when the claims could not be located, or
  once a zero-retention draft is gone.
- **`more_claim_locations`** on both review views
  (`list[ClaimLocation] | None`): one `ClaimLocation` (`claim`, `positions`)
  per `more_claims` string, same order, the shape `extract(locate=True)`
  returns. `None` until the draft is read. `more_claims` stays `list[str]`.
  A body from an older server without either key parses as `None`.
  `lenz review` prints a claim's first position as `· at start-end` when it
  has offsets (never for a URL draft).

### Deprecated

- **`similar_claims`** and **`candidates`** on `TaskStatus`. Both are always
  empty: the API no longer sends them, and a `needs_input` `reason` is only
  ever `multi_claim` now (`duplicate_found` is no longer a documented
  reason). Removal is planned for 2026-11-29, together with the other
  deprecated fields. Until then a response without the keys reads them as
  `[]`, and one from an older server that still carries them parses as
  before (`similar_claims` into `SimilarVerification` objects). After the
  removal, models still accept such a body: an unknown key is kept as an
  extra field (`model_extra`) rather than rejected. They are marked
  deprecated in the JSON schema only, so reading them does not warn.
  `SimilarVerification` itself stays: it is the item type of
  `client.related()` (`GET /verifications/{id}/related`).
- CLI: the `candidates` and `similar` keys on the `lenz verify --json`
  `needs_input` object are always `[]`, and go with the fields above.

### Changed

- CLI: `lenz verify` no longer has a separate branch for a
  `duplicate_found` pause. Any `needs_input` reason it cannot resolve with
  `select` ends in a `needs_input` error that names the reason.

### Fixed

- **`lenz verify` waits the server's `poll_after_seconds`** between polls, one
  task or a batch (the shortest hint among the tasks still running), instead of
  a fixed 2.5 s; 2.5 s stays the fallback when the body carries no hint, as the
  client's `*_and_wait` helpers already did.

## [2.17.0] - 2026-09-26

`review`: the whole extract → assess → verify ladder on a draft in one call,
from the SDK and from the CLI (below). And a new optional field on every
verification, single or listed, `suggested_rewrite`. Nothing the SDK already
sends changes, and 2.16.0 keeps working against the current API; `review`
needs an API that serves `POST /review`.

### Added

- **`client.review(text, ...)`**, which starts a review of a draft and returns
  a `ReviewStarted` with its `review_id`. Every claim gets a quick verdict;
  the ones whose verdict is `False`, `Mostly False` or `Mixed`, or whose
  confidence is `low`, get a deep check, up to five. The rule is set with flat
  keyword arguments (`verdicts`, `confidence`, `max_assessments`,
  `max_verifications`, `depth`), which the SDK sends as the API's `escalate`
  object; `None` keeps the server default and `[]` switches a rule off.
  `webhook_url` has three states: `None` (the credential's default URL), `""`
  (no webhook) or a URL. An `Idempotency-Key` is generated when you pass none:
  a resend with the same key within 24 hours returns the same review, and a
  new key is a new review.
- **`client.get_review(review_id)`** returns a `ReviewFull` (every claim, in
  `claims`), and **`get_review(review_id, view="issues")`** a `ReviewIssues`
  (the issues and the failures only). Both carry `status`, `outcome` (`clean`,
  `issues_found`, `incomplete` or `unchecked` once the review ends), `issues`,
  `failures`, `summary`, `policy`, `credits.charged` and, on a failed review,
  `failure`.
- **`client.review_and_wait(text, *, timeout=600, on_update=None, **kw)`**
  submits and polls on the review's `poll_after_seconds` (never faster than
  every 5 s), one request per poll bounded by the time left, so the wait
  keeps to its `timeout`; a failed read (5xx, network, 429) is retried on the
  next poll. `on_update(review)` fires on every poll whose body changed; the
  helper is silent without it. It raises `ReviewFailed` (a
  `LenzPipelineError`, with `review_id`, `error_code`, `hint` and the final
  `review`) on a failed review, and `ReviewTimeout` (a `LenzTimeoutError`,
  with `review_id` and the last body read as `partial`) when the timeout
  passes first.
- **The review models.** Exported from `lenz_io`: `ReviewStarted`,
  `ReviewFull`, `ReviewIssues`, `ReviewIssue`, `ReviewClaim`,
  `ReviewFailure`, `EscalationPolicy`, `Escalation` (`matched_rules`,
  `disposition`) and `FailureBlock`. The nested shapes you read but never
  build (`ReviewEnvelope`, `ReviewResult`, `ReviewAssessment`,
  `ReviewVerification`, `ReviewEntity`, `ReviewSummary`, `ReviewCredits`,
  `ReviewAssessmentCounts`, `ReviewVerificationCounts`) live in
  `lenz_io.models`. Every status, disposition and error code is a plain
  string, so a value the API adds later passes through.
- **`ReviewEvent`** for the `review.completed` and `review.failed` webhooks,
  from `LenzWebhooks.parse`, with `event_id` (the same on every retry of one
  delivery: deduplicate on it), `review_id` and the final `review`.
  **`parse_webhook(body)`** parses a body whose signature was already checked
  into the same typed events. Verification events are unchanged, and an event
  type this version does not know still parses as a plain `WebhookEvent`.
- **`lenz review draft.md`** in the CLI. It prints the quick verdicts as they
  arrive and rewrites each row as its deep check lands, then the result: the
  claim, the verdict, the key finding (deep check) or the reviewers' note
  (quick check), and `Suggested rewrite: …`. The exit code is the outcome:
  `0` clean, `1` issues found, `2` anything else, errors included, so a CI step
  never reads an outage as a clean draft. `--issues`, `--json`,
  `--max-verifications N`, `--depth low`, `--detach` and
  `--resume <review_id>`. The draft is a file, `-` for stdin, or one URL.
- **Error codes from `/review`** reach you on the existing errors, with
  `code` set: `review_in_flight` (a `LenzRateLimitError` carrying
  `retry_after`; the account already has its maximum of reviews running),
  `capacity` and `upstream_unavailable` (`LenzUpstreamUnavailableError`),
  `invalid_verdict_label`, `invalid_confidence_band` and
  `webhook_secret_missing` (`LenzValidationError`), and, for a URL review,
  `extract_daily_limit` (`LenzRateLimitError`).
- **`suggested_rewrite` on `Verification`, `VerificationListItem` and
  `LibraryItem`**, a string or `None`: a suggested rewrite of the
  verification's `claim` that its findings support, to use in place of the
  original sentence. It has not been verified itself: before using it, review
  it or run it through `client.verify(...)`. It is `None` for a true claim,
  when no correction is established, and on verifications that predate the
  field; an older server that does not send the key also reads `None`. It is on every verification, single or listed:
  `verifications.get`, `verifications.list`, `library.list`,
  `verify_and_wait`, `wait`, a completed `get_status`, and the `result` of a
  `verification.completed` webhook. Not on `assess` rows.
- **The CLI prints it.** `lenz verify`, `lenz show` and the batch view print
  one line under the key finding, `Suggested rewrite: …`, when a verification
  carries one; `--json` output includes the field.
- **The `openapi.json` snapshot is refreshed.** Additive only: the two
  review paths and their schemas, `suggested_rewrite` on the verification
  detail and on list items, and their lines in the API description.

### Changed

- **A 429 with `code: review_in_flight` is raised at once** instead of being
  slept through by the retry ladder: a review runs for minutes, so waiting the
  stated minute inside the call would most likely end in the same answer.
  Every other 429 is retried as before.

## [2.16.0] - 2026-09-24

A new error, `LenzGoneError`, for a verification removed by its account's
retention period, and a new coverage reason, `account`. Nothing the SDK sends
changes, and 2.15.0 keeps working against the current API.

### Added

- **`"account"` in `CoverageReason`.** An account on Pro or Scale can now turn
  warranty certificates off; its verifications then read
  `coverage.reasons == ["account"]` (or `["plan", "account"]` after a
  downgrade). A verification that already carries a certificate keeps it.
  `reasons` was already typed as strings on the wire, so earlier SDK versions
  read the new value without error. The `CoverageReason` Literal gained a
  member: a type-checked exhaustive `match` over it needs an `"account"` case.
- **`LenzGoneError` for HTTP 410.** An account on Pro or Scale can set a
  retention period; a verification older than it answers 410 with
  `code: "purged"` and `purged_at`. The SDK now raises `LenzGoneError`
  (a `LenzError`, carrying `purged_at`) instead of a plain `LenzError`, and
  `wait()` raises it at once instead of polling until its timeout.
  `verify_batch_and_wait` reports such an item as `failed` with no
  `status_detail`. Only a 410 whose body carries `code: "purged"` is this
  error; any other 410 stays a plain `LenzError`. The CLI reports it as
  `gone`, and a batch it is polling marks that row failed and keeps going.
- **The `openapi.json` snapshot is refreshed.** Additive only: the 410
  `VerificationPurgedOut` schema, `account` in the coverage reasons, and the
  404, 409 and 410 responses of `GET /verifications/{id}`.
- **`lenz_io.errors.__all__` lists every public error.** `LenzGoneError`,
  and the previously omitted `LenzUpstreamUnavailableError` and
  `UPSTREAM_503_CODES`.

### Changed

- **The 401 fix hint is neutral about the credential.** It used to say only
  "Generate a new key", which is the wrong advice for a key that was mistyped
  or left out. It now reads: "Your credential is missing, invalid or expired.
  Check the key you passed, or get a new one at https://lenz.io/api-credentials."
  The error class and its fields are unchanged.

## [2.15.0] - 2026-09-17

Two new optional fields on every `assess` row, `rationale` and `dissent`
(below). Nothing the SDK sends changes, and 2.14.0 keeps working against the
current API.

### Added

- **`rationale` and `dissent` on `AssessClaim`**, the two optional notes the
  API now returns on every `assess` row. `rationale` is the reasoning of a
  reviewer who agrees with the panel's verdict; `dissent`, when set, is the
  reasoning of the reviewer farthest from it. Both are reviewers' notes, not
  checked sources; for sourced evidence, call `verify`. Both default to
  `None`: an `"Error"` row has neither, and neither does a response the API
  replays from before it added them. `lenz assess` prints them under the
  verdict. Earlier SDK versions ignore the two keys and keep working.

## [2.14.0] - 2026-09-15

Two behaviour changes — an opt-in `Idempotency-Key` on `ask.send`, and typed
errors for the 409s `verifications.get` answers on a task_id — and a new name
for the non-expiring balance, `credits.extra` (all below); the rest is docs.
The only new parsing is that 409 error body and `credits.extra`, and 2.13.0
keeps working against the current API.

### Added

- **`LenzVerificationNotReadyError`**, raised by `verifications.get` when it
  is handed the `task_id` of a run that is still processing or waiting for
  input (a 409 with `code` `verification_not_ready`). It carries `task_id`,
  `status` and the server's `hint`, which is also its `fix`. A run that
  failed raises `LenzPipelineError` from the same call (`code`
  `verification_failed`), carrying `task_id`, `failure_reason`,
  `failure_class`, `retryable` and `hint`. Both used to surface as a generic
  `LenzError` whose advice was to retry and file an issue, which is wrong for
  both. Every other 409 is still a plain `LenzError`. Against an older API
  the call behaves as before.
- **`hint`** on `LenzPipelineError`, the server's one sentence on what to
  send instead (e.g. for `not_a_claim`). `wait` and `verify_and_wait` now set
  it too, as the Node SDK always has. `lenz show <task_id>` on a run with no
  result yet now says so, with `code` `not_ready`, and points at
  `lenz status <task_id>`.
- **`UsageCredits.extra`**: the non-expiring part of the balance, credits
  from grants and top-ups that are spent only once the monthly allowance is
  gone. It is the new name of `UsageCredits.bonus` and carries the same
  number. It is filled from `bonus` when the server does not send it, so it
  reads correctly against any server version.
- **`idempotency_key=`** on `ask.send`: sent as the `Idempotency-Key` header,
  so a retry of the same question replays the first reply instead of spending
  a second credit and appending a second question-and-answer pair to the
  conversation. Nothing is sent unless you pass a key: no key is generated
  for you and none is derived from the message, because asking the same
  question again is a normal thing to do on this endpoint. A retry that
  arrives while the first call is still running raises on a 409 — there is no
  finished reply to replay yet.

### Deprecated

- **`UsageCredits.bonus`**, the old name of `UsageCredits.extra`. The API
  removes it on 2026-11-29, along with the per-capability `credits` alias.
  Reading it emits a `DeprecationWarning`; it stays in `model_dump()` output
  while the server sends it.

### Changed

- **`Usage.plan` is `"pro"` for the Pro plan.** The API renamed the slug on
  2026-09-15; it was `"developer"`. Nothing in the SDK branches on it, so the
  change is the docstring, the README and the test fixtures. If your code
  compares `plan` to `"developer"`, compare it to `"pro"` (or read
  `plan_label`, which has read `"Pro"` throughout).
- `lenz usage` labels the non-expiring part of each row "extra"
  (`+ 20 extra`) instead of "bonus".

## [2.13.0] - 2026-09-15

One behaviour change, `extract`'s default timeout (below); the rest is docs
and dead CLI code. Nothing the SDK sends or parses changes, and 2.12.x keeps
working against the current API.

### Added

- **`timeout=`** on `extract`: a per-call HTTP timeout in seconds, like the
  one `assess` takes.

### Deprecated

- **`candidate_claims`** on `ExtractedClaims`, `AssessClaim` and
  `AssessResponse`, and **`candidates`** on `TaskStatus`. The API has sent
  them empty since 2026-09-12, when `/assess` stopped returning
  `error_code: "ambiguous"` and `/verify` stopped pausing with
  `reason: "clarification_required"` (a vague input is now checked on its
  most likely reading). The fields stay because the keys still arrive. They
  are marked deprecated in the JSON schema only, so reading them does not
  warn.

### Changed

- **`extract` waits up to 90s per attempt by default** (`EXTRACT_TIMEOUT`) instead of
  the 30s client timeout. The slowest extractions take 30-60s, and on a
  client timeout the SDK re-sent the call, which ran the same extraction
  again. A longer client timeout is never shortened, and `lenz extract` in
  the CLI gets the same default.
- Docs: `assess`'s 45s default is described as covering both forms, as it
  has since 2.12.0.
- Docs: `ambiguous` and `clarification_required` are gone from the
  documented values. The `/assess` row causes are `no_claim` /
  `framing_failed` / `upstream_unavailable` / `timeout`, and the `needs_input`
  reasons are `multi_claim` / `duplicate_found`. `Assessment` describes the
  current panel (Reviewers A–C, plus D and E when they disagree) and the
  older specialist panelists; `Source.snippet` is the passage around the
  quote, in the page's language; a single `assess` text answers with up to
  20 rows.
- The demo claim is no longer described as pre-cached: the API's verdict
  cache now lasts an hour, so it answers in seconds only when someone
  verified it within the hour.
- Release smoke: the `/verify` checks (SDK and CLI) run the quickstart
  claim at `depth="low"` with a 150s budget instead of expecting a cache
  hit inside 30s, which a 1-hour cache no longer guarantees.

### Removed

- CLI: the `clarification_required` picker in `lenz verify` and the
  "readings" / "did you mean" lines in the `assess`, `extract` and status
  output, which the API no longer sends anything to fill. A
  `clarification_required` pause from an older server now ends with a
  `needs_input` error that names it.

### Fixed

- `assess` no longer shortens the timeout of an `httpx.Client` passed as
  `http_client=`: its 45s floor was compared with `Lenz(timeout=...)`, which
  such a client does not use. `extract`'s 90s floor reads the client in use
  the same way, and an unbounded client stays unbounded.

## [2.12.1] - 2026-09-07

CLI only; the SDK is unchanged.

### Fixed

- **The CLI no longer dies on a non-UTF-8 locale.** Python takes the standard
  streams' encoding from the environment, so a `C`/`POSIX` locale (a bare
  container, an editor terminal, a shell started without `LANG`) gave `lenz`
  **ascii** streams. Every command that printed a claim then failed —
  `'ascii' codec can't encode characters in position 44-45` — because claim
  text carries en dashes, arrows, Greek letters and, for most of the world,
  its whole alphabet. `execute`'s catch-all reported that as a plain
  `Error:`, so it read as the *input* being rejected and sent people hunting
  for a character Lenz "doesn't like" in a document that was fine. The same
  locale broke the input side: `lenz extract - < file.txt` could not decode
  the document it was handed, and even `lenz --help` tracebacked on the em
  dash in its own help text. The entry point now forces UTF-8 on all three
  streams. Output encodes leniently (`errors="replace"`), so an
  unrepresentable character can't kill a command at its last step; **input
  decodes strictly**, so a document that genuinely isn't UTF-8 still fails
  loudly and for free rather than becoming a claim full of `\ufffd` that gets
  submitted and charged. Importing `lenz_io` as a library still touches
  nothing.

## [2.12.0] - 2026-09-06

**`assess` now defends against being charged twice for a call you never
received.** Two changes that belong together.

The single form used the 30s client default while a list call got 45s. The
server runs framing and then a 3-model panel inside one synchronous request
and divides a single time budget between them, so a single-claim call can take
as long as a list one — and when it overran, the client timed out *after* the
server had charged it. `assess` sent no `Idempotency-Key`, so the retry
charged again.

Both forms now use `ASSESS_TIMEOUT` (45s), and every `assess` call carries an
auto-generated key that is reused across this SDK's own retries.

Works against any server version: the key is simply honoured by newer servers
and ignored by older ones, and a longer timeout only ever waits longer.

### Added

- **`ASSESS_TIMEOUT`** (45s) — the default for both forms.
  `ASSESS_LIST_TIMEOUT` stays as an alias of it.
- **`assess(idempotency=...)` / `assess(idempotency_key=...)`** — same shape
  as `verify_and_wait`. On by default, generating a random key per invocation
  that is reused across retries. Pin your own to make a retry from another
  process replay too, or pass `idempotency=False` to send none.

  Deliberately random rather than derived from the claim text: an identical
  claim sent an hour later is a new question, and a content-derived key would
  replay the first answer for 24h.
- **`timeout`** joins the row `error_code` vocabulary — the call ran out of
  its time budget before that item was done. It is free, and worth resending
  as-is; sending fewer items per call makes it less likely.

### Changed

- **`assess(claim=...)` waits up to 45s**, not 30s.
- **A longer configured client timeout is no longer shortened.** `assess` took
  the flat `ASSESS_LIST_TIMEOUT` on list calls, overruling a client built with
  `timeout=60`. It now takes the max, matching the Node SDK, which always did.
- **The row `error_code` set is documented as OPEN.** Branch on the values you
  know and fall through on the rest; surface `hint` to humans, since it is
  written per cause and stays correct as causes are added.

## [2.11.0] - 2026-09-03

**`progress` on `GET /verify/status` is now a documented object.** It was
previously an untyped bag whose contents were a server implementation detail,
so nothing about it was safe to depend on. It now carries five typed fields —
`step`, `index`, `total`, `elapsed_seconds`, `poll_after_seconds` — and this
SDK models them. Lockstep release with Node 2.11.0.

Works against any server version: an older server simply never sends
`index` / `total` / `poll_after_seconds`, and the SDK falls back to its own
backoff ladder.

### Added

- **`Progress`** — `step`, `index`, `total`, `elapsed_seconds`,
  `poll_after_seconds`. `step` is one of `starting` / `framing` / `research` /
  `debate` / `adjudication` / `conclusion`; `index` is the 1-based stage
  position out of `total`. It is typed `str`, not a `Literal`, so a stage the
  server adds later passes through rather than raising.
- **`on_progress=` on `verify_and_wait`, `wait` and `verify_batch_and_wait`** —
  called as `on_progress(task_id, progress)` once per poll while a run is
  still going. It takes the `task_id` because the batch helper round-robins
  several ids in one loop. Without this the stage is invisible to anyone
  using the documented happy path, since these helpers do the polling. An
  exception raised inside your callback is logged at DEBUG and swallowed; it
  never breaks the poll.
- **Honouring `progress.poll_after_seconds`** — the poll loop uses the
  server's suggested interval in place of the fixed 2/4/8s ladder when it is
  present and within bounds. A batch waits the shortest hint in flight.
- **`TaskStatus.task_id`** — echoed by the server on every status shape.
- **`TaskStatus.docs_url`** — on a `failed` status, the page explaining that
  `failure_class`.

### Changed

- **`TaskStatus.progress` is a `Progress` model, not a `dict`.** Attribute
  access (`status.progress.step`) is the supported form. The dict access
  patterns — `progress["step"]`, `progress.get("step")`, `"step" in progress`,
  `progress.keys()` / `.values()` / `.items()`, `dict(progress)` — all keep
  working and are **deprecated; they go in 3.0.0**.
- **A completed status body no longer carries a `progress` key at all** (the
  server omits unset fields). `TaskStatus.progress` defaults to an empty
  `Progress`, so reading `.step` or `.get("step")` on one returns `""` rather
  than raising.
- `lenz verify` and `lenz status` render the stage as `Gathering evidence
  (2/5)` when the server sends `index` / `total`.

**Covered verification: the warranty block and the certificate.** Qualifying
verdicts on paid Developer and Scale plans carry a contractual warranty from
Lenz. This release types the `coverage` block that says whether a given verdict
carries it, and adds `get_certificate()` to download the signed, timestamped
document.

**Not yet live.** The server gates all of this behind a flag that is off, so
`coverage` is `None` on every response until Lenz enables it. Nothing here
breaks against a server that has never heard of the feature.

### Added — covered verification


- **`Coverage`** on `Verification.coverage` — `status`, `reasons`,
  `certificate_id`, `certificate_url`, `as_of`, `currency`, `cap`,
  `aggregate`, `terms_version`. `None` when Lenz is not operating the
  warranty, and on unauthenticated calls.
- **`Certificate`** and **`client.verifications.get_certificate(id)`** — the
  signed record, byte-identical to the public document, with `leaf`,
  `signature`, `anchors` (an eIDAS-qualified RFC 3161 timestamp plus an
  OpenTimestamps receipt), and `verifier_url` / `keys_url` so you can check it
  with the published open-source verifier **without involving Lenz**.
- **`CoverageStatus`** and **`CoverageReason`** literals, exported for
  exhaustive matching. The model fields stay `str` / `list[str]` so the SDK
  never rejects a value the server adds after this release was cut.
- **`CertificateTimestamped`** — the `certificate.timestamped` webhook event, typed.
  **This is the event to publish on, not `verification.completed`.** The
  warranty requires the certificate's timestamp to PRECEDE what you publish or
  send, so a pipeline keyed on `completed` races the anchor and can put the
  statement out before cover exists. It carries `coverage` instead of
  `result` — it reports a timestamp landing, not a verdict being produced.

### Notes for callers — covered verification
- **`coverage is None` and `status == "uncovered"` are different facts.** The
  first means Lenz is not operating the warranty, or you called without a key;
  the second means it IS operating and this verdict did not qualify —
  `reasons` says why. Do not conflate them.
- **The money fields are three, not two.** `currency` is ISO 4217 and the
  amounts are integers in **major units** — `cap=10000` means ten thousand,
  not a hundred. They are contract figures, not amounts a payment processor
  charges. Read `currency`; do not assume EUR.
- **A 404 from `get_certificate()` is not a reliable "not covered" signal** —
  it is also what you get for a verification that has no certificate for your
  account. Check `coverage.status` first.
- A **withdrawn** certificate is still returned, with `withdrawn_at` set. It is
  the record of what was warranted, and use before the withdrawal notice can
  still be covered.

## [2.10.0] - 2026-09-01

One input vocabulary: **`text` is a document, `claim` is a claim.** `extract`
takes `text`; `assess`, `verify`, batch items and `select` take `claim` /
`claims`. Lockstep release with Node 2.10.0. No request body changes: every
call still serialises to the wire keys it always has, so this release works
against any server version.

### Added

- **`client.assess(claim=...)`** — `claim` is now the first (positional)
  parameter. `text=` keeps working as an alias; `claim` wins if both are given.
- **`client.verify(text=...)`** / **`verify_and_wait(text=...)`** — `text` is
  accepted as an explicit keyword alias for `claim`.
- **`VerifyBatchItem.claim`** — batch items take `claim`; `text` stays as an
  alias. Items without `claim` are forwarded byte-for-byte as before.
- **`client.select(task_id, claims=[...])`** — `texts=` keeps working as an
  alias. The `ValueError` for an empty selection now names `claims`.
- **`client.assess(claims=[...])`** — up to 20 claims in one call, one
  parallel wave (~10-25s). Exactly one `AssessClaim` per item, in the order
  sent; an item that got no verdict comes back in position as an `"Error"`
  row (not charged) rather than being dropped. Mutually exclusive with
  `claim=` / `text=` — passing both raises `ValueError`. The single form's
  request body is unchanged. Sends `{"claims": [...]}`, which servers before
  the list form reject with a 422 — needs a Lenz API with the list form live.
  The CLI's `lenz assess "a" "b" "c"` uses it (one positional keeps the
  single form).
- **`AssessClaim.error_code` / `.candidate_claims` / `.identified_claims` /
  `.hint`** — why an `"Error"` row got no verdict (`no_claim` / `ambiguous` /
  `framing_failed` / `upstream_unavailable`, the retryable one), the readings
  when it was ambiguous, the other claims found in a compound item that were
  not assessed, and one sentence on what to send next (`None` on a plain
  verdict row). All default empty, so older servers still parse. `lenz
  assess` prints the hint under a row and lists `identified_claims` as
  "also found:".
- **`hint` on `needs_input`** — `TaskStatus.hint`, `LenzNeedsInputError.hint`
  and `VerificationNeedsInput.hint` (read from the webhook's `needs_input`
  block): one sentence on what was unclear and how to resolve it via
  `select`. `TaskStatus.hint` is also set on a `failed` status with
  `failure_reason == "not_a_claim"`. `""` from older servers.
- **`assess(..., timeout=)`** — a per-call HTTP timeout overriding the client
  default for that one request. A list call defaults to 45s
  (`lenz_io.client.ASSESS_LIST_TIMEOUT`).

### Changed

- The CLI's `lenz assess` and the multi-claim picker call the new names.
  Behaviour is unchanged.
- The documented ladder is now extract → **one** `assess(claims=...)` over
  the extracted claims → `verify_batch_and_wait` for the low-confidence rows
  → `ask` (README, `examples/core/quickstart.py`,
  `examples/core/verify_llm_output.py`, the module and method docstrings).

## [2.9.1] - 2026-08-30

Documentation only; no behaviour changes.

### Fixed

- **CLI `--depth` help** said `low` costs the same as `standard`. It costs
  half: 10 credits at `standard`, 5 at `low` (`lenz usage` prints both).
  The `lenz verify` summary now says the same, and the quickstart shows
  `--depth low`.
- **README** documented a `costs["verify_low"]` key that does not exist —
  `costs` holds capability names only. The low-depth price lives at
  `cost_options["verify"]["depth"]["low"]`, as the model and the 2.9.0
  notes already describe.

## [2.9.0] - 2026-08-29

One weighted credit pool replaces six per-endpoint quotas; `extract` takes a
`focus`; `verify` takes a `depth`. Lockstep release with Node 2.9.0 and the
server-side pool.

### Added

- **`Usage.credits`** (`UsageCredits`: `total`, `used`, `remaining`, `bonus`,
  `resets_at`) — the account's balance, and the authoritative number. Every
  billable call debits it.
- **`Usage.costs`** — the price list, keyed by **capability** at its default
  price: `{"verify": 10, "assess": 1, "ask": 1, "extract": 0}`. Read the weight
  from here rather than hard-coding it; a new capability arrives as a new key.
  Capability names and nothing else, so it is safe to iterate.
- **`Usage.cost_options`** — prices that depend on a request **parameter**,
  nested capability → parameter → value:
  `{"verify": {"depth": {"standard": 10, "low": 5}}}`. Every capability here
  also appears in `costs` at its default, so reading only `costs` is imprecise
  but never wrong. Nested rather than flat so a future parameter adds a key
  under its capability instead of a new top-level entry.
  - The low-depth price is a **price, not a capability**: there is deliberately
    no `usage.verify_low` block beside `usage.verify`, because it would report
    the same balance in a second unit. Divide `credits.remaining` by it for the
    low-depth count.
  - **You are charged for the depth you REQUESTED, not the one you were
    served.** A `low` request answered from a cached `standard` verdict still
    costs 5. The `depth` echoed on the completed verification is what the
    verdict was *produced* with, so it can read `standard` on a `low` request
    — the echo describes the evidence, the charge follows the request.
- **`Usage.plan_label`** — the tier as display copy (`"Developer"`), beside the
  stable `plan` slug. Two fields on purpose: `plan` is what you branch on,
  `plan_label` is copy and may be reworded.
- **`UsageCapacity.bonus`** — the non-expiring top-up bucket, in that
  capability's unit. 200 bonus credits read as `assess.bonus == 200` and
  `verify.bonus == 20`: 5 credits does not buy a verification.
- **`LenzQuotaExceededError.credit_balance` and `.cost`** on 402 — the credits
  left in the pool and what the rejected call would have taken, both in
  credits, so a client can tell "4 credits, and this verify wants 10" from
  "empty". `remaining` / `requested` stay in the capability's own unit. Both
  are `None` when the server omits them, matching `remaining`.
  - `credit_balance` carries the server's `credits_remaining` body field under
    a different name **on purpose**: `exc.credits_remaining` has been a
    deprecated alias of `remaining` since 2.7.0 and means a different quantity
    (verifications, not credits). Re-pointing it would have silently changed
    the number under everyone still on the deprecated path. The raw key stays
    available as `exc.body["credits_remaining"]`.
  - `cost` is **depth-aware**, not a fixed multiple: a rejected `depth="low"`
    verify reports 5, and a rejected batch that mixes depths reports its real
    summed total. Read it rather than multiplying `requested` by an assumed
    price.
- `lenz usage` leads with the balance — `5070 credits left (≈ 507
  verifications · 5070 assessments)` — with the per-capability rows and their
  per-call price beneath it. The `Verify` row's tail also carries the
  low-depth price (`· 10 credits each · 5 at depth "low"`) — on the existing
  row, not one of its own, because there is no separate low-depth allowance.
- **`focus=` on `extract`.** An optional hint of at most 300 characters —
  `focus="market size, growth and competitors"` — that narrows the result to
  the claims it names. A focus can only SELECT from the claims the extractor
  found: it cannot add a claim, reword one, reorder them, change the output
  language, or change what counts as a claim, so a claim you get back is one
  an unfocused call would have returned too, verbatim. Omitted from the
  request body when empty, so nothing changes for callers who don't use it.
  There is no client-side length check — the server's 422 is the contract, and
  a cap duplicated here would drift from it.
- **`lenz extract --focus`** on the CLI, and a distinct `no_match` line in the
  pretty renderer: "No claims matched the focus." rather than "No verifiable
  claim found in that text", which would send you off to fix the wrong thing.
- **`ExtractStatus`** — `Literal["ready", "not_a_claim", "no_match"]`, exported
  from `lenz_io` for exhaustive matching. `ExtractedClaims.status` stays `str`
  (same convention as `FailureClass`): the SDK must not reject a status the
  server adds after this release was cut.
- **`no_match`** — the status when the text HAS claims but none fall within
  your `focus`. It is a successful answer, not an error, and it is never the
  unfocused list in disguise: `identified_claims` is empty and `claim` is
  `""`. Widen the focus and call again.

- **`depth` on `verify` / `verify_batch`** (and their `_and_wait` helpers) —
  `'standard'` (server default) or `'low'`. `'low'` runs a shallower check:
  fewer sources, faster, and **half the credits** — same models throughout;
  it is not a model downgrade. Batch takes a
  batch-wide `depth`; each item dict may set its own `depth` to override it,
  exactly like `visibility`. Omitted from the request body when unset, so
  existing callers stay byte-identical on the wire and keep working against
  a server that does not know the field yet.
- **`Verification.depth`** — echoes the depth the verdict was actually
  produced with. A `'low'` request served from the result cache reads back
  `'standard'`. `""` on servers that predate the field.
- **`lenz verify --depth standard|low`** on the CLI. Claims picked out of a
  multi-claim input inherit the depth of the submission that offered them —
  the server carries it through `/select`, so the flag is sent once.
- **`openapi.json` refreshed**, which also catches up on server changes that
  were never re-snapshotted after 2.8.0: `failure_class` / `retryable` on the
  status schema, the 502/503 error rows, and a `/extract` 200 response schema
  where the vendored spec previously had none. No SDK behaviour depends on it —
  the file is documentation and generator input.

### Changed

- The per-capability blocks (`usage.verify` / `ask` / `assess`) are now
  **projections** of the one balance into each capability's unit, not separate
  allowances. Every field keeps its name and meaning, and `quota_used +
  quota_remaining == quota_total` still holds, so code reading
  `usage.verify.remaining` needs no change — but spending on any capability
  now moves all of them.
- Minimum `pydantic` is now **2.7** (was 2.0), for `Field(deprecated=...)`.

### Deprecated

- **The per-capability blocks** `usage.verify` / `ask` / `assess`, and
  **`UsageCapacity.credits`**, are removed together on **2026-11-29** — one
  date, one release, rather than two breaking changes months apart.
  - The blocks are two floor divisions of `credits` by `costs`:
    `remaining = credits.remaining // costs[capability]`. Two capabilities at
    the same price emit identical objects (`ask` and `assess` are both 1
    credit), because there is one balance behind all of them.
  - `UsageCapacity.credits` is an alias of `bonus`. It never meant the pool:
    before the pool existed it meant that capability's one-off top-up balance,
    which is exactly what `bonus` reports. Reading it emits a
    `DeprecationWarning`; it stays in `model_dump()` output, unwarned, for as
    long as the server sends it. The two mirror each other in both directions,
    so the SDK reads correctly against a server on either side of the change.

## [2.8.0] - 2026-08-21

The server now says WHY a verification failed and states honest waits when it
is overloaded; the SDK types both.

### Added
- **`failure_class` + `retryable` on failed verifications.** `TaskStatus`, the
  `VerificationFailed` webhook event, and `LenzPipelineError` all carry
  `failure_class` (closed set: `upstream_unavailable` | `insufficient_evidence`
  | `invalid_input` | `cancelled` | `internal`) and `retryable` (true iff
  `upstream_unavailable` — resubmitting the same claim is the right move).
  Older servers omit both; the fields default rather than break. The closed
  set is also exported as the `FailureClass` `Literal` alias
  (`from lenz_io import FailureClass`) for exhaustive matching — the model
  field itself stays `str` so a class the server adds later can't turn into a
  `ValidationError`.
- **`LenzUpstreamUnavailableError`** — a `LenzAPIError` subclass for 503s with
  `code` `upstream_unavailable` (model/search providers exhausted; the request
  was not charged) or `capacity` (submission shed at the door; nothing was
  accepted). Carries `retry_after`.

### Changed
- **A 503 that Lenz itself typed — body `code` `upstream_unavailable` or
  `capacity` — and that asks for more than 60s now raises immediately**, as
  `LenzUpstreamUnavailableError` carrying the true `retry_after`, instead of
  silently burning the 1s/2s/4s backoff ladder against a server that asked
  for 90-120s. That is the same rule 429 has always had. The decision is
  gated on the body code, not on the status number:
  - typed 503, stated wait ≤ 60s → still slept through and retried (unchanged);
  - **untyped 503** — an ordinary proxy / load-balancer / maintenance
    response with no Lenz `code` — → **backoff ladder, exactly as before**,
    however long a `Retry-After` it states;
  - every other 5xx → backoff ladder, unchanged.

  If you relied on long-stated-wait typed 503s being retried blindly, catch
  `LenzUpstreamUnavailableError` (existing `except LenzAPIError` handlers
  keep catching it).
- The stated wait is now also read from the 503 body's `retry_after` key
  (previously only the `Retry-After` header and the 429 body's
  `reset_in_seconds`), so a proxy that strips headers can't demote an honest
  wait to blind backoff.

### Fixed
- `LenzPipelineError.retryable` now coerces a non-boolean server value to
  `None` instead of passing it through (parity with the Node SDK).
- Contract fixtures refreshed to the live failed-status body; added the
  `verification.failed` webhook payload and both 503 envelopes (shared
  byte-identically with the Node SDK, as ever).

## [2.7.1] - 2026-08-15

### Fixed

- **`library.list` no longer documents the `popular` sort.** The server
  retired the view counter and its popularity sort (Lenz #273) and silently
  coerces `sort=popular` to `recent`, so the docstring advertised a dead
  option. `sort` stays a plain `str`, so nothing breaks — a caller that still
  sends `"popular"` keeps getting `recent` ordering from the server.
- Refreshed the `openapi.json` snapshot (doc-only server drift: /extract
  enumeration semantics, /verify body-keyed idempotency, the errors table).

## [2.7.0] - 2026-08-10

Quota errors are now a first-class, typed condition instead of an
authorization failure.

### Changed
- **Out-of-credits raises `LenzQuotaExceededError`, not `LenzAuthError`.** The
  API moved these rejections from HTTP 403 to **402**; 402 already mapped to
  `LenzQuotaExceededError` in this SDK, so the class you catch changes the
  moment the server ships. Previously a developer who ran out of credits was
  told *"This key doesn't have access to that resource"* and pointed at
  `/docs/auth`.

  **Breaking-ish:** `LenzQuotaExceededError` does not inherit from
  `LenzAuthError`. If you were catching the auth error to handle an empty
  balance, catch the quota error instead.

- **`Retry-After` is clamped at 60s** (`MAX_RETRY_AFTER_SLEEP`, now exported).
  The `/extract` daily cap sends seconds-until-UTC-midnight, so the old
  behavior could block a call for most of a day — three times over, once per
  retry. Past the clamp the two retryable statuses now differ:

  - **429** raises immediately with the true `retry_after`. Schedule the work;
    don't sit in it.
  - **5xx** falls back to the normal backoff ladder and keeps retrying — the
    server is down, not throttling you, and a maintenance-window
    `Retry-After: 3600` shouldn't become an hour-long sleep *or* abort a call
    that backoff might still satisfy.

  Note the clamp bounds a single sleep, not the call: a 429 stating 60s can
  still sleep 60s on each of `max_retries` attempts.

- **`LenzRateLimitError.retry_after` now reads `reset_in_seconds`** from the
  body when the `Retry-After` header is absent. The previously-read
  `retry_after` body key was an SDK invention the server has never sent.

- **Two server `code` values were retired** (server-side change, affects every
  SDK version): `insufficient_credits` and `no_chat_credits` are now plain
  `no_credits`. Both named the endpoint you called rather than what went
  wrong. **A branch on either string stops matching silently** — read
  `remaining` instead.

### Added
- **`LenzError.code`** — the server's machine-readable error code, on the base
  class so 402, 403 and 429 all carry it. `""` when the server sent none.
- **`LenzQuotaExceededError.upgrade_url`** — where the wall lifts. No rejection
  used to carry a URL at all.
- **`LenzQuotaExceededError.remaining` / `.resets_at` / `.requested`.**
  `remaining` is **nullable**: `None` means the server didn't report a balance,
  `0` means it reported an empty one. The server omits these rather than
  sending `null`, so the distinction survives the wire.
- **`LenzRateLimitError.limit` / `.reset_in_seconds` / `.upgrade_url`.** The
  server sends `upgrade_url` on 429 as well as 402 — someone hitting the daily
  `/extract` cap also wants to know a paid plan raises it.
- **`MAX_RETRY_AFTER_SLEEP`** is exported from the package root.

### Deprecated
- **`LenzQuotaExceededError.credits_remaining`** — use `remaining`. The old
  attribute was zero-defaulted and the server never sent the field it read, so
  it was always `0`. It is now a property that reads and writes through to
  `remaining` (still assignable, so constructor kwargs and fixtures keep
  working) and emits a `DeprecationWarning`. Removed in 3.0.

### Fixed
- **CLI `--json` reports `"no_credits"`, not `"unauthorized"`,** for an
  out-of-credits run, and `friendly_text` no longer tells someone with a
  working key to run `lenz login`. The payload gains `upgrade_url`.

## [2.6.0] - 2026-08-05

### Added
- **`key_finding` on verdict payloads.** `Verification` and
  `VerificationListItem` gain `key_finding: str` — one declarative sentence
  stating the most important fact the analysis established (e.g. *"Water
  boils at 100°C at standard atmospheric pressure."*), written by the
  verification pipeline's conclusion step. Empty string on legacy claims that
  pre-date the field. The CLI now leads verdict output with it.

## [2.5.0] - 2026-07-23

### Changed
- **`verifications.related()` is now keyless.** The server opened the endpoint
  to anonymous callers (same optional-Bearer model as `verifications.get`);
  the SDK's client-side auth guard is dropped accordingly. A configured key is
  still sent, so owners keep seeing related lists for their own verifications.

### Added
- **Claim visibility on submit.** `verify` / `verify_and_wait` / `verify_batch` /
  `verify_batch_and_wait` accept `visibility="private"` (default, owner-only) or
  `visibility="unlisted"` (readable by `verification_id` and at the `/c/` URL, but
  never listed in the Library or search). Batch items may set per-item `visibility`
  to override the batch-wide default. Omitted → private (byte-identical bodies for
  existing callers).
- **`Verification.visibility`** returns — `"private"` | `"unlisted"` | `"public"`,
  echoing the claim's visibility so callers can read back what they set (`"public"`
  is read-only, for genuinely listed claims).
- **`library.list` filters.** `curated` — restrict to one or more named curated
  collections (e.g. `["trivia"]`); `verdict` — comma-separated verdict labels
  (e.g. `"True,False"`); and `sort="random"` for a shuffled page.
- **`usage()` now reports `has_webhook_secret`** — whether a webhook signing
  secret is configured for the API key.

## [2.3.0] — 2026-07-06

### Changed
- **Docs corrected to match the API.** The Lenz Score range is 1–10 (was
  documented 0–10); the full pipeline is 8 models across 5 stages (was
  "7-model"); public stage names are Framing → Research → Debate →
  Panel Review → Conclusion (README, docstrings, examples, CLI labels).
  No runtime or API changes — documentation and one CLI display string.

## [2.2.0] — 2026-06-29

### Changed
- **Verdict scale is now 5-point.** `verdict` values are
  `"True" | "Mostly True" | "Mixed" | "Mostly False" | "False" | "Error"`
  (was 4-point with `"Misleading"`). `verdict` remains a plain `str` for
  forward compatibility — no type changes — but consumers branching on the
  literal `"Misleading"` should map it to `"Mixed"` / `"Mostly False"`. The
  `lenz` CLI colors `Mostly False` distinctly.

## [2.1.0] — 2026-06-26

### Added
- **`lenz status` and `lenz show` commands.** `status <task_id>` reports where a
  submitted verification stands; `show <task_id|claim>` prints the resolved
  result. Together with `--resume`/`--detach` they make the verify lifecycle
  fully scriptable.
- **Candidate readings for ambiguous `assess`.** When framing can't pin a vague
  input to a single claim, `assess` now surfaces the specific candidate readings
  it identified so you can pick one, instead of failing with a bare error.
- `lenz usage` humanizes the quota-reset timestamp instead of printing a raw
  ISO string.

### Changed
- `extract` output no longer prints the domain/entity tags line, keeping the
  default view focused on the extracted claims.

### Fixed
- **Bearer scoping on optional-auth endpoints.** The API key is now sent on
  optional-auth reads when one is configured — `verifications.get` opts in so
  the owning caller can retrieve their own private/hidden claims, while purely
  public reads (`library.list`) stay anonymous.
- `--detach` (and `--claim`) are now honored when resuming a paused task,
  including the clarification resume branch.
- `extract` reads the primary claim from `claim` rather than `atomic_claim`.

## [2.0.0] — 2026-06-25

### Added
- **`lenz` command-line tool**, shipped inside this package behind the `cli`
  extra (`pip install "lenz-io[cli]"`). Wraps the four primitives —
  `extract` / `assess` / `verify` / `ask` — plus `login` and `config`.
  First-class `--json` output (auto-enabled off a TTY) with a stable
  `{"error": {...}}` failure contract for scripting and downstream tools.
  `verify` handles the full status lifecycle (multi-claim / clarification /
  duplicate prompts) and prints a `--resume <task_id>` handle on Ctrl-C.
  Sends a distinct `User-Agent: lenz-cli/<version>`. A bare
  `pip install lenz-io` keeps the SDK lean; running `lenz` without the extra
  prints an install nudge instead of a traceback.
- **`/me/usage` is now per-capability.** `client.usage()` returns `plan`,
  `quota_resets_at`, and a `verify` / `ask` / `assess` / `extract` block instead
  of the old flat `credits_used` / `credits_total`. Each quota-backed capability
  (`UsageCapacity`) separates the recurring monthly `quota_*` from one-off
  top-up `credits`, with `remaining = quota_remaining + credits`. `assess` is
  quota-only (`credits` always 0). New models: `Usage`, `UsageCapacity`,
  `UsageExtract`. The `lenz usage` CLI now prints a row per capability
  (Verify / Ask / Assess / Extract).

### Changed
- **`client.select()` resolves a multi-claim interrupt by selecting one or more
  claims.** It now takes `texts=[...]` (was a single `text=` / dead
  `claim_index=`) and returns a `BatchAccepted` — each selected claim fans out
  into its own pipeline, so poll each `items[].task_id`. Every text must match a
  claim offered in the prior status (server-validated). On a rare mid-fan-out
  enqueue failure the server returns the partial set plus `partial: true` (still
  HTTP 202); `partial` is not a typed field on `BatchAccepted` but is reachable
  via the lax/extra-field path (`batch.partial`).

## [1.2.0] — 2026-06-07

Polling ergonomics. The async path (`verify()` → poll) is now first-class and
discoverable, not just a webhook fallback. Parallel verification (unlocked by the
server dropping its per-user single-flight lock) gets a dedicated batch-and-wait
helper.

### Added
- `client.wait(task)` → `Verification`. Blocks on an already-submitted task until
  it terminates. Accepts a `task_id` string **or** a `TaskAccepted`, so
  `client.wait(client.verify(claim=...))` reads naturally. `verify_and_wait` is now
  `wait(verify(...))` internally (behavior unchanged).
- `client.verify_batch_and_wait(claims=[...])` → `list[BatchItemResult]`. Fans out a
  batch and polls every item to completion, returning one result per claim in input
  order. Never raises on a per-item outcome — inspect each `BatchItemResult.status`
  (`completed` | `needs_input` | `failed` | `timeout`).
- `BatchItemResult` model (`task_id`, `claim_text`, `status`, `verification`,
  `status_detail`).
- `TaskStatus.error` — the server's failed-status responses carry the diagnostic
  under `error`; it's now a typed field.

### Fixed
- Failed verifications now surface the real diagnostic. The server sends
  `{"status": "failed", "error": "..."}`, but the SDK only read
  `failure_reason`/`failure_detail`, so `LenzPipelineError` reported "unknown". The
  failed path now reads `error or failure_detail or failure_reason`.

## [1.1.0] — 2026-05-28

API privacy redesign. The server now treats every API claim as private
by default and never leaks another customer's verification_id back on
a cache-hit. SDK changes align the typed surface with the new server
contract.

### Removed
- `Verification.url`, `Verification.visibility` — API claims are
  private and referenced by `verification_id` only. Cache-hit on
  someone else's claim is transparent: the customer always sees their
  own `verification_id`.
- `VerificationListItem.url`, `VerificationListItem.visibility` —
  same reasoning at the list-item layer.
- `client.verifications.set_visibility(...)` method — the underlying
  endpoint is gone. Accessing the attribute raises `AttributeError`.
- `visibility` kwarg from `verify`, `verify_batch`, `verify_and_wait`
  — server rejects it as unknown.

### Migration
If you were reading `verification.url`, the URL is no longer part of
the API surface. If you need to link to a verification, use the
`verification_id` directly (e.g. construct your own deep-link in your
app, or fetch and render the verdict in-app). `verification.visibility`
was always `'private'` for any API-created claim — the field had zero
information value and is now removed.

If you were calling `client.verifications.set_visibility(...)`,
remove those calls. API claims are private; there's no public-facing
surface to flip to.

## [1.0.2] — 2026-05-27

### Fixed
- `AskReply` contract now matches the server. Pre-1.0.2 the model declared
  a single `reply: str` field that **never matched the wire** — the server
  always returned `{role, content, created_at}`, and the SDK's `_Lax`
  base swallowed those as extras. Reading `reply.content` worked at
  runtime via attribute fall-through; reading `reply.reply` silently
  returned `""`. 1.0.2 makes the typed surface match reality:
  `AskReply.role`, `AskReply.content`, `AskReply.created_at`.

### Migration
If your code uses `.reply`, switch to `.content` — it's the same data
that was already coming over the wire, just now properly typed. Any
1.0.x code reading `.reply` was always getting an empty string anyway,
so functional impact is limited to "code that errored silently now
errors loudly at type-check time."

## [1.0.1] — 2026-05-27

### Fixed
- `VerifyBatchItem` now importable from the top-level `lenz_io` package
  (`from lenz_io import VerifyBatchItem`). In 1.0.0 it was reachable
  only via the submodule path `from lenz_io.client import VerifyBatchItem`
  because it was missing from `lenz_io.__init__.__all__`. The type was
  always present in the wheel — this is purely a re-export gap.
  Regression test added so future drift fails CI.

## [1.0.0] — 2026-05-27

First stable release. The pre-1.0 RC series (`1.0.0rc1` … `1.0.0rc11`) is now
considered superseded; consumers should upgrade. No breaking changes vs the
final RC — see entries below for the multi-language additions that landed in
this cut.

### Added
- **Multi-language API support** (12 languages). Optional `language=` kwarg on
  `verify`, `verify_and_wait`, `verify_batch`, `assess`, `extract`, and
  `ask.send`. Supported codes: `en` (default), `es`, `de`, `fr`, `it`, `pt`,
  `nl`, `sv`, `da`, `no`, `fi`, `bg`. Verdict / domain / status enum values
  stay English regardless of language; only free-form prose follows the request.
- `VerifyBatchItem` TypedDict — IDE-only type hint for per-item shapes on
  `verify_batch`; runtime still accepts plain dicts (no Pydantic coercion).
- `language: str` field on `Verification`, `VerificationListItem`,
  `LibraryItem`, and `AssessClaim` response models. Defaults to `'en'` for
  resilience against older payloads that omit the field.
- `client.assess(text=...)` — new sync verb that returns a fast 3-model
  panel verdict in ~5-10s. Mirrors the new `POST /api/v1/assess` server
  endpoint.
- `AssessClaim` and `AssessResponse` types for the assess response shape.
- `AskMessage` model (`role`, `content`, `created_at`) — `AskHistory.messages`
  is now a typed `list[AskMessage]` instead of `list[dict]`.
- `confidence` (categorical: `"high"` | `"medium"` | `"low"`) at the top
  level of every claim-shaped response. Replaces the numeric
  `verdict.confidence` (0–1) — the numeric form is no longer in the
  public API; the SDK exposes only the categorical label.
- `lenz_score` (integer 0–10) flattened to the top level (was nested
  under `verdict.score` as a float). The DB column is now
  `IntegerField`; the API/SDK type narrows from `float | None` to
  `int | None`. The conclusion-step LLM already constrained the score
  to integers — only the storage and surface types lagged.
- Contract test (`tests/test_contract.py`) — re-validates 6 frozen
  server-response fixtures under `extra="forbid"` so silent rename
  misses fail CI.

### Changed (breaking)
- `client.followup.*` → `client.ask.*`; URL paths
  `/verifications/{id}/follow-up` → `/ask/{id}`.
- `FollowupHistory` → `AskHistory`, `FollowupReply` → `AskReply`.
- `Verdict` block flattened — was `verification.verdict.label/.score/.confidence`,
  now `verification.verdict` (string), `verification.confidence`
  (categorical), `verification.lenz_score`.
- `ExtractedClaims.atomic_claim` → `ExtractedClaims.claim`.
- `SimilarVerification.verdict_label` → `verdict`; `score` → `lenz_score`;
  added `confidence`.
- `TaskStatus.candidate_claims` → `candidates`.
- `client.library.get(id)` removed — use `client.verifications.get(id)`,
  which now accepts anon callers and returns the same `Verification`
  shape for any non-hidden public claim.

### Removed
- `Verdict` class (no consumers after the flatten).
- `published_at` on `Verification` / `VerificationListItem` /
  `LibraryItem`. Use `created_at` + `modified_at` instead.
- `FollowupHistory`, `FollowupReply`, `Verdict` exports.
- `Source.stance` — the per-source SUPPORT/REFUTE/NEUTRAL label is gone
  from the server response. Research is now purely evidence-gathering;
  adjudication owns the verdict. See
  `lenzhq/lenz@b9419e50` for the server-side change.

## [1.0.0rc1] — 2026-05-13

First public release candidate. Targets Lenz Public API v1
(`X-Lenz-API-Version: 2026-05-13`).

### Added

- `Lenz` client with marquee top-level methods (`verify`, `verify_and_wait`,
  `verify_batch`, `extract`, `select`, `get_status`, `usage`) and resource
  namespaces (`verifications`, `followup`, `library`).
- `verify_and_wait()` — submit + poll with exponential backoff
  (2s/4s/8s cap 10s), auto-idempotency by default, 120s default timeout.
- Typed exception hierarchy with `cause` + `fix` + `doc_url` + `request_id`
  on every error; HTTP status → exception mapping is single-source and
  mirrored in the TS SDK.
- `LenzWebhooks` stateful handler — HMAC-SHA256 signature verification,
  5-minute replay window, typed event union
  (`VerificationCompleted` / `VerificationFailed` / `VerificationNeedsInput`).
- Auto-retry on 5xx and 429 with `Retry-After` honored.
- `X-Lenz-API-Version` pinned at SDK release date; persistent `httpx.Client`
  with HTTP keep-alive for connection reuse.
- `LENZ_API_KEY` and `LENZ_BASE_URL` environment variables.
- Examples: `examples/core/{quickstart,verify_llm_output,fastapi_webhook}.py`.
- 67 unit tests covering construction, verb dispatch, namespaces,
  `verify_and_wait` state machine, idempotency, auto-retry, webhook
  parsing, error mapping. Mocked end-to-end via `respx`.
