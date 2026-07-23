# Changelog

All notable changes to this SDK are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[SemVer](https://semver.org/).

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
