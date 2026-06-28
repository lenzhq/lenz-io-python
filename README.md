# lenz-io

Official Python SDK for the [Lenz Fact Checking API for AI Product Teams](https://lenz.io/developers).

**Four API primitives, one research-depth ladder.**

- `extract` — pull verifiable claims out of any text. Free, 1000 calls/key/day.
- `assess` — fast 3-model panel verdict in ~5-10s. Sync, paid.
- `verify` — full 7-model pipeline with citations in ~90s. Async, paid.
- `ask` — follow-up questions grounded on a verification.

Built for teams whose AI output is async or document-shaped: legal-memo
generators, deep-research products, due-diligence platforms, vertical
agents producing structured deliverables. Not chat AI, not voice AI,
not real-time copilots — pipeline runs are the wrong shape for those.

```bash
pip install lenz-io
```

## Command-line tool

The same primitives from your terminal — submit, poll, and read full reports.
Ships inside this package behind the `cli` extra (quotes matter — bare brackets
are a glob in zsh):

```bash
pipx install "lenz-io[cli]"      # isolated CLI install (recommended)
pip install "lenz-io[cli]"       # or into your current environment
```

```bash
lenz login                       # paste an API key (free — get one at lenz.io/api-integration)
lenz extract "Einstein won the 1921 Nobel for relativity"   # free, 1000/day
lenz assess  "The Great Wall is visible from space"          # fast verdict
lenz verify  "Water boils at 90C at sea level"               # full pipeline (~90s)
lenz verify  "<claim>" --json | jq .verdict                 # machine-readable
lenz status  <task_id>           # non-blocking: poll a verify task's progress
lenz show    <verification_id>   # full report — sources, warnings, panel + debate (-c for concise)
lenz ask <verification_id> "Which source is strongest?"
lenz usage                       # plan, remaining quota, and when it resets
lenz config                      # show which key/base URL is in use
```

Every command takes `--json` for a clean machine-readable object (also emitted
automatically when stdout is not a TTY, so pipes Just Work). Errors in `--json`
mode are `{"error": {"code", "message", "status"}}` on stdout with a nonzero
exit. `verify` blocks with a progress spinner; Ctrl-C prints a
`lenz verify --resume <task_id>` handle so a long run isn't lost. Key resolution
order is `--api-key` flag → `LENZ_API_KEY` → `~/.config/lenz/config.json`.

**Scripting the lifecycle (no blocking).** `verify --detach` returns a
`task_id` immediately; poll it with `status` and read the full report with
`show` once it completes:

```bash
tid=$(lenz verify "<claim>" --detach --json | jq -r .task_id)
lenz status "$tid" --json | jq -r .status          # processing → completed
lenz show <verification_id> --json                 # full report once done
```

If the input holds several claims, `status` reports `needs_input` and lists
them; resolve it non-interactively by index (spawns one verification per pick):

```bash
lenz verify --resume "$tid" --claim 1,3 --detach --json   # → spawned task_ids
```

## Quickstart — the canonical integration

```python
from lenz_io import Lenz

client = Lenz(api_key="lenz_...")

# 1. extract — pull verifiable claims out of any text (free)
out = client.extract(text=llm_output)

# 2. assess — fast 3-model verdict on each (~5-10s, sync)
quick = client.assess(text=llm_output)
for c in quick.claims:
    print(c.verdict, c.confidence, c.claim)

# 3. verify — escalate low-confidence claims to the full panel + citations
for c in quick.claims:
    if c.confidence == "low":
        v = client.verify_and_wait(claim=c.claim)
        print(v.verdict, v.lenz_score, v.executive_summary)

# 4. ask — follow-up grounded on a verification
reply = client.ask.send(v.verification_id, message="Which source is strongest?")
print(reply.reply)
```

`assess` and `verify` share a result cache server-side: if a claim
already has a deep verification, `assess` returns it via
`verification_url` and you can skip the escalation.

## How verification works

Frame → Collect Evidence → Debate (2 models, 2 rounds) → Adjudicate
(3 models: sources, logic, precision) → Conclude. ~90 seconds wall-clock
per claim. `assess` runs a leaner 3-model panel against the same
framing for the ~5-10s pass.

## Magical-moment demo

```python
from lenz_io import Lenz

client = Lenz(api_key="lenz_...")

v = client.verify_and_wait(claim="Sharks don't get cancer")
print(v.verdict, v.lenz_score)
# False 2.0

for source in v.sources[:3]:
    print(" -", source.title, source.url)
```

The demo claim is pre-cached so this returns in ~1.5s. Your own claims
hit the full pipeline (~60-90s) — use webhooks for production async flows.

> **Get your webhook secret here →** [lenz.io/api-integration](https://lenz.io/api-integration)

## What you get on the client

- **`client.extract(text=...)`** → `ExtractedClaims`. Free, capped at 1000/key/day.
- **`client.assess(text=...)`** → `AssessResponse`. Sync, ~5-10s, returns one entry per identified claim.
- **`client.verify(...)`** → `TaskAccepted`. Async submit; returns a `task_id`. Get the result by polling (`client.wait(...)` / `client.get_status(...)`) or via a webhook.
- **`client.verify_and_wait(...)`** → `Verification`. Submit + poll until the pipeline lands (sync ergonomic). Equivalent to `wait(verify(...))`.
- **`client.wait(task)`** → `Verification`. Block on a `task_id` (or a `TaskAccepted`) until it terminates. The polling counterpart to a webhook.
- **`client.verify_batch(claims=[...])`** → `BatchAccepted`. Fan-out for multi-claim LLM outputs.
- **`client.verify_batch_and_wait(claims=[...])`** → `list[BatchItemResult]`. Fan out a batch and poll every item to completion; one result per claim, in input order, never raises on a per-item failure.
- **`client.ask.{history,send,reset}(verification_id, ...)`** → Q&A on a verification. `reply.content` uses a small markdown subset (`**bold**`, `*italic*`, `- ` or `* ` bullets, blank-line paragraphs) — render with a minimal markdown library or display verbatim. See [docs/quickstart#ask-reply-format](https://lenz.io/docs/quickstart#ask-reply-format).
- **`client.verifications.{list,get,delete,related}(...)`** → manage past verifications. All API claims are private; reference them by `verification_id`. Cache-hit on another customer's claim is transparent — you always see your own `verification_id`, never another customer's.
- **`client.library.list(...)`** → browse the public catalog (no API key needed).
- **`client.usage()`** → remaining capacity per capability (`verify` / `ask` / `assess` quota + top-up credits, and the daily `extract` rate limit).

## Polling without webhooks

`verify()` returns immediately with a `task_id`; the pipeline runs async (~60-90s
for a cold claim). You don't need webhooks to get the result — poll for it.

The one-liner is `verify_and_wait()`. If you already hold a `task_id` (or want to
submit and wait separately), use `wait()`:

```python
task = client.verify(claim="Sharks don't get cancer")   # async, returns a task_id
verification = client.wait(task)                          # blocks until it lands
print(verification.verdict, verification.lenz_score)
```

To run several claims in parallel, submit a batch and wait on all of them.
`verify_batch_and_wait` returns one `BatchItemResult` per claim, in input order,
and never raises on a single claim failing — inspect each item's `status`:

```python
results = client.verify_batch_and_wait(claims=[
    {"text": "Sharks don't get cancer"},
    {"text": "The Eiffel Tower is 330m tall"},
])
for r in results:
    if r.status == "completed":
        print(r.claim_text, "→", r.verification.verdict)
    else:
        print(r.claim_text, "→", r.status)   # needs_input | failed | timeout
```

Prefer **webhooks** for production async flows (no long-lived HTTP connection);
prefer **polling** for scripts, notebooks, and request/response handlers where
blocking is fine. If you want full control over the loop, call `get_status(task_id)`
yourself — it's a single non-blocking poll.

## Response shape — the unified vocabulary

Every claim-shaped response shares these fields at top level:

| Field | Type | Notes |
|-------|------|-------|
| `claim` | `str` | The framed claim text. |
| `verdict` | `str` | `"True"` \| `"Mostly True"` \| `"Mixed"` \| `"Mostly False"` \| `"False"` \| `"Error"`. |
| `confidence` | `str` | Categorical: `"high"` \| `"medium"` \| `"low"`. |
| `lenz_score` | `int \| None` | Integer 0–10 (deep verdicts and list endpoints; `assess` omits it). |

### Webhooks

```python
from lenz_io import LenzWebhooks, VerificationCompleted, VerificationNeedsInput

webhooks = LenzWebhooks(secret="whsec_...")

# In your web handler:
event = webhooks.parse(raw_body=request.body, headers=request.headers)
if isinstance(event, VerificationCompleted):
    vid, result = event.verification_id, event.result
    # result["verdict"], result["lenz_score"], result["confidence"], ...
elif isinstance(event, VerificationNeedsInput):
    tid, ni = event.task_id, event.needs_input
    ...
```

If you're on Python 3.10+ a `match` statement reads even cleaner — events are
plain dataclasses, so structural pattern matching works.

Signature verification is HMAC-SHA256 over the raw body; the SDK does it for
you and rejects tampered or replayed payloads.

See [`examples/core/fastapi_webhook.py`](examples/core/fastapi_webhook.py)
for a runnable FastAPI receiver, and [`examples/core/verify_llm_output.py`](examples/core/verify_llm_output.py)
for the headline assess-then-escalate pattern.

## Errors

Every error subclass is typed and carries a `request_id` you can quote on
support tickets:

```python
from lenz_io import LenzAuthError, LenzRateLimitError, LenzValidationError

try:
    client.verify_and_wait(claim="...")
except LenzAuthError as exc:
    print(exc)
    # Unauthorized
    #   Cause:  Invalid api key
    #   Fix:    Generate a new key at https://lenz.io/api-integration.
    #   Docs:   https://lenz.io/docs/auth
    #   Request ID: req_abc123
except LenzRateLimitError as exc:
    time.sleep(exc.retry_after)
except LenzValidationError as exc:
    for field_err in exc.errors:
        print(field_err["loc"], field_err["msg"])
```

## Resuming a verification

If a `verify_and_wait` call exceeds its `timeout` (default 120s) or your
process dies mid-poll, the pipeline keeps running. The exception carries the
`task_id`:

```python
from lenz_io import LenzTimeoutError

try:
    client.verify_and_wait(claim="...", timeout=30)
except LenzTimeoutError as exc:
    print("resume later via:", exc.task_id)

# Later (different process / restart) — block on the same task_id:
verification = client.wait("tsk_abc123")
print(verification.verdict, verification.lenz_score)

# ...or do a single non-blocking poll yourself:
status = client.get_status("tsk_abc123")
if status.status == "completed":
    print(status.result.verdict, status.result.lenz_score)
```

## Idempotency

`verify_and_wait` sends an auto-generated `Idempotency-Key` on every call by
default, so a network drop after submit doesn't spawn a duplicate verification
or charge a second credit. Override with `idempotency_key="..."` to pin a
specific key, or `idempotency=False` to opt out.

## Multi-language output

The Lenz API returns prose fields (atomic claim, executive summary, debate, panel
reasoning) in any of 12 languages. Pass `language=` on `verify`, `verify_and_wait`,
`verify_batch`, `assess`, `extract`, or `ask.send`. Verdict labels stay English
regardless of language.

```python
v = client.verify_and_wait(
    claim="La Tierra es plana",
    language="es",                 # Spanish output
)
print(v.verdict, v.language)
# False es
```

Supported codes: `en` (default), `es`, `de`, `fr`, `it`, `pt`, `nl`, `sv`, `da`,
`no`, `fi`, `bg`. Per-item override on `verify_batch`:

```python
batch = client.verify_batch(
    claims=[
        {"text": "Coffee causes cancer."},                    # en (batch default)
        {"text": "El café causa cáncer.", "language": "es"},  # overrides
    ],
    language="en",
)
```

## Configuration

```python
Lenz(
    api_key="lenz_...",                  # or set LENZ_API_KEY env var
    base_url="https://lenz.io/api/v1",   # override for staging / local
    timeout=30.0,
    max_retries=3,
)
```

Environment variables:

- `LENZ_API_KEY` — read if `api_key=` is not passed
- `LENZ_BASE_URL` — read if `base_url=` is not passed

## Compatibility

- Python 3.9, 3.10, 3.11, 3.12
- Works in CI/CD (no interactive prompts, no global state)
- Mockable for tests: every HTTP call goes through `httpx`; use `respx` or
  inject your own `httpx.Client` via `Lenz(..., http_client=...)`

## Contributing

```bash
git clone https://github.com/lenzhq/lenz-io-python && cd lenz-io-python
uv sync --extra dev
git config core.hooksPath scripts/hooks   # one-time: enables pre-commit
```

The pre-commit hook mirrors CI exactly (`ruff check`, `ruff format --check`,
`mypy`, `pytest`). Runs ~10s per commit on a warm cache. Skip once with
`git commit --no-verify` when you must.

## Bug reports + feature requests

[github.com/lenzhq/lenz-io-python/issues](https://github.com/lenzhq/lenz-io-python/issues)

For commercial use, volume pricing, or onboarding support,
[get in touch](https://lenz.io/contact).

## License

MIT. See [LICENSE](LICENSE).
