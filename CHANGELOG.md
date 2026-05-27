# Changelog

All notable changes to this SDK are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[SemVer](https://semver.org/).

## [Unreleased]

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
