# lenz-io

Official Python SDK for the [Lenz Hallucination Verification API](https://lenz.io).

The fact-check API for your LLM features. Drop in `/verify` after your model
generates an answer; get back a verdict with sources you can show your users
and audit later.

```bash
pip install lenz-io
```

## Quickstart

```python
from lenz_io import Lenz

client = Lenz(api_key="lenz_...")

v = client.verify_and_wait(claim="Sharks don't get cancer")
print(v.verdict.label, v.verdict.score)
# false 2.0

for source in v.sources[:3]:
    print(" -", source.title, source.url)
```

The quickstart claim is pre-cached so this returns in ~1.5s. Your own claims
hit the full pipeline (~60-90s) — use webhooks for production async flows.

> **Get your webhook secret here →** [lenz.io/api-integration](https://lenz.io/api-integration)

## What you get

- **`client.verify_and_wait(...)`** — submit + poll until the pipeline lands. Returns a typed `Verification`.
- **`client.verify(...)`** — async submit; returns a `task_id`. Use webhooks for the callback.
- **`client.extract(text=...)`** — pull verifiable claims out of any text (free, capped at 1000/key/day).
- **`client.verify_batch(claims=[...])`** — fan-out for multi-claim LLM outputs.
- **`client.verifications.{list,get,delete,set_visibility}(...)`** — manage past verifications.
- **`client.followup.{history,send,reset}(verification_id)`** — Q&A on a verification.
- **`client.library.{list,get}(...)`** — browse the public catalog (no API key needed).
- **`client.usage()`** — credits and rate-limit remaining.

### Webhooks

```python
from lenz_io import LenzWebhooks, VerificationCompleted, VerificationNeedsInput

webhooks = LenzWebhooks(secret="whsec_...")

# In your web handler:
event = webhooks.parse(raw_body=request.body, headers=request.headers)
if isinstance(event, VerificationCompleted):
    vid, result = event.verification_id, event.result
    ...
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
for the headline extract-and-verify integration pattern.

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
    print(status.result.verdict.label)
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
