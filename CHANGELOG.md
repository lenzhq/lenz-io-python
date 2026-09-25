# Changelog

All notable changes to this SDK are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[SemVer](https://semver.org/).

## [Unreleased]

A new optional field on every full verification, `suggested_revision`
(below). Nothing the SDK sends changes, and 2.16.0 keeps working against the
current API.

### Added

- **`suggested_revision` on `Verification`**, a string or `None`: a suggested
  rewrite of the verification's `claim` that its findings support, to use in
  place of the original sentence. It has not been verified itself: before
  using it, review it or run it through `client.verify(...)`. It is
  `None` for a true claim, when no correction is established, and on
  verifications that predate the field; an older server that does not send
  the key also reads `None`. It is on `verifications.get`, `verify_and_wait`,
  `wait` and a completed `get_status`, and the same key arrives in the
  `result` dict of a `verification.completed` webhook. List items, `assess`
  rows and the library do not carry it.
- **The CLI prints it.** `lenz verify`, `lenz show` and the batch view print
  one line under the key finding, `Suggested rewrite: …`, when a verification
  carries one; `--json` output includes the field.
- **The `openapi.json` snapshot is refreshed.** Additive only:
  `suggested_revision` on the verification detail, and its line in the API
  description.

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
