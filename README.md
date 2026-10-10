# lenz-io

Official Python SDK for the [Lenz Fact Checking API for AI Product Teams](https://lenz.io/developers).

**Six API calls: one research-depth ladder, one call that runs it on a whole draft, and the citation check on its own.**

- `extract` — pull verifiable claims out of any text, optionally narrowed with a `focus`. Free, 1000 calls/account/day (shared across your API keys).
- `assess` — fast 3-model panel verdict in ~15s. Sync, paid.
- `verify` — full multi-model pipeline with citations in ~90s. Async, paid.
- `citecheck` — the citation check on its own: does each source a draft cites say what the draft says? Async.
- `ask` — follow-up questions grounded on a verification. Sync, paid.
- `review` — the ladder on a draft in one call, its citations too if asked: issues and rewrites in 2-4 min. Async, paid.

Built for teams whose AI output is async or document-shaped: legal-memo
generators, deep-research products, due-diligence platforms, vertical
agents producing structured deliverables. Not chat AI, not voice AI,
not real-time copilots — pipeline runs are the wrong shape for those.

## First call

Get a free API key, with free credits to start, at
[lenz.io/api-credentials](https://lenz.io/api-credentials), then:

```bash
pip install lenz-io
export LENZ_API_KEY=lenz_...
```

```python
from lenz_io import Lenz

client = Lenz()  # reads LENZ_API_KEY
row = client.assess(claim="The Great Wall of China is visible from space.").claims[0]
if row.status == "failed":  # no verdict for this row (not charged): failure says why
    print(row.failure.code, row.failure.hint)
else:
    print(row.verdict, row.confidence)  # e.g. False high  (about 15-20 s, 1 credit)
```

Using asyncio? `AsyncLenz` has the same methods, awaited
([more](#using-lenz-from-async-code)): `async with AsyncLenz() as client:
row = (await client.assess(claim=...)).claims[0]`.

From there: [the whole ladder](#quickstart--the-canonical-integration) on an
LLM answer, [a review](#review-a-draft) of a draft, or [the command-line
tool](#command-line-tool).

## Review a draft

One call runs the whole ladder on a draft: it pulls out the claims, gives each
a quick `assess` verdict, and sends the ones that look wrong or uncertain
through the full `verify` pipeline, up to five by default. It takes two to four
minutes and hands back the issues, with suggested rewrites.

```python
from lenz_io import Lenz

client = Lenz(api_key="lenz_...")

draft = """
The EU AI Act entered into force on 1 August 2024, and its obligations for
general-purpose models applied from 2 August 2025. Fines for prohibited
practices reach 7% of global annual turnover. About 40% of European
companies had started compliance work by the end of 2024.
"""

# One call: extract, assess, and verify the doubtful claims (async, 2-4 min)
review = client.review_and_wait(text=draft)
print(review.outcome)  # clean | issues_found | incomplete | unchecked
for i in review.issues:
    print(i.verdict, i.confidence, i.claim)
    if i.suggested_rewrite:
        print("  Suggested rewrite:", i.suggested_rewrite)

# Past the cap: send the remaining claims to /verify in one batch
capped = [{"claim": c.claim} for c in review.claims if c.escalation and c.escalation.disposition == "cap"]
results = client.verify_batch_and_wait(claims=capped) if capped else []
```

**Which claims get the deep check.** A claim is deep-checked when its quick
verdict is `False`, `Mostly False` or `Mixed`, or its confidence is `low`, up
to five of them. Change the rule with flat keyword arguments:

```python
client.review_and_wait(
    text=draft,
    verdicts=["False", "Mostly False"],  # quick verdicts that get a deep check ([] = none)
    confidence=["low", "medium"],  # confidence bands that get one ([] = none)
    max_verifications=10,  # the deep-check cap (0 = quick checks only)
    max_assessments=20,  # how many of the draft's claims get a quick verdict
    depth="low",  # every deep check at half the credits
)
```

**Reading the result.** `outcome` is the one field to branch on. Each entry in
`issues` is a claim whose final verdict is `False`, `Mostly False` or `Mixed`:
`source` says whether that verdict comes from a deep check (`verification`,
with `key_finding`, `url` and `verification_id`) or from the quick check alone
(`assessment`, with the reviewers' `rationale`; `escalation.disposition` says
why it was not deep-checked). `suggested_rewrite` comes from the deep check;
an issue that stayed on the quick verdict has one only when the review asked
for suggested edits (`suggest_edits=True`). It is not verified itself:
review it, or run it through `verify`, before you use it. `claims` lists every
claim with both checks; `failures` lists the ones whose work failed. The
top-level types are importable from `lenz_io`; the nested ones (`ReviewAssessment`,
`ReviewVerification`, `ReviewSummary`, …) from `lenz_io.models`.

**Checking the draft's sources.** `max_citations=N` (1-20) also checks the
draft's first N citations, its links and DOIs: does each source say what the
draft says it does? Links are read from `text`, so keep a link as a markdown
link (`[words](https://...)`); a Word or Google document pasted as plain text
loses them. With `max_assessments=0` the review checks the sources and no
claim.

```python
review = client.review_and_wait(text=draft, max_citations=20, max_assessments=0)
s = review.summary
print(f"{s.citations_found} found, {s.citations_selected} checked")
for c in review.citation_issues:  # most serious first
    print(c.finding, c.cited_url or c.doi, c.statement)
    if c.snippet:
        print("  The source says:", c.snippet)
```

`finding` is one of `doi_not_found`, `page_not_found`, `contradicted`,
`quote_not_in_source`, `not_in_source` or `metadata_mismatch`, most serious
first. `partly_supported` (the source backs part of the statement) is reported
in `citations` with its snippet, but it is not an issue: `is_issue` is false
and it is not in `citation_issues`. `citations` lists every checked
citation with its `check`; a row that could not be checked says why in
`check.unchecked_reason` and what to do in `check.hint`, and
`citation_failures` lists the ones that failed on our side. A citation issue
makes `outcome` `issues_found` even when `issues` is empty. `rationale` is a
reviewer's note, not a checked source; `snippet` is the passage from the page.
`more_claims` and `more_citations` list what the draft holds past
`max_assessments` and `max_citations`: found, not checked, to send in a later
request. Leave `max_citations` out (or `0`) and no citation is checked.
Only claims traced directly back to the draft are checked. Each claim row's
`positions` says where the draft makes it (a `Position`: `start`/`end` are
code-point offsets into `text`, so `text[start:end]` is the passage, and
`text` is the passage itself). For a URL draft `start` and `end` are `None`
and `text` still carries the passage. `more_claim_locations` does the same for
`more_claims`: one `ClaimLocation` (`claim`, `positions`) per string, same
order, `None` until the draft is read. A citation's `position` is the same
`Position`, with `text` `None` (the row carries the `statement`).

**Suggested edits.** `suggest_edits=True` also returns, for each claim with a
suggested rewrite (from its deep check, or from its quick check when it stayed
on the quick verdict), the smallest edits to the draft that make it say what
the rewrite says, in the draft's own language. They cost no extra credits, and
the review completes once they are settled. Each
claim row (and its issue) carries `suggested_edits`: `status` `"pending"` or
`"completed"`, and `edits`, each a span of the `text` you sent (`start`/`end`
in code points, `text` the exact slice) and its `replacement`. `edits == []`
means no edit could be made safely, or it could not be computed. They are not
themselves verified. Two claims that share a passage can return overlapping
edits: apply one of them.

Offsets all index the text you sent, so apply every claim's edits together,
from the end of the text back:

```python
review = client.review_and_wait(text=draft, suggest_edits=True)
edits = [e for c in review.claims if c.suggested_edits for e in c.suggested_edits.edits or []]
chars = list(draft)  # code points
taken_from = len(chars)
for e in sorted(edits, key=lambda e: e.start, reverse=True):
    if e.end <= taken_from and "".join(chars[e.start : e.end]) == e.text:  # no overlap, draft unchanged
        chars[e.start : e.end] = list(e.replacement)
        taken_from = e.start
edited = "".join(chars)
```

**Waiting.** `review_and_wait` polls on the review's own
`poll_after_seconds`. Pass `on_update=` to see the quick verdicts as soon as
they are in and each deep check as it lands; without it the helper is silent.
It raises `ReviewFailed` when the review fails (`review.failure.code` and
`review.failure.hint` say why; the exception's `error_code` keeps its 2.x spelling) and `ReviewTimeout` after `timeout` seconds (600 by default); the review
keeps running, and the error carries its `review_id` and the last body read.
To submit without waiting, `client.review(draft)` returns a `review_id`;
read it with `client.get_review(review_id)`, or only the issues with
`client.get_review(review_id, view="issues")`.

A runnable version, with the errors handled:
[`examples/core/review_draft.py`](examples/core/review_draft.py).

**Credits.** 1 per claim assessed, plus 10 (5 at `depth="low"`) per deep
check, plus 1 per checked citation; `review.credits.charged` says what the
review cost. A resend with the
same `Idempotency-Key` within 24 hours returns the same review; a new key is a
new review.

**From the terminal.** `lenz review draft.md` prints the quick verdicts as
they arrive and rewrites each row as its deep check lands. The exit code is the
outcome, for CI: `0` clean, `1` issues found, `2` anything else (incomplete,
unchecked, failed, timed out, or an error). `--issues` prints only the issues,
`--json` the review body, `--max-assessments N`, `--max-verifications N` and
`--depth low` set the policy, and `--detach` prints the `review_id` for `lenz review --resume <id>`
(and exits `0`: it submitted, it did not review). The draft's first 20 sources
are checked too (1 credit per checked citation), and their count and their
issues print after the claims; a source issue exits `1` like a claim one.
`--max-citations N` checks the first N and `--max-citations 0` none, and
`lenz review draft.md --max-assessments 0` checks the sources and no claim.
This default is the CLI's: `client.review()` checks no citation unless you
pass `max_citations`.

## Check a draft's citations

`citecheck` runs the citation check on its own, without the rest of a review:
does each cited source say what the draft says it does? Send a draft (its
links are read from the text, as for `review`) or the statement-source pairs
yourself.

```python
check = client.citecheck_and_wait(draft, max_citations=10)
print(check.outcome)  # clean | issues_found | incomplete | unchecked
for c in check.citation_issues:  # most serious first
    print(c.finding, c.cited_url or c.doi, c.statement)

# Pairs: each checked as it is (max_citations does not apply)
check = client.citecheck_and_wait(
    pairs=[
        {
            "statement": "Water boils at 100 degrees Celsius at sea level.",
            "url": "https://en.wikipedia.org/wiki/Boiling_point",
        },
        {
            "statement": "Diamond sensors can measure temperature in a living cell.",
            "doi": "10.1038/nature12373",
            "cited_year": "2013",
        },
    ]
)
```

The body carries the same rows as a review's: `citations`, `citation_issues`,
`citation_failures`, `summary` and `more_citations` (the draft's citations past
`max_citations`, found but not checked). `client.citecheck(...)` returns a
`citecheck_id` at once; read it with `client.get_citecheck(citecheck_id)`.
`citecheck_and_wait` raises `CitecheckFailed` when the check fails (or is
cancelled elsewhere: `failure_class` `cancelled`) and `CitecheckTimeout` after
`timeout` seconds. The `citecheck.completed`, `citecheck.failed` and
`citecheck.cancelled` webhooks parse into a `CitecheckEvent`. A runnable version:
[`examples/core/citecheck_draft.py`](examples/core/citecheck_draft.py).

From the terminal: `lenz citecheck draft.md` (or `--pairs pairs.json`, a JSON
list of pairs) prints the count, the key numbers and each source issue, and
exits `0` clean, `1` issues found, `2` anything else.

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
lenz extract "$(cat draft.txt)" --locate                    # where the text makes each claim
lenz extract "Die Erde ist flach." --language auto           # claims in the text's own language
lenz assess  "The Great Wall is visible from space"          # fast verdict
lenz assess  "<claim 1>" "<claim 2>" "<claim 3>"              # one call, one verdict per claim (up to 20)
lenz verify  "Water boils at 90C at sea level"               # full pipeline (~90s)
lenz verify  "<claim>" --depth low                          # shallower, faster, half the credits
lenz verify  "<claim>" --json | jq .verdict                 # machine-readable
lenz status  <task_id>           # non-blocking: poll a verify task's progress
lenz show    <verification_id>   # full report — sources, warnings, panel + debate (-c for concise)
lenz ask <verification_id> "Which source is strongest?"
lenz review draft.md             # the whole draft: quick verdicts, deep checks, and up to 20 of its sources
lenz review draft.md --issues    # only the issues
lenz review draft.md --max-citations 0    # claims only, no source checked
lenz review draft.md --max-assessments 0  # only the sources, no claim
lenz citecheck draft.md          # the citation check on its own
lenz citecheck --pairs pairs.json   # statement-source pairs, each checked as it is
lenz usage                       # credits left, what they buy, and when they reset
lenz config                      # show which key/base URL is in use
```

Every command takes `--json` for a clean machine-readable object (also emitted
automatically when stdout is not a TTY, so pipes Just Work). Errors in `--json`
mode are `{"error": {"code", "message", "status"}}` on stdout with a nonzero
exit (an out-of-credits run reports `"code": "no_credits"` and adds
`upgrade_url`). `lenz usage` leads with the balance:

```text
Lenz usage  (Pro plan)
  5070 credits left  (≈ 507 verifications · 5070 assessments)
  Verify:   507 left  (13 / 520 quota + 20 extra · 10 credits each · 5 at depth "low")
  Ask:      5070 left  (130 / 5200 quota + 200 extra · 1 credit each)
  Assess:   5070 left  (130 / 5200 quota + 200 extra · 1 credit each)
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
claims = [c.claim for c in out.claims]

# 2. assess — one call per 20 claims (extract finds up to 100), one row per claim, same order
quick = [row for i in range(0, len(claims), 20) for row in client.assess(claims=claims[i : i + 20]).claims]
for c in quick:
    if c.status == "failed":  # no verdict: failure.code says why, failure.hint what to send next
        print("failed", c.failure.code if c.failure else "", c.claim)
        continue
    print(c.verdict, c.confidence, c.claim)
    if c.rationale:
        print("  ", c.rationale)

# 3. verify — escalate the low-confidence rows to the full panel + citations
# verify_batch_and_wait takes up to 20 claims a call: the first 20 here
doubtful = [{"claim": c.claim} for c in quick if c.status == "completed" and c.confidence == "low"][:20]
results = client.verify_batch_and_wait(claims=doubtful) if doubtful else []
for r in results:
    if r.verification:
        print(r.verification.verdict, r.verification.lenz_score, r.verification.executive_summary)

# 4. ask — follow-up grounded on a verification (when a claim was escalated)
deep = next((r.verification for r in results if r.verification is not None), None)
if deep is not None:
    reply = client.ask.send(deep.verification_id, message="Which source is strongest?")
    print(reply.content)
```

`assess(claims=[...])` takes up to 20 claims per call and always answers
with exactly one row per claim, in the order sent. A row that could not be
given a verdict comes back in position with `status == "failed"` and a
`failure` block: `failure.code` (`no_checkable_claim` / `framing_failed` /
`upstream_unavailable` / `timeout` — an open set; the last two are the ones
worth resending as-is) and `failure.hint`, one sentence on what to send next;
it is not charged. A compound item is assessed on its main claim and lists the
other claims it found in `more_claims` — send those as their own items to
check the rest. `assess(claim="...")` takes one text and answers
with a row per claim found in it, up to 20, at 1 credit each; a text that
makes more claims gets its 20 most check-worthy checked and the rest in
`more_claims`, unchecked and free — send them back as `claims`, 20 a call.

Each verdict row also carries an optional note. `rationale` is the
reasoning of a reviewer who agrees with the panel's verdict. It is a
reviewer's note, not a checked source; for sourced evidence, call `verify`.
Read it as optional: it can be `None`. (`dissent` is deprecated: it is always
`None` and is kept only so code that reads it keeps working.)

**Suggested rewrite.** `suggest_rewrite=True` also writes, on each row the
check found `False` or `Mostly False` with high confidence, the claim with its
wrong part corrected (`suggested_rewrite`, else `None`), at no extra credit.
It is not itself verified: review it, or run it through `verify`, before
using it.

```python
row = client.assess(claim="Venus is the closest planet to the Sun.", suggest_rewrite=True).claims[0]
if row.suggested_rewrite:
    print(row.suggested_rewrite)  # e.g. "Mercury is the closest planet to the Sun."
```

`assess` and `verify` share a result cache server-side: if a claim
already has a deep verification, `assess` returns it via
`verification_url` and you can skip the escalation. An answer served from
that cache (a claim checked in the last hour) is free, so a tool that
resends the same request is not charged twice.

## How verification works

Framing → Research → Debate (2 models, 2 rounds) → Panel Review
(3 reviewers running the same checks, 2 more when they disagree) → Conclusion. ~90 seconds wall-clock
per claim. `assess` runs a leaner 3-model panel against the same
framing for the ~15s pass.

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

The demo claim is cached for an hour after anyone verifies it, so it can
come back in seconds; otherwise it runs the full pipeline (~90s) like
your own claims. Use webhooks for production async flows.

> **Get your webhook secret here →** [lenz.io/api-credentials](https://lenz.io/api-credentials)

## What you get on the client

- **`client.extract(text=...)`** → `ExtractedClaims`. Free, capped at 1000/account/day. Add `focus=` to narrow the list — see [Steering extract](#steering-extract) — and `locate=True` to keep only the claims traced back to your text, with their positions (each `out.claims[i].positions`). Each attempt waits up to 150s by default (a timeout is retried like any transport error, and the call's idempotency key makes the retry replay the first answer); `timeout=` overrides it for that call.
- **`client.assess(claim=...)`** / **`client.assess(claims=[...])`** → `AssessResponse`. Sync. One statement (~15s; `text=` is accepted as an alias: a document is `text`, a claim is `claim`) or a list of up to 20 claims in one call (~15s) — exactly one row per claim, in order; rows that got no verdict have `status == "failed"` and a `failure` block (`failure.code`, `failure.hint`), in position and free. A single text past 20 claims lists the rest in `more_claims` (`[]` otherwise). The two forms are mutually exclusive. `timeout=` overrides the client timeout for that call (both forms default to 100s: a long text can take up to 90s).
- **`client.verify(...)`** → `TaskAccepted`. Async submit; returns a `task_id`. Get the result by polling (`client.wait(...)` / `client.get_status(...)`) or via a webhook.
- **`client.verify_and_wait(...)`** → `Verification`. Submit + poll until the pipeline lands (sync ergonomic). Equivalent to `wait(verify(...))`.
- **`client.wait(task)`** → `Verification`. Block on a `task_id` (or a `TaskAccepted`) until it terminates. The polling counterpart to a webhook.
- **`client.verify_batch(claims=[...])`** → `BatchAccepted`. Fan-out for multi-claim LLM outputs.
- **`client.verify_batch_and_wait(claims=[...])`** → `list[BatchItemResult]`. Fan out a batch and poll every item to completion; one result per claim, in input order, never raises on a per-item failure.
- **`client.ask.{history,send,reset}(verification_id, ...)`** → Q&A on a verification. `reply.content` uses a small markdown subset (`**bold**`, `*italic*`, `- ` or `* ` bullets, blank-line paragraphs) — render with a minimal markdown library or display verbatim. See [docs/quickstart#ask-reply-format](https://lenz.io/docs/quickstart#ask-reply-format).
- **`client.verifications.{list,iter,get,delete,related}(...)`** → manage past verifications. `iter()` walks every page lazily (`for item in client.verifications.iter(): ...`). Both take `page_size=` (1-100, default 20 on the server; anything else raises `ValueError` before the request): `client.verifications.iter(page_size=100)` reads 100 a page. All API claims are private; reference them by `verification_id`. Cache-hit on another customer's claim is transparent — you always see your own `verification_id`, never another customer's.
- **`client.library.list(...)`** / **`client.library.iter(...)`** → browse the public catalog (no API key needed); `iter` walks every page lazily and refuses `sort="random"`, which is not exhaustive.
- **`client.usage()`** → the account's credit balance (`usage.credits`), the price list (`usage.costs` — `verify` 10, `assess` 1, `ask` 1, `extract` 0 — plus `usage.cost_options` for parameter-dependent prices such as `depth`), and per-capability projections of that one pool (`usage.verify.remaining` is how many verifications the balance still buys), plus the daily `extract` rate limit. Also reports `has_webhook_secret` — whether this key can receive signed webhook callbacks (`verify` with a `webhook_url` needs one); the secret value itself is never exposed.

## Polling without webhooks

`verify()` returns immediately with a `task_id`; the pipeline runs async (~90s
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
        print(r.claim, "→", r.verification.verdict)
    else:
        print(r.claim, "→", r.status)  # needs_input | failed | timeout
```

A `failed` item with `status_detail is None` is a verification its account's
retention period has removed (HTTP 410, see [Retention](#retention)), a task
id nothing was found under (404) or an answer in another API version; every
other failure carries a `status_detail`. A 401 / 403 on a poll (the key
itself refused) raises from the whole call.

A verify takes ~90 seconds, so show your users where it is. `on_progress` fires
once per poll while the run is going — it takes the `task_id` as well, because
the batch helper round-robins several ids in one loop:

```python
client.verify_and_wait(
    claim="Sharks don't get cancer",
    on_progress=lambda task_id, p: print(f"{p.step} — step {p.index} of {p.total}"),
)
# framing — step 1 of 5
# research — step 2 of 5
# ...
```

`p.step` is one of `starting` / `framing` / `research` / `debate` /
`adjudication` / `conclusion`. `p.index` is stage **position**, not elapsed
work — the stages are uneven, so a bar driven by it sits on `research` for
roughly half the run. An exception raised inside your callback never breaks
the poll.

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

The accepted values are exported as `Literal` aliases for comparisons and
exhaustive matching: `Verdict` (the five labels and `"Error"`), `VerdictLabel`
(the five labels), `Confidence` and `Depth` (`"standard"` or `"low"`). The
fields themselves stay `str`, so a value a later API adds still reads.

```python
from lenz_io import Verdict

NEEDS_A_LOOK: set[Verdict] = {"False", "Mostly False", "Mixed"}
flagged = [row for row in quick if row.verdict in NEEDS_A_LOOK]
```

### Field names: the current names, and the deprecated 2.x ones

The API's current response shape gives each field one name across every
endpoint. Since 3.0 the SDK asks for it (`X-Lenz-API-Version: 2026-10-11`) and
reads only that shape from its own calls (webhooks of both shapes are still
parsed). The current names are attributes already. The 2.x names are
**deprecated** and still work, with the value they had in 2.x (except the few
values the API no longer sends, listed under "What reads differently" in the
[changelog](CHANGELOG.md)); they will be removed in a future major release.
Move to the current names when convenient:

| Read this | Instead of (deprecated, still works) |
|---|---|
| `ExtractedClaims.claims` (each `.claim`, `.positions`) | `claim`, `identified_claims`, `locations` |
| `AssessClaim.status` (`"completed"` / `"failed"`) and `.failure` | `verdict == "Error"`, `error_code`, `hint` |
| `AssessClaim.more_claims`, `ReviewAssessment.more_claims` | `identified_claims` |
| `AssessResponse.status` and `.failure` | `error`, `error_code` |
| `TaskStatus.failure` (`code`, `detail`, `hint`, `failure_class`, `retryable`, `docs_url`) | `error`, `failure_reason`, `failure_detail` and the flat fields |
| `TaskAccepted.claim`, `BatchItemResult.claim`, `CandidateClaim.claim` | `claim_text`, `text` |
| `Verification.completed_at` | `modified_at` |
| `ReviewSummary.claim_limit_exceeded`, `citation_limit_exceeded` | `claim_limit_reached`, `citation_limit_reached` |
| `FailureBlock.code`, `.detail` | `failure_reason` |
| `Usage.credits` and `Usage.costs` | the `verify` / `ask` / `assess` blocks, `quota_resets_at` |

"Nothing checkable" is `no_checkable_claim` in the current names; the 2.x
fields keep their own spelling (`not_a_claim`, `no_claim`). The current names
are read-only properties. The full list, with the aliases that have no
replacement, is in the [changelog](CHANGELOG.md).

**What `model_dump()` and `--json` return.** Not the response body as sent:
a model's `model_dump()` (and the CLI's `--json`, which prints it) holds the
2.x-compatible fields, computed from the response, plus the current-shape
keys the server sent (`failure`, `more_claims`, `claims`, `completed_at`, ...).
The body exactly as sent is `.raw` on a result (below), `exc.body` on an
error and `event.raw` on a webhook event.

**The body as received: `.raw`** (since 3.2). Every result model has a `raw`
property: the JSON object it was read from, exactly as received, as plain
dicts and lists (a deep copy: changing it changes nothing). No 2.x alias,
default or rewritten value is in it, whatever `legacy_aliases` is. Nested
results hold their own part of the body:

```python
out = client.assess(claims=["A.", "B."])
out.raw  # {"claims": [...], "more_claims": [...], ...} as sent
out.claims[0].raw  # that row's object
status = client.get_status(task_id)
status.result.raw if status.result else None  # the verification's object
```

Covered: every result a call returns and every model nested in one
(`TaskStatus.result`, `AssessResponse.claims`, `ReviewFull.claims` /
`issues` / `citations`, ...). A webhook's payload as received is
`event.raw`; the models `parse_webhook` builds from it (`event.verification`,
...) hold the object the SDK built them from, which can leave out a `null`
the payload carried. A
`ReviewStarted` / `CitecheckStarted` that a 409 naming the job settled (see
[Idempotency](#idempotency)) holds that 409's body. `BatchItemResult` is built
by the SDK, so its `raw` is `None` (its `verification.raw` and
`status_detail.raw` are the bodies it was built from). A model built with
keyword arguments has `None`; `Model.model_validate(data)` sets it. `raw` is
the object the model was read from, not a copy taken then (a copy would cost
every read, used or not); each access returns a new copy. A client's reads
hand the model a freshly parsed body, so nothing else holds it; when you
validate a dict of your own, changing that dict in place changes what `raw`
returns.

**The answer's status and headers: `.http_status` and `.headers`** (since
3.2). The result a call returns also carries the HTTP status of the answer it
was read from (`200`, or `202` for a receipt such as `TaskAccepted`,
`BatchAccepted`, `ReviewStarted` or `CitecheckStarted`) and its headers, as
the same read-only, case-insensitive mapping errors carry (`exc.headers`):

```python
started = client.review("The Eiffel Tower is in Paris. It opened in 1889.")
print(started.http_status)  # 202
print(started.headers.get("location"))  # where to read the review
print(started.headers.get("retry-after"))  # None when it states no wait
print(started.headers.get("x-request-id"))  # quote it to support
```

Use `.get()`: which headers an answer carries depends on the endpoint (a
review's or citation check's receipt names its `Location`; a `verify`
receipt does not).

**The `Result` type** (since 3.2). Every call that returns a result read from
one answer returns a subclass of `lenz_io.Result`, on which `http_status` is
an `int` and `headers` a `lenz_io.errors.ResponseHeaders`, never `None`, so
code handling any call's result can be typed once, with no check:

```python
from lenz_io import Result


def log_answer(result: Result) -> None:
    print(result.http_status, result.headers.get("x-request-id"))


log_answer(client.assess("Water boils at 100 C at sea level."))
log_answer(client.verify("The Eiffel Tower is in Paris."))
```

The calls are annotated with these subclasses: `AssessResponse`,
`ExtractedClaims`, `TaskStatus`, `BatchAccepted`, `CancelResult`,
`ReviewStarted`, `ReviewFull`, `ReviewIssues`, `CitecheckStarted`,
`Citecheck`, `Usage`, `VerificationList`, `Certificate`,
`RelatedVerifications`, `AskHistory`, `AskReply` and `LibraryList`. Two
models are also nested in other results, so the calls returning them at the
top level return a subclass of their own: `verify` returns a
`TaskAcceptedResult` (a `TaskAccepted`) and `verifications.get` a
`VerificationResult` (a `Verification`). Each compares equal to the base
model read from the same body (`client.verifications.get(v) ==
status.result`), and `isinstance(r, Verification)` holds, but
`type(r) is Verification` no longer does. A `Result` not read from an answer
(one you validate yourself, a webhook's `review` or `citecheck`) has
`http_status` `0` and empty `headers`.

Only the top-level result has them: a model nested in it (`TaskStatus.result`,
an `AssessResponse.claims` row, the items an `iter()` yields, each of which
sits in a page, ...) and a result the SDK builds (`BatchItemResult`) are not
`Result`s and have `None` for both. A result a wait helper returns carries its
last poll's (`review_and_wait`, `citecheck_and_wait`); `verify_and_wait`
returns the poll's nested `result`, a plain `Verification` with `None`. Like
`raw`, they are not part of `model_dump()` or `--json`.

### A suggested rewrite (`suggested_rewrite`)

A verification can carry `suggested_rewrite`: a suggested rewrite of its
`claim` that the verification's findings support, to use in place of the
original sentence.

```python
v = client.verifications.get("a1b2c3d4")

if v.suggested_rewrite is not None:
    print(v.suggested_rewrite)  # the rewritten sentence
```

- **It has not been verified itself.** Before using it, review it or run it
  through `client.verify(...)`.
- **`None` for a true claim**, when no correction is established, and on
  verifications that predate the field.
- On every verification, single or listed: `verifications.get`,
  `verifications.list`, `library.list`, `verify_and_wait`, `wait`, a completed
  `get_status`, and the `result` of a `verification.completed` webhook.
  `assess` rows carry their own with `suggest_rewrite=True`.

### The warranty (`coverage`)

Qualifying verdicts on paid Pro and Scale plans carry a contractual
warranty from Lenz. Every verification tells you where it stands:

```python
v = client.verifications.get("a1b2c3d4")

if v.coverage is None:
    ...  # Lenz is not operating the warranty, or you called without a key
elif v.coverage.status == "covered":
    cert = client.verifications.get_certificate(v.verification_id)
    # cert.leaf / cert.signature / cert.anchors — verifiable WITHOUT Lenz,
    # with the script at cert.verifier_url and the keys at cert.keys_url.
else:
    print(v.coverage.reasons)  # e.g. ["plan"] or ["verdict"]
```

Three things worth getting right:

- **`coverage is None` and `status == "uncovered"` are different facts.** The
  first means Lenz is not operating the warranty, or the call was
  unauthenticated; the second means it IS, and this verdict did not qualify.
- **The money fields are three, not two.** `currency` is ISO 4217, and `cap` /
  `aggregate` are integers in **major units** — `cap=10000` means ten
  thousand, not a hundred. Read `currency`; do not assume EUR.
- **A 404 from `get_certificate()` does not mean "not covered"** — check
  `coverage.status` for that.
- **`reasons == ["account"]` means the account turned certificates off.** An
  account on Pro or Scale can switch them off on the
  [API credentials page](https://lenz.io/api-credentials); checks submitted
  from then on carry no certificate. A verification that already carries a
  certificate keeps it.

### Retention

By default a verification stays available for as long as the account exists.
An account on Pro or Scale can set a retention period on the
[API credentials page](https://lenz.io/api-credentials). Once a verification
is older than that period, reading it raises `LenzGoneError` (HTTP 410,
`code == "purged"`, with `purged_at`), and `wait()` raises it at once instead
of polling to its deadline. It also disappears from `verifications.list()`.
A certificate issued for it stays available from
`verifications.get_certificate()`.

```python
from lenz_io import LenzGoneError

try:
    v = client.verifications.get("a1b2c3d4")
except LenzGoneError as exc:
    print(exc.purged_at)  # "2026-10-25T10:00:00+00:00"
```

### Webhooks

```python
from lenz_io import LenzWebhooks, VerificationCompleted, VerificationFailed, VerificationNeedsInput

webhooks = LenzWebhooks(secret="whsec_...")

# In your web handler:
event = webhooks.parse(raw_body=request.body, headers=request.headers)
if isinstance(event, VerificationCompleted):
    # event.verification is the verification as client.get_status returns it
    # (a TaskStatus); the verdict is under .result, a typed Verification.
    v = event.verification.result if event.verification else None
    if v is not None:
        print(v.verification_id, v.verdict, v.lenz_score, v.confidence)
elif isinstance(event, VerificationNeedsInput):
    options = [c.claim for c in event.claims]  # pick, then client.select(event.task_id, claims=[...])
elif isinstance(event, VerificationFailed):
    # failure.code is WHAT stopped it; failure_class is WHY (closed set) and
    # retryable tells you what to do about it.
    if event.failure and event.failure.retryable:
        resubmit_later(event.task_id)  # transient provider outage
    else:
        log_permanent_failure(event.task_id, event.failure.code if event.failure else "")
```

`.verification` (since 3.0) is built from either payload shape, and is `None`
only when a payload cannot be read as one. The flat `event.result` dict (and
`error`, `failure_class`, `retryable` on a failed event) are kept with their
2.x values; prefer `.verification` and `.failure`.

If you're on Python 3.10+ a `match` statement reads even cleaner — events are
plain dataclasses, so structural pattern matching works.

**If you rely on the warranty, publish on `CertificateTimestamped`, not on
`VerificationCompleted`.** Cover requires the certificate's qualified
timestamp to precede what you publish or send, so a pipeline keyed on
`completed` races the anchor and can put the statement out before cover
exists:

```python
from lenz_io import CertificateTimestamped

if isinstance(event, CertificateTimestamped):
    # The timestamp landed; cover is in force. Safe to publish now.
    publish(event.verification_id, certificate=event.coverage["certificate_url"])
```

It carries `coverage` instead of `result` — it reports a timestamp landing,
not a verdict being produced.

A task cancelled elsewhere (the website's Stop button, another process) sends
`verification.cancelled`, parsed as `VerificationCancelled`
(`event.verification.status` is `"cancelled"`), `review.cancelled` and
`citecheck.cancelled`. These are sent for work submitted with the API version
this SDK uses; a cancellation of work submitted by an older client keeps
arriving as the `*.failed` event with `failure_class` `cancelled`.

A review sends `review.completed` or `review.failed`, parsed as `ReviewEvent`
with the final review on `event.review`. Deduplicate on `event.event_id`: it is
the same on every retry of one delivery. The deep checks a review runs send no
`verification.*` events of their own, and events this SDK version does not know
parse as a plain `WebhookEvent`: ignore them. `event.review` is `None` if the
body could not be read (`event.raw` keeps it).

```python
from lenz_io import ReviewEvent

if isinstance(event, ReviewEvent) and event.review and not already_seen(event.event_id):
    for issue in event.review.issues:
        flag(issue.claim, issue.verdict, issue.suggested_rewrite)
```

`parse_webhook(body)` parses a body whose signature you have already checked
(or one you stored) into the same events.

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
print(u.credits.remaining // u.costs["verify"], "verifications left")  # that balance in verifications
print(u.credits.extra, "of them non-expiring")  # grants + top-ups
```

The `verify` / `ask` / `assess` blocks on `Usage` are deprecated (kept, with
their 2.x values) **projections** of the one balance into each capability's
unit — how many of those calls the remaining credits would buy — not separate
allowances. Spending on any one of them moves all of them. Derive the same
number from `credits` and `costs`, as above.

`credits.extra` is the non-expiring part of the balance. Its old name,
`credits.bonus`, is deprecated: the same number, it emits a
`DeprecationWarning` when read and is kept for existing code.

Per-capability `bonus` is that capability's share of `credits.extra`, so 200
extra credits read as `assess.bonus == 200` and `verify.bonus == 20`. The old
`capability.credits` field is a deprecated alias of `bonus` (it never meant
the pool); reading it emits a `DeprecationWarning` and it is kept for
existing code.

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
The `depth` echoed on the completed verification is what the verdict was
*produced* with, so it can read `standard` on a `low` request — the echo
describes the evidence behind the answer, the charge follows the request. A
batch may mix depths and is billed per item.

**A verdict served from the last hour's cache is free**, on `verify`,
`assess` and `review` alike. The one exception is a `verify` that issues your
business plan a new warranty certificate, charged at the depth you requested.

## Errors

Every error subclass is typed and carries a `request_id` you can quote on
support tickets:

```python
from lenz_io import (
    LenzAuthError,
    LenzConnectionError,
    LenzNotFoundError,
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
    #   Fix:    Your credential is missing, invalid or expired. Check the key you passed, or get a new one at https://lenz.io/api-credentials.
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
except LenzNotFoundError as exc:
    # HTTP 404. The id (or key) the request names finds nothing: retrying
    # will not change that. A LenzError, as in 2.x.
    print(exc.fix)
except LenzConnectionError as exc:
    # No answer at all, after the SDK's own retries: the network, DNS, TLS,
    # or (LenzRequestTimeoutError, a subclass) one request's timeout. A
    # LenzAPIError, as in 2.x; exc.__cause__ is the httpx exception. The
    # request may have reached the server: resend with the SAME key
    # (idempotency_key=exc.idempotency_key), never as a plain new call.
    schedule_retry_in(30)
```

**`retryable`** (since 3.0, on every error): whether sending the same request
again can succeed. `True` for a connection failure, a request timeout, a 429,
a 5xx and a 409 that means "not yet" (`idempotency_conflict`: the first
request with that key is still running; `verification_not_ready`); `False`
for any other 4xx and a version error; `None` when there was no HTTP status
(a missing key, a `*_and_wait` timeout). A failed verification, review or
citation check carries the server's own value (`None` when it sent none).

**`idempotency_key`** (since 3.0, on every error): the `Idempotency-Key` the
failed call sent, or `None` when it sent none. A resend is safe only with the
same key: pass `idempotency_key=exc.idempotency_key` back, and the server
replays the first answer (or reports the first request still running)
instead of running it again. A plain new call sends a new key, and can run
(and charge) the work twice.

```python
from lenz_io import LenzError

try:
    client.assess(claim="...")
except LenzError as exc:
    if exc.retryable:
        # Later, the same request with the same key:
        # client.assess(claim="...", idempotency_key=exc.idempotency_key)
        schedule_retry_in(getattr(exc, "retry_after", None) or 30)
    else:
        raise
```

Local argument mistakes (an empty id, two exclusive arguments, a blank claim,
a bad `page_size`, `timeout`, `max_retries`, `extra_headers` or `user_agent`)
raise `LenzUsageError` before anything is sent (since 3.2). It is a
`ValueError`, which 3.1 raised, and deliberately not a `LenzError`: an
`except LenzError` that handles API answers never catches a bug in the call.
A blank input or an empty list is refused with the sentence the API's 422
gives for the same request (`str(exc)`, table below); "blank" is what
`str.strip()` removes, the API's rule. When both `claim=` and `text=` are
given, the one with content is sent.

`LenzUsageError` carries `code` (what is wrong, typed `lenz_io.UsageErrorCode`;
`lenz_io.USAGE_ERROR_CODES` lists them) and `param` (the argument at fault, or
`None`): branch on these, never on the message. They are local codes, set by
the SDK before anything is sent, so they are the same whatever
`legacy_aliases` is (a blank claim is `blank_input` and a blank list item
`blank_item` in both modes; the server's `code` on an error answer is `exc.code`
of a `LenzError`, a different thing). The codes are the Node SDK's, string for
string:

| `code` | When | `param`, e.g. |
|---|---|---|
| `blank_input` | a blank claim, text or message (`verify`, `assess`, `review`, `citecheck` with neither text nor pairs, `ask.send`) | `"claim"`, `"text"`, `"message"` |
| `blank_item` | a blank item in a list (`assess(claims=[...])`, `select`) | `"claims[2]"` |
| `empty_list` | an empty list (`assess(claims=[])`, `select`) | `"claims"` |
| `invalid_page_size` | `page_size` outside 1 to 100 | `"page_size"` |
| `invalid_page` | an iterator's `page` below 1 | `"page"` |
| `invalid_id` | an id that cannot name anything: empty, `.`, `..`, a lone surrogate | `"task_id"`, `"verification_id"`, `"review_id"`, `"citecheck_id"` |
| `invalid_header` | `extra_headers` (a reserved or malformed header) or `user_agent` | `"extra_headers"`, `"user_agent"` |
| `invalid_option` | a client or `with_options` option, or a call's `timeout` / `max_retries` | `"timeout"`, `"max_retries"`, `"api_key"`, `"legacy_aliases"` |
| `conflicting_input` | two arguments that exclude each other | `"claims"`, `"text"`, `"max_citations"` |
| `invalid_argument` | anything else (an `assess` list item that is not a string, ...) | `"claims[0]"`, `"view"`, `"sort"`, `"verdicts"` |

For the codes that mirror the API's (`blank_input`, `blank_item`,
`empty_list`), the message is the API's own sentence for the same request, so
it reads the same whether the SDK or the API refused it:

| Call | `code` | Message |
|---|---|---|
| `verify("")`, `verify_and_wait("")`, `assess("")` | `blank_input` | `claim is required.` |
| `assess(claims=[])` | `empty_list` | `claim: Field required` (the API reads an empty list as no input) |
| `assess(claims=["A.", " "])`, `select(task_id, claims=["A.", " "])` (a list or a tuple) | `blank_item` | `claims[1] is blank.` |
| `select(task_id, claims=[])` | `empty_list` | `claims is required.` |
| `review("")`, `review_and_wait("")` | `blank_input` | `text: send the draft, or one public http(s) URL.` |
| `citecheck("")` (no pairs) | `blank_input` | `payload: Value error, send exactly one of text and pairs` |
| `ask.send(id, message="")` | `blank_input` | `Message cannot be empty.` |

The other codes carry the SDK's own message. A blank `select` item is refused
since 3.2 (the API drops it silently). Called with `texts=` (the alias), `select`
names it in `param` (`"texts"`, `"texts[i]"`); an empty `texts` keeps the API's
`claims is required.`, a blank item reads `texts[i] is blank.`.

Not every blank input is refused locally: `verify_batch` / `verify_batch_and_wait`
with an empty list or a blank item, and `citecheck(pairs=[])`, are sent, and the
API's 422 answers them (a `LenzValidationError`).

```python
from lenz_io import LenzUsageError

try:
    client.assess(claims=rows)
except LenzUsageError as exc:
    if exc.code == "blank_item":
        print(f"{exc.param} is empty")  # claims[2] is empty
```

Three authentication errors, all `LenzAuthError`s (so an
`except LenzAuthError` catches each):

| Error | When | `status_code` |
|---|---|---|
| `LenzMissingKeyError` (since 3.2) | no key is configured and the call needs one (`api_key=None` with no `LENZ_API_KEY`, an empty key, a `with_options(api_key=None)` copy); raised before sending | `0` |
| `LenzInvalidKeyError` (since 3.2) | the key cannot be sent (see [Configuration](#configuration)); raised when the client or copy is built | `0` |
| `LenzAuthError` | the server refused the key (401 / 403) | `401` / `403` |

A request that could not be built or sent at all (a `base_url` that is not
http(s), a request httpx refuses to write), or whose answer could not be
decoded, raises `LenzConnectionError` with the httpx error as `__cause__` and
`retryable` `False`, never retried (since 3.2; 3.1 let the httpx error
escape).

**`body` and `code`.** `exc.body` is the parsed JSON body of the error
response exactly as sent (`None` or `{}` when there was none): the source of
truth, every other field is read from it. `exc.code` is the server's
machine-readable code, `""` when there is none. With the default
`legacy_aliases=True` it is the code lenz-io 2.x reported for that endpoint,
which left some codes out (`not_found`, `validation_error`,
`idempotency_conflict` on most endpoints, ...) and renamed a few (a blank
`assess` item reads `blank_item`). A client built with `legacy_aliases=False`
reports exactly the body's `code` (since 3.2): `not_found`,
`idempotency_conflict`, `validation_error`, `blank_input`,
`unsupported_language`, `too_many_items`, `invalid_request`,
`internal_error`, `not_authenticated`, `verification_not_ready`, ... A
`code` that is not a string reads `""`.

**`retry_after`** is the wait the response stated, in whole seconds, as the
SDK parsed it (a value that is not a finite number reads as unstated). Where
it comes from depends on the error:

| Error | `retry_after` read from, first match | Unstated |
|---|---|---|
| `LenzRateLimitError` (429) | the `Retry-After` header, then the body's `reset_in_seconds`, `retry_after_seconds`, `retry_after` | `0` |
| `LenzUpstreamUnavailableError` (503 `upstream_unavailable` / `capacity`) | the body's `retry_after`, then `retry_after_seconds`, then the `Retry-After` header | `None` |
| `LenzAPIError` (any other 5xx) | the `Retry-After` header | `None` |

The SDK's own retry ladder sleeps the `Retry-After` header first, then the
body's wait, whatever the status (up to 60 s; a longer stated wait on a 429
or a typed 503 raises at once with it on `retry_after`).

**`headers`** (since 3.2): the response's headers, a read-only mapping whose
lookups ignore case (`exc.headers["retry-after"]`); empty when there was no
response. **`served_version`** (since 3.2): the `X-Lenz-API-Version` the
response named, or `None`.

**The text of an error.** `str(exc)` is the message, then one indented line
each for what is known:

```
Rate limit exceeded
  Cause:  Rate limit exceeded
  Fix:    Wait Retry-After seconds and retry.
  Docs:   https://lenz.io/docs/rate-limits
  Request ID: req_abc123
```

`exc.message` is the first line alone; `cause`, `fix`, `doc_url` and
`request_id` are the others. The SDK never puts your API key in it; the
message and cause are the server's `detail`, which for a 422 can quote what
you sent.

A `*_and_wait` helper that reaches its own `timeout` raises `LenzTimeoutError`
(`ReviewTimeout`, `CitecheckTimeout`): the job keeps running server-side, so
read it later by its id rather than resubmitting. A poll answered 401, 403,
404 or 410 (or in another API version) ends the wait at once with that error;
in `verify_batch_and_wait` a 404, a 410 or a version error fails that item only (the
others keep going), while a 401 / 403 raises. A 5xx, a 429 or a network
failure is polled again (a poll whose answer is JSON but not an object, too).
Each poll is bounded by what is left of the
`timeout` (each phase at most the client's own), and no poll starts once it
is spent; `timeout=0` reads each status once, as in 2.x. `ReviewFailed`, `ReviewTimeout`, `CitecheckFailed` and
`CitecheckTimeout` are also importable as `ReviewFailedError`,
`ReviewTimeoutError`, `CitecheckFailedError` and `CitecheckTimeoutError` (the
same classes).

`LenzApiVersionError` (a `LenzError`) is raised when a successful response
(status below 400) names an API version other than the one this SDK reads.
Every response carries the version that served it in `X-Lenz-API-Version`;
lenz-io 3.x asks for `2026-10-11` and reads that version's shape only, so an
answer in `2026-05-13` (a server still on the older version, or a reply
replayed from an idempotent request stored before the change) is refused
rather than misread. It carries `served_version` (what the response named),
`expected_version` (`"2026-10-11"`, both since 3.2), `api_version` (the same
as `served_version`), `status_code` and `body` as sent. If it persists,
contact support with the request id; lenz-io 2.x reads both versions. A
response without the header is read as usual, and webhook payloads are never
refused (they are parsed in either shape). Its message names the version:
"The API answered 2026-05-13; this SDK reads 2026-10-11 only."

An **error** response (400 or above) in another version raises its own error,
the one the status and body call for (a `LenzQuotaExceededError` with its
balance, a `LenzRateLimitError` with its `retry_after`, ...), with that
version on `served_version` (since 3.2; 3.0 and 3.1 raised
`LenzApiVersionError` for it, hiding the balance or the wait). Its fields are
read from the body as sent; `exc.body` is the body.

`LenzInvalidResponseError` (a `LenzAPIError`, since 3.2) is raised when a
status below 400 (a 2xx, or a redirect httpx did not follow, such as an HTML
302) carries a body that is not JSON, typically a proxy,
captive portal or load balancer answering in the API's place. It carries the
real `status_code` (`0` stays reserved for a request that got no answer), the
`request_id` if one came back, `body` `None`, `body_text` (the body as text:
its first 1,000 characters, followed by `…` when it was longer) and
`retryable` `None`. It is also a `json.JSONDecodeError`, which is what 3.1 and
earlier raised there, so existing `except json.JSONDecodeError` blocks keep
working. Since 3.2 it is also raised for an empty 2xx body (a 204, a 205 and
`Content-Length: 0` included: 3.1 read those as `{}`; no endpoint answers
without a body), a body that is JSON but not an object (`null`, a list, a
number, a string: every endpoint answers with an object; its message says
"not an object"), and any 3xx, with or without a body (the API never
redirects and httpx does not follow one; the message names the `Location`).
It is also raised for a JSON object with a field of the wrong type
(`{"claims": "x"}`, `{"claims": [42]}`), where earlier releases let pydantic's
`ValidationError` escape (it is the error's `__cause__`): then `body` is the
object as parsed, `body_text` its text, and the message names every field at
fault by its path in the body as sent. Its `fix` suggests upgrading lenz-io:
a newer API may send a shape an older release cannot read. A `null` the SDK
tolerates (see the changelog) is still read as unsent first. Every property a
result reads only when you ask for it (`ExtractedClaims.claims`, a `failure`
on a status, an `assess` response or row, `status`, `more_claims`,
`completed_at`, `claim`, `code`, `detail`, a review summary's counts, ...)
raises the same error on that read for a value of the wrong type (`"failure":
"x"`, `"status": 1`), with the answer's status and headers. Its message names
the field by its path in the model read (a top-level result's `failure`, or
`failure` for an `assess` row's, not `claims[0].failure`), and its `body` is
that model's part of the answer (`row.raw` for a row). Since 3.2 none
reads such a value as not sent (`null` and an absent key still read as not
sent). A `completed` poll with no `result` (absent or `null`) is a run that
ended but cannot be read: `wait` / `verify_and_wait` raise this error for it
(3.1 raised `LenzPipelineError`), and in `verify_batch_and_wait` that item is
`failed` with the poll on `status_detail` and the error on `error`. In a wait
helper, an unreadable poll whose `status` says the run ended (completed,
failed, cancelled, needs input; for a review or a citation check, only a body
carrying that job's id) raises it at once (in `verify_batch_and_wait` only
that item fails, with the error on `BatchItemResult.error`); any other is polled again, and if the wait then times out,
the timeout says the last poll could not be read and carries that answer's
error as `__cause__`.

**Replays of requests made before the switch.** An idempotent request first
sent before lenz.io served `2026-10-11`, and replayed with the same
`Idempotency-Key` afterwards, answers with the stored reply in `2026-05-13`,
which 3.x refuses with `LenzApiVersionError`. Replays are kept for 24 hours
and replies stored since 2026-10-09 are kept in both versions, so in practice
none remain when 3.0 ships. If you meet one, finish that work with lenz-io
2.x; never change the key to get past it, which would run (and charge) the
request a second time.

```python
from lenz_io import LenzApiVersionError

try:
    client.assess("The Earth is round.")
except LenzApiVersionError as exc:
    print(exc.served_version)  # "2026-05-13"
    print(exc.expected_version)  # "2026-10-11"
```

A failed *verification* (as opposed to a failed HTTP call) raises
`LenzPipelineError` from `verify_and_wait` / `wait`. Since 2.8.0 it carries
`failure_class` (closed set: `upstream_unavailable` | `insufficient_evidence`
| `invalid_input` | `cancelled` | `internal`) and `retryable` — `True` means
a transient provider-side exhaustion where resubmitting the same claim is the
right move; older servers leave it `None`. A verification cancelled elsewhere
(the website's Stop button, another process) raises the same error with
`failure_class == "cancelled"` and `retryable` `False`; `review_and_wait` and
`citecheck_and_wait` do the same with `ReviewFailed` and `CitecheckFailed`.

`LenzQuotaExceededError` is a **sibling** of `LenzAuthError`, not a subclass —
"fix your key" and "top up your account" are different actions. So if you were
catching `LenzAuthError` to handle an empty balance, that branch stops firing;
add a `LenzQuotaExceededError` handler.

## Resuming a verification

If a `verify_and_wait` call exceeds its `timeout` (default 300s) or your
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

## Stopping a run

A verification, a review or a citation check that is still running can be
stopped. Each has its own method, and each answers 200 whatever the state of
the run, so losing a race is not an error:

```python
result = client.cancel("tsk_abc123")  # a verification -> CancelResult
if result.cancelled:
    print("stopped:", result.status)  # "cancelled"
else:
    print("already ended:", result.status)  # "completed" or "failed"

review = client.cancel_review("d6b2bd72")  # the full view, like get_review
print(review.status, review.credits.charged)  # "cancelled", what it cost

check = client.cancel_citecheck("12bbbf65")  # like get_citecheck
print(check.status, check.credits.charged)
```

- `cancel(task_id)` returns a `CancelResult`. `cancelled=True` with
  `status == "cancelled"` means the run is cancelled, by this call or an
  earlier one, so a repeated or retried cancel answers `True` too.
  `cancelled=False` means it is not cancelled, and `status` is the run's
  status, normally `"completed"` (the verification exists and was charged as
  usual) or `"failed"`. A run waiting on `select` is cancelled too. A task that
  `select` already resolved answers `cancelled=False` with `needs_input`: cancel
  the task ids `select` returned.
- `cancel_review(review_id)` stops the review and everything in it: its quick
  checks, its deep checks and its citation checks. It returns the review as it
  stands, `status == "cancelled"`; a review that had already ended is returned
  unchanged (`completed` or `failed`).
- `cancel_citecheck(citecheck_id)` returns the check the same way.
- A review's deep checks are cancelled **through the review**. Calling
  `cancel(task_id)` with the `task_id` of one raises a `LenzError` with
  `code == "use_review_cancel"` (HTTP 409); call `cancel_review` instead. The
  SDK sends that request once and does not wait or resend.
- An unknown id, another account's, or (for `cancel`) a task started on the
  website raises `LenzNotFoundError`.
- A cancelled verification is not charged and saves nothing. A cancelled
  review or citation check is charged only for what it delivered before the
  cancel (quick checks served, deep checks that finished, citations checked);
  the rest is refunded or never charged. `credits.charged` on the result is
  what the review or check cost.
- Cancelling twice is safe, so the calls send no `Idempotency-Key`. They are
  retried on a 5xx, a 429 or a dropped connection, like any call that is safe
  to repeat.
- Afterwards `get_status`, `get_review` and `get_citecheck` return the
  `cancelled` status, and `wait`, `review_and_wait` and `citecheck_and_wait`
  raise the failed error with `failure_class == "cancelled"`. A `webhook_url`
  receives `verification.cancelled`, `review.cancelled` or `citecheck.cancelled`.

## Idempotency

Every call that charges or starts work sends an auto-generated
`Idempotency-Key` by default: `verify`, `verify_and_wait`, `verify_batch`,
`verify_batch_and_wait`, `select`, `assess`, `extract` and `ask.send` (one
random key per call, reused across that call's own retries), so a network
drop after submit doesn't spawn a duplicate or charge a second time. Override
with `idempotency_key="..."` to pin a specific key (it also makes a retry
from another process replay), or `idempotency=False` to opt out. `review`,
`citecheck` and their `*_and_wait` helpers send one the same way, and take
`idempotency=False` since 3.2 (they always sent one before). On every call an
empty `idempotency_key=""` sends no key (since 3.2 on `review` / `citecheck`
too, which generated one). The batch and
`ask.send` keys are new in 3.0; 2.x sent one there only when you passed it.

The key is never derived from the request, and is random per call: it
protects that call's own retries. Asking the same question again on
`ask.send` is a new call, with a new key, so a new turn of the conversation,
asked and charged again. Pin a key when
your retry means "the same question, once" — the reply, the credit and the
conversation history are then all the first call's:

```python
reply = client.ask.send(
    v.verification_id,
    message="Which source is strongest?",
    idempotency_key="deal-42-followup-1",
)
```

A retry that arrives while the first call with that key is still running is
answered 409 `idempotency_conflict`. The SDK sends the same key and body
again inside the same call, after the wait the server states (or its usual
backoff), within the call's retries; if the first call is still running
after them, it raises that `LenzError` with `retryable=True`. It never mints
a second key to get past it.

`review` and `citecheck` differ here: a 409 `idempotency_conflict` that names
the job (`review_id` / `citecheck_id`) means the first submit with that key
created it, so the call returns it as a `ReviewStarted` / `CitecheckStarted`
(`status` `"queued"`, `.raw` the 409's body, `http_status` `409` and
`settled_by_conflict` `True`, since 3.2; a normal 202 receipt has
`settled_by_conflict` `False`) instead of raising or sending again. Read or wait for it by its id as usual.

Every error of a call that sent a key carries
it as `exc.idempotency_key`: resend with that key, never as a plain new call,
which would send a new key and could run (and charge) the work twice.

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
`"no_match"` and `claims` is empty. The unfocused list is never
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

### Locating claims in your text

Pass `locate=True` to keep only the claims that can be traced directly back
to your text, and to learn where the text makes each one:

```python
out = client.extract(text=draft, locate=True)
for c in out.claims:
    for pos in c.positions or []:
        print(c.claim, "->", pos.text)
        if pos.start is not None:
            assert draft[pos.start : pos.end] == pos.text
```

A claim found nowhere in the text, or found with a different figure, is left
out; if that leaves no claim, `status` is `"not_a_claim"`. Each entry of
`claims` has its `positions` (every place the text makes it, in text order,
at least one and at most 10). Each is a `Position`:
`start` and `end` index the text you sent in Unicode code points, so
`text[start:end]` works natively; `end` is exclusive. Both are `None` when the
input was a URL, since the page is not returned; `pos.text` carries the
passage.

`positions` is `None` when `locate` was not set, or when the claims could not
be located — the list is then returned unfiltered.
Locating adds a few seconds. `locate` defaults to off; leave it `None` to use
the server default, or pass `False` to turn it off explicitly.

On the CLI:

```bash
lenz extract "$(cat draft.txt)" --locate
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
`no`, `fi`, `bg`. To ask for another language, contact us at
https://lenz.io/contact.

### Answer in the language of the text

`language="auto"` on `assess`, `verify` / `verify_and_wait`, `review` /
`review_and_wait`, `extract` and `ask.send` answers in the language of the text you
submitted (on `ask.send`, the language of the claim being discussed; on a review, one
language for the whole draft; on `extract`, for a `text` that is a single URL, the
language of the fetched page). A concrete code always wins, and leaving `language`
out still means English. A short or undetectable text gives English. The other
methods (`verify_batch`, `citecheck`) take the codes above, not `auto`. A review of
a draft that is only a link decides its language once the page is read: until then
its `language` reads `"auto"`, and a page that cannot be read leaves English.

```python
r = client.assess(claim="Die Erde ist flach.", language="auto")
print(r.claims[0].verdict, r.claims[0].language)
# False de
```

`extract` reports the language its claims are written in as `language` on the result
(`None` on a replayed response stored before the API sent it). Pass it on to
`assess` or `verify` to keep a chain in one language, rather than sending `auto`
again on short extracted claims:

```python
out = client.extract(text="Die Erde ist flach.", language="auto")
print(out.language)
# de
r = client.assess(claim=out.claims[0].claim, language=out.language or "")
```

On `assess` with a `claims` list, one language is chosen for the whole request: the
one most items are written in, otherwise English. For a list that mixes languages,
name the code you want instead.

Per-item override on `verify_batch`:

```python
batch = client.verify_batch(
    claims=[
        {"claim": "Coffee causes cancer."},  # en (batch default)
        {"claim": "El café causa cáncer.", "language": "es"},  # overrides
    ],
    language="en",
)
```

## Using Lenz from async code

`AsyncLenz` (since 3.1) is the client for asyncio: the same methods, parameters, defaults,
results and errors as `Lenz`, awaited. It never blocks the event loop.

```python
from lenz_io import AsyncLenz

async with AsyncLenz() as client:  # reads LENZ_API_KEY
    row = (await client.assess(claim="The Great Wall of China is visible from space.")).claims[0]
    review = await client.review_and_wait(draft)
    async for item in client.verifications.iter():
        print(item.verification_id, item.verdict)
```

In a web app, create one `AsyncLenz` in the app's lifespan and close it on shutdown. FastAPI
does not cancel a handler when the caller goes away, so a long wait behind a request runs as a
task that you cancel on `request.is_disconnected()`; with `cancel_on_abort=True` that also
stops the job on the server
([full example](https://github.com/lenzhq/lenz-io-python/blob/main/examples/core/fastapi_async.py)):

```python
import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response
from lenz_io import AsyncLenz


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    async with AsyncLenz() as client:
        app.state.lenz = client
        yield


app = FastAPI(lifespan=lifespan)


@app.post("/review", response_model=None)
async def review(text: str, request: Request) -> dict[str, object] | Response:
    client: AsyncLenz = request.app.state.lenz
    wait = asyncio.ensure_future(client.review_and_wait(text, cancel_on_abort=True))
    try:
        while not wait.done():
            await asyncio.wait({wait}, timeout=1)
            if not wait.done() and await request.is_disconnected():
                wait.cancel()  # cancel_on_abort: the review is asked to stop on the server too
                await asyncio.gather(wait, return_exceptions=True)
                return Response(status_code=499)
    finally:
        wait.cancel()  # no-op once it is done; stops it if this handler is cancelled
    result = wait.result()
    return {"outcome": result.outcome, "issues": [issue.claim for issue in result.issues]}
```

To check many claims at once, send them in one call (`assess(claims=[...])`,
`verify_batch_and_wait`), or fan out with `asyncio.gather` behind a semaphore. A 429 is
retried by the SDK; the semaphore keeps the burst (and the connection pool, 100 connections
by default) in bounds:

```python
limit = asyncio.Semaphore(8)


async def one(claim: str) -> str:
    async with limit:
        return (await client.assess(claim=claim)).claims[0].verdict


verdicts = await asyncio.gather(*(one(c) for c in claims))
```

`verify_batch_and_wait` polls its items one after another, as the sync client does; to poll
items in parallel, `asyncio.gather(*(client.wait(item) for item in batch.items))`.

**Cancellation.** Cancelling the task that awaits a call (a timeout, a caller that left)
raises `CancelledError` and closes the request in flight. It only stops waiting: the
verification, review or citation check keeps running on the server and is charged as usual if
it completes. Unless the wait was called with `cancel_on_abort=True` (`wait`,
`verify_and_wait`, `verify_batch_and_wait`, `review_and_wait`, `citecheck_and_wait`): then
the SDK also sends the matching `cancel` / `cancel_review` / `cancel_citecheck` (one request
per job, at most 5 seconds) and then re-raises the `CancelledError` unchanged. Call
`aclose()` (or leave `async with`) before the event loop ends, so pending cancels go out: a
cancel cut off by the loop closing is logged at WARNING and never sent. A cancelled
verification is not charged; a cancelled review or citation check refunds what it had not
delivered. A cancel that fails, or that loses the race to the end of the job (which is then
charged as usual), is logged at WARNING with the job id. `asyncio.timeout()` and
`asyncio.wait_for()` around a wait are cancellations; the wait's own `timeout=` running out is
not, and never stops the job. `wait(task_id, cancel_on_abort=True)` knows the id up front: a
cancellation already requested when it starts cancels the run without polling. A task
cancelled before it ever runs never enters the call, so nothing is sent.

A submit cancelled after its request left may still have started the job, and a fresh call
would start (and charge) a second one. If you may cancel a submit, pin `idempotency_key=` and
resend with the same key to get the same job back (within 24 hours); to keep the `task_id`
across a cancelled wait, call `verify()` and then `wait()`.

What differs from `Lenz`:

- the methods are coroutines (`iter()` returns an async iterator, `with_options()` stays a
  plain method); close with `await client.aclose()` or `async with` (no `close()`, no `with`);
- `http_client=` takes an `httpx.AsyncClient`;
- `on_progress` / `on_update` may be `async def` (awaited before the next poll);
- the waits take `cancel_on_abort` (above);
- the User-Agent ends `; async)`.

One `AsyncLenz` belongs to one event loop (like the `httpx.AsyncClient` inside it): create it
where it is used, never at import time. asyncio only; trio is not supported. In tests, pass
`http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))` to answer every
request yourself.

For a long check you do not need to wait for, `verify(..., webhook_url=...)` returns at once
and Lenz posts the result to you when it is done (see [Webhooks](#webhooks)).

On lenz-io 3.0 and earlier (no `AsyncLenz`), run the sync client in a worker thread
(`await asyncio.to_thread(client.assess, claim=claim)`); cancelling such a task does not stop
the call.

## Configuration

```python
Lenz(
    api_key="lenz_...",  # or set LENZ_API_KEY env var
    base_url="https://lenz.io/api/v1",  # override for staging / local
    timeout=30.0,  # seconds per request; also None or an httpx.Timeout
    max_retries=3,
    legacy_aliases=True,  # False: results exactly as the API sent them (since 3.2)
)
```

Request strings are sent as well-formed UTF-8: a lone UTF-16 surrogate (half
of a pair, `"\ud800"`) in a claim, a text or a query value goes out as U+FFFD
(`�`), as the Node SDK sends it, instead of raising `UnicodeEncodeError`.
Valid text is sent unchanged.

`AsyncLenz(...)` takes the same arguments, with an `httpx.AsyncClient` as `http_client=`.

Environment variables:

- `LENZ_API_KEY` — read if `api_key=` is not passed (an explicit `api_key=""` or whitespace is no key and never reads it: calls that need a key raise `LenzMissingKeyError`, a `LenzAuthError`)
- `LENZ_BASE_URL` — read if `base_url=` is not passed

So `api_key=None` (or leaving it out) is for scripts: the key comes from the
environment. A server holding several users' keys builds its client with
`api_key=""` (no key, and `LENZ_API_KEY` is never read) and gives each request
its user's key with `with_options(api_key=...)` (see
[the server section](#using-lenz-io-from-a-server-that-forwards-per-user-credentials)).

ASCII whitespace around a key (space, tab, line breaks, form feed, vertical
tab: a trailing newline read from a file or an environment variable) is
dropped silently, with no warning or error (since 3.2); a BOM or a no-break
space is not. What is left is printable ASCII
without spaces: a key with a space, a line break, another control character
or a non-ASCII character inside it raises `LenzInvalidKeyError` (a
`LenzAuthError`) when the client (or the `with_options` copy) is built, before any request (since 3.2; before, it
failed on every call with an encoding or transport error). The message never
contains the key.

**`http_client=`**: your own `httpx.Client` (`httpx.AsyncClient` for
`AsyncLenz`). Its connection pool, proxies and transport are used as they are,
and it is never closed by the SDK. Since 3.2 a `timeout=` passed next to it is
sent on each request (3.1 ignored it), `30.0` (or `lenz_io.client.DEFAULT_TIMEOUT`)
passed explicitly included; left out, the client's own timeout applies. The client itself is never changed.

An OAuth access token for the Lenz API works wherever the API key goes: pass it as `api_key` or in `LENZ_API_KEY`.

### Results exactly as sent: `legacy_aliases=False`

Since 3.0 the SDK reads the API's `2026-10-11` response shape and, by default,
also fills in every deprecated 2.x field from it, so 2.x code runs unchanged.
`legacy_aliases=False` on the constructor (both clients, since 3.2) turns that off:
results carry exactly what the API sent. Read the current names (`claim`,
`status`, `failure`, `more_claims`, `completed_at`, `claims`, `credits`,
`costs`, ...). The default, `True`, is 3.x behaviour, unchanged.

With `legacy_aliases=False`:

| Model | Field | Default (`True`) | `legacy_aliases=False` |
|---|---|---|---|
| `AssessClaim` (failed row) | `verdict` | `"Error"` | `None`, as sent |
| `AssessClaim` (failed row) | `confidence` | `"low"` | `None`, as sent |
| `AssessClaim` | `error_code`, `hint`, `identified_claims` | from `failure`, `more_claims` | `None`, `None`, `[]` |
| `AssessResponse` | `error`, `error_code` | from `failure` | `None`, `""` |
| `ExtractedClaims` | `status` | `no_checkable_claim` read as `"not_a_claim"` | as sent (`"no_checkable_claim"`) |
| `ExtractedClaims` | `claim`, `identified_claims`, `locations` | from `claims` | `""`, `[]`, `None` |
| `CandidateClaim` (picker / needs-input option) | `text` | from `claim` | `""` |
| `TaskAccepted` | `claim_text` | from `claim` | `""` |
| `TaskStatus` | `error`, `failure_reason`, `failure_class`, `docs_url`, `hint` | from `failure` | `""` unless sent |
| `TaskStatus` | `retryable` | from `failure` | `None` unless sent |
| `TaskStatus` (`cancelled`, no block sent) | `failure` | the 2.x cancelled block | `None` |
| `FailureBlock` (every failure block) | `failure_reason` | from `code` | `""` unless sent |
| `Verification`, `VerificationListItem`, `LibraryItem`, `ReviewVerification` | `modified_at` | from `completed_at` | `None` |
| `ReviewAssessment` | `identified_claims`, `error_code`, `hint` | from `more_claims`, `failure` | `[]`, `None`, `None` |
| `ReviewSummary` | `claim_limit_reached`, `citation_limit_reached` | from the `*_exceeded` fields | `None` |
| `CitecheckSummary` | `citation_limit_reached` | from `citation_limit_exceeded` | `None` |
| `Usage` | `verify`, `ask`, `assess` | computed from `credits` and `costs` | `None` unless sent |
| `Usage` | `quota_resets_at` | from `credits.resets_at` | `None` |
| `UsageCredits` | `bonus` | from `extra` | `None` unless sent |
| `UsageCapacity` | `credits` | from `bonus` | `None` unless sent |

**A field sent as `null`** (since 3.2). A few optional fields keep a 3.x
type without `None` (`AssessClaim.verdict` / `confidence` / `error_code` /
`hint`, `AssessResponse.error` / `error_code`, `CandidateClaim.text`,
`EntityRef.name`, `TaskStatus.reason` / `hint` / `progress` / `claims` /
`docs_url` / `error` / `failure_class` / `failure_reason`, `Usage.verify` /
`ask` / `assess`), and the API may send them as `null` (a stored replay, a
value it has none of). That is read as if the field were not sent: its 3.x
default, or `None` with `legacy_aliases=False`. 3.1 raised pydantic's
`ValidationError` there. A required field sent as `null` still raises.

These keep their 3.x annotations (`str`, `int`, `UsageCapacity`, `Progress`,
`list`), so with `legacy_aliases=False` read them as `... | None`: the numeric
`Usage` aliases, and every field the API can send as `null`
(`AssessClaim.verdict` / `confidence` / `error_code` / `hint`,
`AssessResponse.error` / `error_code`, `CandidateClaim.text`,
`EntityRef.name`, `TaskStatus.reason` / `hint` / `progress` / `claims` /
`docs_url` / `error` / `failure_class` / `failure_reason`, `Usage.verify` /
`ask` / `assess`). With the default `legacy_aliases=True` a failed `assess`
row with a `null` verdict reads `verdict == "Error"` and `confidence == "low"`,
as in 2.x. The deprecated attributes still exist on the models.
A field the response leaves out still reads its declared default, as in every
3.x release. `model_dump(exclude_unset=True)` is the body as sent (the
`Usage` aliases that read `None` are not marked set). Errors (classes and
fields) are the same either way, except `exc.code`, which is exactly the
`code` of the response body (since 3.2; see [Errors](#errors)). Webhook
parsing is the same either way, and so is
`BatchItemResult.claim_text`, which the SDK builds itself. Results attached to
an error are results: the `partial` of a `ReviewTimeout` / `CitecheckTimeout`
and the `review` / `citecheck` of a `ReviewFailed` / `CitecheckFailed` are read
with the client's setting, so with `legacy_aliases=False` they carry the body
as sent.

`timeout` must be a number of seconds greater than 0 and at most 2,147,483, `None` (no timeout), an
`httpx.Timeout` or httpx's `(connect, read, write, pool)` tuple; `max_retries` a
whole number, 0 or more. Anything else raises `ValueError` when the client is
built.

### Per-call options

Every method takes three keyword-only request options, for that call only:

```python
client.assess(claim="...", timeout=20)  # one HTTP attempt, in seconds
client.usage(max_retries=0)  # no retries for this call
client.verify("...", extra_headers={"X-Trace-Id": trace})  # added to the request
```

- `timeout`: one HTTP attempt, a number of seconds greater than 0 or an
  `httpx.Timeout`. httpx applies it per phase (connect, read, write, pool), and
  the read limit to each chunk of the answer: it limits inactivity, not the
  whole call, and each retry gets its own.
- `max_retries`: how often a request that failed in a way worth retrying (a
  5xx, a 429, a dropped connection) is sent again: a whole number, 0 or more.
- `extra_headers`: headers added to the request. Names are header tokens and
  values visible ASCII, with spaces and tabs allowed inside but not at either
  end (an empty value is fine); anything else raises `ValueError`. The SDK's own headers are
  refused (`X-Lenz-API-Version`, `Idempotency-Key`, `Authorization`,
  `Content-Type`, `Content-Length`, `Host`, `Transfer-Encoding`): use
  `idempotency_key=` and `api_key=` for the first two. A header with the name
  of a default one (`User-Agent`, `Accept`), in any casing, replaces the
  default instead of being sent next to it.

What each option reaches:

| Methods | `timeout` | `max_retries` | `extra_headers` |
|---|---|---|---|
| Plain calls (`verify`, `review`, `get_status`, `cancel`, `usage`, `verifications.*`, `ask.*`, `library.list`, ...) | the attempt | the call's retries | every request |
| `extract`, `assess` | the attempt, used as given (so is a copy's; only the client's own is raised to 150 s / 100 s) | the call's retries | every request |
| Waits (`wait`, `verify_and_wait`, `verify_batch_and_wait`, `review_and_wait`, `citecheck_and_wait`) | **how long to wait** (unchanged) | the submit's retries (`wait` has none: each poll is one request) | the submit and every poll |
| `verifications.iter`, `library.iter` | each page's attempt | each page's retries | every page |
| `with_options` | the default attempt timeout of the copy (also what each poll of a wait uses, capped by what is left of the wait) | the copy's default for plain calls and submits; never a wait's polls, which are one attempt each | added to every request of the copy |

A bad value raises `ValueError` before anything is sent (for the iterators,
when the iterator is created). Precedence, per option: the call's keyword, then
the copy's (`with_options`), then the client's; headers merge, the call's over
the copy's.

`extract` and `assess` wait at least 150 s and 100 s when the timeout is the
client's own (the constructor's, or the `http_client`'s). A timeout passed to
the call, or set on a copy with `with_options(timeout=...)`, is used as given,
even below that. **An explicit timeout under 100 s (`assess`) or 150 s
(`extract`) can end a call the server is still running, and charging for**:
resend it with the same `idempotency_key` (`exc.idempotency_key`) to get its
answer instead of paying again. (3.1 and earlier raised a copy's timeout to
the minimum too.)

`None` means different things in two places:

| | `None` | not passed |
|---|---|---|
| `timeout=` on a call | the copy's or the client's timeout | the same |
| `with_options(timeout=...)`, `Lenz(timeout=...)` | no timeout | keep the current one (the client default is 30 s) |
| `extra_headers={"X-A": None}` | removes `X-A` added by a copy | — |

For no timeout on one call, pass `timeout=httpx.Timeout(None)`.

### A client with other defaults: `with_options`

`client.with_options(...)` returns a copy with its own defaults, sharing the
connection pool, key and base URL. It is cheap, so you can make one per request
or per job, and the client it was made from does not change:

```python
fast = client.with_options(timeout=10, max_retries=0)
fast.assess(claim="...")

for job in jobs:
    scoped = client.with_options(extra_headers={"X-Job-Id": job.id})
    scoped.review_and_wait(job.draft)
```

A copy of a copy starts from the copy's options (timeout, retries and
headers), and changes only what it is given. The pool belongs to the
client that created it: `close()` and `with` on a copy do nothing, closing the
original closes the pool for every copy (a copy then raises httpx's
closed-client error), and a client given `http_client=` never closes it. A
copy is as safe to share across threads as the client. The copy is shallow:
attributes a subclass of `Lenz` adds are shared with the client it was made
from.

**One key per copy** (since 3.2). `with_options(api_key=...)` gives the copy
its own key on the same pool, for a server that acts for several users, each
with their own Lenz API key or OAuth access token (`lat_...`):

```python
shared = Lenz(api_key="")  # one pool for the process, no key of its own


def handle(request):
    user = shared.with_options(api_key=request.user_token)
    return user.assess(claim=request.text)
```

Any string is sent as given. An empty or whitespace-only key, or `None`, gives
a copy with no key: a call that needs one raises `LenzMissingKeyError` (a `LenzAuthError`) before
anything is sent. A copy never reads `LENZ_API_KEY`, and leaving `api_key` out
keeps the key the copy was made from. Copies with different keys can run at
the same time on one pool (threads, or tasks on `AsyncLenz`). `legacy_aliases`
is set on the constructor only: a copy reads results the way its client does,
and `with_options(legacy_aliases=...)` raises `TypeError`.

## Using lenz-io from a server that forwards per-user credentials

A gateway, an MCP server or any backend that calls Lenz on behalf of its own
users, each with their own Lenz API key or OAuth access token:

```python
from lenz_io import Lenz, LenzError

# One client, one pool, for the process. No key of its own, and
# LENZ_API_KEY is never read.
lenz = Lenz(
    api_key="",
    legacy_aliases=False,  # results and error codes exactly as sent
    max_retries=0,  # you own the retry budget
    user_agent="my-gateway/1.4",
)


def handle(user, text):
    client = lenz.with_options(api_key=user.lenz_token)  # no short timeout: assess can take 100 s
    try:
        out = client.assess(claim=text)
    except LenzError as exc:
        return {"error": exc.code, "status": exc.status_code, "body": exc.body}
    return out.raw  # the API's JSON object, as received
```

- **Per-user keys**: `with_options(api_key=...)` per request, on the shared
  pool. A copy never reads `LENZ_API_KEY`; an empty key, or `None`, gives a
  copy with no key, and a call that needs one raises `LenzMissingKeyError` (a `LenzAuthError`) before
  sending. ASCII whitespace around a key is dropped silently; a key with a
  space, a control or a non-ASCII character inside it raises
  `LenzInvalidKeyError` (a `LenzAuthError`) there, before sending. Copies with different keys can run
  at once (threads, or tasks on `AsyncLenz`).
- **`legacy_aliases=False`** is a constructor argument only: every copy
  reads like its client. Results carry what the API sent, and `exc.code` is
  the body's `code`.
- **User-Agent**, highest first: a per-call (or copy's)
  `extra_headers={"User-Agent": ...}`, then the constructor's `user_agent=`,
  then the User-Agent your `http_client=` set itself, then the SDK's
  (`lenz-io-python/<version> (...)`).
- **Reserved headers**: `Authorization` (from the key), `Idempotency-Key`
  (`idempotency_key=` / `idempotency=`), `Content-Type` (on a request with a
  body only, since 3.2) and `X-Lenz-API-Version` are set by the SDK, and
  `extra_headers` refuses them (with `Content-Length`, `Host` and
  `Transfer-Encoding`), in any casing.
- **`Accept`**: a client the SDK creates sends `Accept: application/json`. A
  borrowed `http_client=` sends its own default (httpx's is `*/*`) unless you
  set it on that client or pass `extra_headers={"Accept": "application/json"}`.
- **Timeouts**: a timeout on the copy or the call is used as given, also on
  `assess` and `extract` (only the client's own timeout is raised to their
  100 s / 150 s minimum). Below that minimum it can end a call the server is
  still running, and charging for: resend with `exc.idempotency_key` to get
  its answer. A `timeout=` given with `http_client=` is sent on each request.
- **Retries**: `max_retries=0` when your caller has its own deadline or
  retry budget; the SDK then sends each request once. A resend should reuse
  `exc.idempotency_key`.
- **Raw bodies**: `result.raw` is the JSON object a result was read from
  (nested results too; the Node SDK sets it on top-level results only),
  and `result.http_status` / `result.headers` the answer's status and
  headers (top-level results only); `exc.body` is an error's; `exc.headers` its response headers
  (`retry-after`, `x-request-id`, ...), and `exc.retry_after` the parsed wait
  (see [Errors](#errors) for which wins).
- **Errors as text**: `str(exc)` is the message plus `Cause:`, `Fix:`,
  `Docs:` and `Request ID:` lines; the SDK never puts the key in it, but a
  422's text can quote the input. For a structured answer, forward `code`, `status_code`, `retryable`,
  `retry_after` and `request_id` instead.

## Compatibility

- Python 3.10, 3.11, 3.12
- Works in CI/CD (no interactive prompts, no global state)
- Mockable for tests: every HTTP call goes through `httpx`; use `respx` or
  inject your own `httpx.Client` via `Lenz(..., http_client=...)` (an
  `httpx.AsyncClient` for `AsyncLenz`)
- `AsyncLenz`: asyncio (not trio)

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
