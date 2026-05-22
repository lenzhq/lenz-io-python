# lenz-io

Official Python SDK for the [Lenz Claim Verification API for AI Product Teams](https://lenz.io/developers).

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
(3 models: sources, logic, context) → Conclude. ~90 seconds wall-clock
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
- **`client.verify(...)`** → `TaskAccepted`. Async submit; returns a `task_id`. Pair with a webhook for the callback.
- **`client.verify_and_wait(...)`** → `Verification`. Submit + poll until the pipeline lands (sync ergonomic).
- **`client.verify_batch(claims=[...])`** → `BatchAccepted`. Fan-out for multi-claim LLM outputs.
- **`client.ask.{history,send,reset}(verification_id, ...)`** → Q&A on a verification.
- **`client.verifications.{list,get,delete,set_visibility,related}(...)`** → manage past verifications. `get` accepts anon callers and returns any non-hidden public claim.
- **`client.library.list(...)`** → browse the public catalog (no API key needed).
- **`client.usage()`** → credits and rate-limit remaining.

## Response shape — the unified vocabulary

Every claim-shaped response shares these fields at top level:

| Field | Type | Notes |
|-------|------|-------|
| `claim` | `str` | The framed claim text. |
| `verdict` | `str` | `"True"` \| `"Mostly True"` \| `"Misleading"` \| `"False"` \| `"Error"`. |
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

# Later (different process / restart):
status = client.get_status("tsk_abc123")
if status.status == "completed":
    print(status.result.verdict, status.result.lenz_score)
```

## Idempotency

`verify_and_wait` sends an auto-generated `Idempotency-Key` on every call by
default, so a network drop after submit doesn't spawn a duplicate verification
or charge a second credit. Override with `idempotency_key="..."` to pin a
specific key, or `idempotency=False` to opt out.

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

## Bug reports + feature requests

[github.com/lenzhq/lenz-io-python/issues](https://github.com/lenzhq/lenz-io-python/issues)

For commercial use, volume pricing, or onboarding support,
[get in touch](https://lenz.io/contact).

## License

MIT. See [LICENSE](LICENSE).
