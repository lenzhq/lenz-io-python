# lenz-io

Official Python SDK for the [Lenz Fact Checking API for AI Product Teams](https://lenz.io/developers).

**Four API primitives, one research-depth ladder.**

- `extract` — pull verifiable claims out of any text, optionally narrowed with a `focus`. Free, 1000 calls/account/day (shared across your API keys).
- `assess` — fast 3-model panel verdict in ~10s. Sync, paid.
- `verify` — full 8-model pipeline with citations in ~90s. Async, paid.
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
lenz login                       # paste an API key (free — get one at lenz.io/api-credentials)
lenz extract "Einstein won the 1921 Nobel for relativity"   # free, 1000/day
lenz extract "$(cat deck.txt)" --focus "market size"        # only the claims you want
lenz assess  "The Great Wall is visible from space"          # fast verdict
lenz assess  "<claim 1>" "<claim 2>" "<claim 3>"              # one call, one verdict per claim (up to 20)
lenz verify  "Water boils at 90C at sea level"               # full pipeline (~90s)
lenz verify  "<claim>" --depth low                          # shallower, faster, half the credits
lenz verify  "<claim>" --json | jq .verdict                 # machine-readable
lenz status  <task_id>           # non-blocking: poll a verify task's progress
lenz show    <verification_id>   # full report — sources, warnings, panel + debate (-c for concise)
lenz ask <verification_id> "Which source is strongest?"
lenz usage                       # credits left, what they buy, and when they reset
lenz config                      # show which key/base URL is in use
```

Every command takes `--json` for a clean machine-readable object (also emitted
automatically when stdout is not a TTY, so pipes Just Work). Errors in `--json`
mode are `{"error": {"code", "message", "status"}}` on stdout with a nonzero
exit (an out-of-credits run reports `"code": "no_credits"` and adds
`upgrade_url`). `lenz usage` leads with the balance:

```text
Lenz usage  (developer plan)
  5070 credits left  (≈ 507 verifications · 5070 assessments)
  Verify:   507 left  (13 / 520 quota + 20 bonus · 10 credits each · 5 at depth "low")
  Ask:      5070 left  (130 / 5200 quota + 200 bonus · 1 credit each)
  Assess:   5070 left  (130 / 5200 quota + 200 bonus · 1 credit each)
  Extract:  4 / 1000 today  (free — no credit charge)
  Credits reset in 3 days (Sep 1, 2026)
```

`verify` blocks with a progress spinner; Ctrl-C prints a
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
#    add focus="..." to narrow it to the claims you care about
out = client.extract(text=llm_output)
claims = out.identified_claims or [out.claim]

# 2. assess — ONE call over the extracted claims (up to 20), one row per claim, same order
quick = client.assess(claims=claims).claims
for c in quick:
    print(c.verdict, c.confidence, c.claim)

# 3. verify — escalate the low-confidence rows to the full panel + citations
doubtful = [{"claim": c.claim} for c in quick if c.verdict != "Error" and c.confidence == "low"]
results = client.verify_batch_and_wait(claims=doubtful) if doubtful else []
for r in results:
    if r.verification:
        print(r.verification.verdict, r.verification.lenz_score, r.verification.executive_summary)

# 4. ask — follow-up grounded on a verification
v = results[0].verification
reply = client.ask.send(v.verification_id, message="Which source is strongest?")
print(reply.content)
```

`assess(claims=[...])` takes up to 20 claims per call and always answers
with exactly one row per claim, in the order sent. A row that could not be
given a verdict comes back in position with `verdict == "Error"`, an
`error_code` (`no_claim` / `ambiguous` / `framing_failed` /
`upstream_unavailable` — the retryable one), `candidate_claims` when it was
ambiguous, and a one-sentence `hint` on what to send next; it is not
charged. A compound item is assessed on its main claim and lists the other
claims it found in `identified_claims` (also with a `hint`) — send those as
their own items to check the rest. `assess(claim="...")` still takes a
single statement and is unchanged.

`assess` and `verify` share a result cache server-side: if a claim
already has a deep verification, `assess` returns it via
`verification_url` and you can skip the escalation.

## How verification works

Framing → Research → Debate (2 models, 2 rounds) → Panel Review
(3 reviewers: source quality, logical structure, claim precision) → Conclusion. ~90 seconds wall-clock
per claim. `assess` runs a leaner 3-model panel against the same
framing for the ~10s pass.

## Quickstart demo

```python
from lenz_io import Lenz

client = Lenz(api_key="lenz_...")

v = client.verify_and_wait(claim="Sharks don't get cancer")
print(v.verdict, v.lenz_score)
# False 2

for source in v.sources[:3]:
    print(" -", source.title, source.url)
```

The demo claim is pre-cached so this returns in ~1.5s. Your own claims
hit the full pipeline (~60-90s) — use webhooks for production async flows.

> **Get your webhook secret here →** [lenz.io/api-credentials](https://lenz.io/api-credentials)

## What you get on the client

- **`client.extract(text=...)`** → `ExtractedClaims`. Free, capped at 1000/account/day. Add `focus=` to narrow the list — see [Steering extract](#steering-extract).
- **`client.assess(claim=...)`** / **`client.assess(claims=[...])`** → `AssessResponse`. Sync. One statement (~10s; `text=` is accepted as an alias: a document is `text`, a claim is `claim`) or a list of up to 20 claims in one call (~10-25s) — exactly one row per claim, in order; rows that got no verdict are `"Error"` rows with an `error_code` and a `hint`, in position and free. The two forms are mutually exclusive. `timeout=` overrides the client timeout for that call (a list call defaults to 45s).
- **`client.verify(...)`** → `TaskAccepted`. Async submit; returns a `task_id`. Get the result by polling (`client.wait(...)` / `client.get_status(...)`) or via a webhook.
- **`client.verify_and_wait(...)`** → `Verification`. Submit + poll until the pipeline lands (sync ergonomic). Equivalent to `wait(verify(...))`.
- **`client.wait(task)`** → `Verification`. Block on a `task_id` (or a `TaskAccepted`) until it terminates. The polling counterpart to a webhook.
- **`client.verify_batch(claims=[...])`** → `BatchAccepted`. Fan-out for multi-claim LLM outputs.
- **`client.verify_batch_and_wait(claims=[...])`** → `list[BatchItemResult]`. Fan out a batch and poll every item to completion; one result per claim, in input order, never raises on a per-item failure.
- **`client.ask.{history,send,reset}(verification_id, ...)`** → Q&A on a verification. `reply.content` uses a small markdown subset (`**bold**`, `*italic*`, `- ` or `* ` bullets, blank-line paragraphs) — render with a minimal markdown library or display verbatim. See [docs/quickstart#ask-reply-format](https://lenz.io/docs/quickstart#ask-reply-format).
- **`client.verifications.{list,get,delete,related}(...)`** → manage past verifications. All API claims are private; reference them by `verification_id`. Cache-hit on another customer's claim is transparent — you always see your own `verification_id`, never another customer's.
- **`client.library.list(...)`** → browse the public catalog (no API key needed).
- **`client.usage()`** → the account's credit balance (`usage.credits`), the price list (`usage.costs` — `verify` 10, `assess` 1, `ask` 1, `extract` 0 — plus `usage.cost_options` for parameter-dependent prices such as `depth`), and per-capability projections of that one pool (`usage.verify.remaining` is how many verifications the balance still buys), plus the daily `extract` rate limit. Also reports `has_webhook_secret` — whether this key can receive signed webhook callbacks (`verify` with a `webhook_url` needs one); the secret value itself is never exposed.

## Polling without webhooks

`verify()` returns immediately with a `task_id`; the pipeline runs async (~60-90s
for a cold claim). You don't need webhooks to get the result — poll for it.

The one-liner is `verify_and_wait()`. If you already hold a `task_id` (or want to
submit and wait separately), use `wait()`:

```python
task = client.verify(claim="Sharks don't get cancer")  # async, returns a task_id
verification = client.wait(task)  # blocks until it lands
print(verification.verdict, verification.lenz_score)
```

To run several claims in parallel, submit a batch and wait on all of them.
`verify_batch_and_wait` returns one `BatchItemResult` per claim, in input order,
and never raises on a single claim failing — inspect each item's `status`:

```python
results = client.verify_batch_and_wait(
    claims=[
        {"text": "Sharks don't get cancer"},
        {"text": "The Eiffel Tower is 330m tall"},
    ]
)
for r in results:
    if r.status == "completed":
        print(r.claim_text, "→", r.verification.verdict)
    else:
        print(r.claim_text, "→", r.status)  # needs_input | failed | timeout
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
| `lenz_score` | `int \| None` | Integer 1–10 (deep verdicts and list endpoints; `assess` omits it). |

### Webhooks

```python
from lenz_io import LenzWebhooks, VerificationCompleted, VerificationFailed, VerificationNeedsInput

webhooks = LenzWebhooks(secret="whsec_...")

# In your web handler:
event = webhooks.parse(raw_body=request.body, headers=request.headers)
if isinstance(event, VerificationCompleted):
    vid, result = event.verification_id, event.result
    # result["verdict"], result["lenz_score"], result["confidence"], ...
elif isinstance(event, VerificationNeedsInput):
    tid, ni = event.task_id, event.needs_input
    ...
elif isinstance(event, VerificationFailed):
    # event.error is WHERE the pipeline stopped; event.failure_class is WHY
    # (closed set) and event.retryable tells you what to do about it.
    if event.retryable:
        resubmit_later(event.task_id)  # transient provider outage
    else:
        log_permanent_failure(event.task_id, event.error)
```

If you're on Python 3.10+ a `match` statement reads even cleaner — events are
plain dataclasses, so structural pattern matching works.

Signature verification is HMAC-SHA256 over the raw body; the SDK does it for
you and rejects tampered or replayed payloads.

See [`examples/core/fastapi_webhook.py`](examples/core/fastapi_webhook.py)
for a runnable FastAPI receiver, and [`examples/core/verify_llm_output.py`](examples/core/verify_llm_output.py)
for the headline extract → assess → escalate pattern.

## Credits

One pool per account funds every billable call, at a fixed weight:

| Call | Credits |
|---|---|
| `verify` (and `verify_batch`, `select`) | **10** per claim |
| `verify` with `depth="low"` | **5** per claim |
| `assess` | 1 per claim; `"Error"` rows are free |
| `ask` | 1 |
| `extract` | 0 — free at the pool, bounded by the daily fair-use cap instead |

```python
u = client.usage()
print(u.credits.remaining, "credits")  # the balance — the authoritative number
print(u.costs["verify"], "credits per verification")  # the price list
print(u.cost_options["verify"]["depth"]["low"], "at depth low")  # 5 — half price
print(u.verify.remaining, "verifications left")  # a projection of that balance
print(u.credits.bonus, "of them non-expiring")  # grants + top-ups
```

The `verify` / `ask` / `assess` blocks are **projections** of the one balance
into each capability's unit — how many of those calls the remaining credits
would buy — not separate allowances. Spending on any one of them moves all of
them.

Per-capability `bonus` is that capability's share of the non-expiring bucket,
so 200 bonus credits read as `assess.bonus == 200` and `verify.bonus == 20`.
The old `capability.credits` field is a deprecated alias of `bonus` (it never
meant the pool); reading it emits a `DeprecationWarning` and it goes away on
2026-11-29.

### Depth pricing

`cost_options["verify"]["depth"]["low"]` is the price of a `depth="low"`
verification — half a standard one. `low` caps research breadth (fewer
discovery queries, a hard extraction ceiling, no recovery fetch tiers) while
every reasoning step runs the same models; it is not a model downgrade.

It is a **price, not a capability**, which is why it is nested under
`cost_options` rather than sitting in `costs` beside the four capability
names. There is deliberately no `usage.verify_low` block beside
`usage.verify` — it would report the same balance in a second unit. Divide
the balance yourself when you want the count:

```python
# Every level is optional: a server predating this field sends `{}`, and
# the capability's default price in `costs` is the right fallback.
low = u.cost_options.get("verify", {}).get("depth", {}).get("low") or u.costs["verify"]
low_depth_left = u.credits.remaining // low  # 1014
```

**You are charged for the depth you requested, not the one you were served.**
A `low` request answered from a cached `standard` verdict still costs 5. The
`depth` echoed on the completed verification is what the verdict was *produced*
with, so it can read `standard` on a `low` request — the echo describes the
evidence behind the answer, the charge follows the request. A batch may mix
depths and is billed per item.

## Errors

Every error subclass is typed and carries a `request_id` you can quote on
support tickets:

```python
from lenz_io import (
    LenzAuthError,
    LenzQuotaExceededError,
    LenzRateLimitError,
    LenzUpstreamUnavailableError,
    LenzValidationError,
)

try:
    client.verify_and_wait(claim="...")
except LenzQuotaExceededError as exc:
    # HTTP 402. Out of credits — retrying will not clear it.
    print(exc.remaining)  # 0 verifications, or None if the server didn't say
    print(exc.credit_balance)  # 4 — credits left in the pool, or None
    print(exc.cost)  # 10 — credits this call would have taken, or None
    # `cost` is depth-aware: a rejected depth="low" verify reports 5, and a
    # rejected batch mixing depths reports its real summed total. Read it
    # rather than multiplying `requested` by a price you assumed.
    print(exc.resets_at)  # "2026-09-01T00:00:00+00:00", or None
    print(exc.upgrade_url)  # https://lenz.io/plans
except LenzAuthError as exc:
    print(exc)
    # Unauthorized
    #   Cause:  Invalid api key
    #   Fix:    Generate a new key at https://lenz.io/api-credentials.
    #   Docs:   https://lenz.io/docs/auth
    #   Request ID: req_abc123
except LenzRateLimitError as exc:
    # Waits up to 60s are already retried for you, so reaching here means
    # either the ladder ran out or the wait is long. Don't sleep it — the
    # /extract daily cap can be hours away.
    schedule_retry_in(exc.retry_after)
except LenzValidationError as exc:
    for field_err in exc.errors:
        print(field_err["loc"], field_err["msg"])
except LenzUpstreamUnavailableError as exc:
    # HTTP 503, code "upstream_unavailable" (model/search providers
    # exhausted) or "capacity" (submissions shed at the door). Nothing was
    # charged. Waits up to 60s are already slept through by the automatic
    # retry ladder; reaching here means the server stated a longer one.
    schedule_retry_in(exc.retry_after)  # typically 90-120s
```

A failed *verification* (as opposed to a failed HTTP call) raises
`LenzPipelineError` from `verify_and_wait` / `wait`. Since 2.8.0 it carries
`failure_class` (closed set: `upstream_unavailable` | `insufficient_evidence`
| `invalid_input` | `cancelled` | `internal`) and `retryable` — `True` means
a transient provider-side exhaustion where resubmitting the same claim is the
right move; older servers leave it `None`.

`LenzQuotaExceededError` is a **sibling** of `LenzAuthError`, not a subclass —
"fix your key" and "top up your account" are different actions. So if you were
catching `LenzAuthError` to handle an empty balance, that branch stops firing;
add a `LenzQuotaExceededError` handler.

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

## Steering extract

`extract` returns every major factual claim it finds, ranked most-check-worthy
first. On a long document that is often more than you want to verify. Pass
`focus=` to narrow it:

```python
out = client.extract(
    text=pitch_deck,
    focus="market size, growth and competitors",
)
```

A focus can only **select** from the claims the extractor found. It cannot add
a claim, reword one, reorder them, change the output language, or change what
counts as a claim — selection runs over the claim list, not over your document,
so a claim you get back is one an unfocused call would have returned too,
verbatim.

At most 300 characters. A longer focus is rejected with a 422 rather than
truncated, so you never get a subset you did not ask for.

When the document has claims but none fall within your focus, `status` is
`"no_match"` and `identified_claims` is empty. The unfocused list is never
substituted — widen the focus and call again.

```python
if out.status == "no_match":
    ...  # nothing in this document matched; broaden the focus
```

A focused call costs the same single unit of the daily cap as an unfocused one.

On the CLI:

```bash
lenz extract "$(cat deck.txt)" --focus "market size and competitors"
```

## Multi-language output

The Lenz API returns prose fields (atomic claim, executive summary, debate, panel
reasoning) in any of 12 languages. Pass `language=` on `verify`, `verify_and_wait`,
`verify_batch`, `assess`, `extract`, or `ask.send`. Verdict labels stay English
regardless of language. On `extract`, `language` and `focus` are independent —
a focus written in any language selects claims emitted in `language`.

```python
v = client.verify_and_wait(
    claim="La Tierra es plana",
    language="es",  # Spanish output
)
print(v.verdict, v.language)
# False es
```

Supported codes: `en` (default), `es`, `de`, `fr`, `it`, `pt`, `nl`, `sv`, `da`,
`no`, `fi`, `bg`. Per-item override on `verify_batch`:

```python
batch = client.verify_batch(
    claims=[
        {"claim": "Coffee causes cancer."},  # en (batch default)
        {"claim": "El café causa cáncer.", "language": "es"},  # overrides
    ],
    language="en",
)
```

## Configuration

```python
Lenz(
    api_key="lenz_...",  # or set LENZ_API_KEY env var
    base_url="https://lenz.io/api/v1",  # override for staging / local
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

## Maintainer

[@Pavel12431432](https://github.com/Pavel12431432)
