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
print(row.verdict, row.confidence)  # e.g. False high  (about 15-20 s, 1 credit)
```

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

Each verdict row also carries two optional notes. `rationale` is the
reasoning of a reviewer who agrees with the panel's verdict; `dissent`, when
set, is the reasoning of the reviewer farthest from it. Both are reviewers'
notes, not checked sources; for sourced evidence, call `verify`. Read them as
optional: either can be `None`.

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
- **`client.verifications.{list,iter,get,delete,related}(...)`** → manage past verifications. `iter()` walks every page lazily (`for item in client.verifications.iter(): ...`). All API claims are private; reference them by `verification_id`. Cache-hit on another customer's claim is transparent — you always see your own `verification_id`, never another customer's.
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
The body exactly as sent is `exc.body` on an error and `event.raw` on a
webhook event.

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

Local argument mistakes (an empty id, two exclusive arguments) raise
`ValueError`, never a `LenzError`.

A `*_and_wait` helper that reaches its own `timeout` raises `LenzTimeoutError`
(`ReviewTimeout`, `CitecheckTimeout`): the job keeps running server-side, so
read it later by its id rather than resubmitting. A poll answered 401, 403 or
404 (or in another API version) ends the wait at once with that error; in
`verify_batch_and_wait` a 404 or a version error fails that item only (the
others keep going), while a 401 / 403 raises. A 5xx, a 429 or a network
failure is polled again. Each poll is bounded by what is left of the
`timeout` (each phase at most the client's own), and no poll starts once it
is spent; `timeout=0` reads each status once, as in 2.x. `ReviewFailed`, `ReviewTimeout`, `CitecheckFailed` and
`CitecheckTimeout` are also importable as `ReviewFailedError`,
`ReviewTimeoutError`, `CitecheckFailedError` and `CitecheckTimeoutError` (the
same classes).

`LenzApiVersionError` (a `LenzError`) is raised when a response names an API
version other than the one this SDK reads. Every response carries the version
that served it in `X-Lenz-API-Version`; lenz-io 3.x asks for `2026-10-11` and
reads that version's shape only, so an answer in `2026-05-13` (a server still
on the older version, or a reply replayed from an idempotent request stored
before the change) is refused rather than misread. It carries `api_version`
(what the response named), `status_code` and `body` as sent. If it persists,
contact support with the request id; lenz-io 2.x reads both versions. A response without the header is read as usual, and webhook
payloads are never refused (they are parsed in either shape).

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
    print(exc.api_version)  # "2026-05-13"
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
result = client.cancel("tsk_abc123")            # a verification -> CancelResult
if result.cancelled:
    print("stopped:", result.status)             # "cancelled"
else:
    print("already ended:", result.status)       # "completed" or "failed"

review = client.cancel_review("d6b2bd72")        # the full view, like get_review
print(review.status, review.credits.charged)     # "cancelled", what it cost

check = client.cancel_citecheck("12bbbf65")      # like get_citecheck
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
  retried on a 5xx or a dropped connection, like any call that is safe to
  repeat.
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
from another process replay), or `idempotency=False` to opt out. `review` and
`citecheck` always send one (pin it with `idempotency_key=`). The batch and
`ask.send` keys are new in 3.0; 2.x sent one there only when you passed it.

The key is never derived from the request: asking the same question again on
`ask.send` is a new call, with a new key, and is asked again. Pin a key when
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
a second key to get past it. Every error of a call that sent a key carries
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

`language="auto"` on `assess`, `verify` / `verify_and_wait` and `ask.send` answers
in the language of the text you submitted (on `ask.send`, the language of the claim
being discussed). A concrete code always wins, and leaving `language` out still
means English. The other methods (`extract`, `verify_batch`, `citecheck`, `review`)
take the codes above, not `auto`.

```python
r = client.assess(claim="Die Erde ist flach.", language="auto")
print(r.claims[0].verdict, r.claims[0].language)
# False de
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

The client is synchronous. Lenz calls take a while (`assess` about 15 s, a deep check about
90 s, a review a few minutes), so calling one directly inside an `async def` blocks the event
loop for that long: in a FastAPI or aiohttp server, every other request on that worker
waits. Run the call in a worker thread instead:

```python
import asyncio
from lenz_io import Lenz

client = Lenz()  # one client for the whole app; it is safe to share across threads

async def quick_check(claim: str) -> str:
    out = await asyncio.to_thread(client.assess, claim=claim)
    return out.claims[0].verdict
```

The same applies to every method, and above all to the ones that wait (`verify_and_wait`,
`verify_batch_and_wait`, `review_and_wait`, `citecheck_and_wait`, `wait`): never call them
directly inside an async handler. In FastAPI, a plain `def` route runs in the thread pool
already:

```python
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from fastapi import FastAPI
from lenz_io import Lenz

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.lenz = Lenz()
    yield
    app.state.lenz.close()

app = FastAPI(lifespan=lifespan)

@app.post("/check")
def check(claim: str) -> dict[str, object]:  # `def`, not `async def`: FastAPI runs it in a thread
    v = app.state.lenz.verify_and_wait(claim)
    return {"verdict": v.verdict, "score": v.lenz_score}
```

Cancelling the asyncio task that awaits `asyncio.to_thread(...)` does not stop the call: the
thread runs on until the call returns. Bound each request with `timeout=` (see
[Per-call options](#per-call-options)), and stop paid work on the server with `cancel()`,
`cancel_review()` or `cancel_citecheck()` (see [Stopping a run](#stopping-a-run)). A native
async client is planned.

For a long check behind a web request, `verify(..., webhook_url=...)` returns at once and
Lenz posts the result to you when it is done (see [Webhooks](#webhooks)). That ties up no
thread for the wait. To check many claims at once, send them in one call
(`assess(claims=[...])`, `verify_batch`) rather than one thread per claim.

## Configuration

```python
Lenz(
    api_key="lenz_...",  # or set LENZ_API_KEY env var
    base_url="https://lenz.io/api/v1",  # override for staging / local
    timeout=30.0,  # seconds per request; also None or an httpx.Timeout
    max_retries=3,
)
```

Environment variables:

- `LENZ_API_KEY` — read if `api_key=` is not passed
- `LENZ_BASE_URL` — read if `base_url=` is not passed

An OAuth access token for the Lenz API works wherever the API key goes: pass it as `api_key` or in `LENZ_API_KEY`.

`timeout` must be a number of seconds greater than 0, `None` (no timeout) or an
`httpx.Timeout`; `max_retries` a whole number, 0 or more. Anything else raises
`ValueError` when the client is built.

### Per-call options

Every method takes three keyword-only request options, for that call only:

```python
client.assess(claim="...", timeout=20)                    # one HTTP attempt, in seconds
client.usage(max_retries=0)                               # no retries for this call
client.verify("...", extra_headers={"X-Trace-Id": trace})  # added to the request
```

- `timeout`: one HTTP attempt, a number of seconds greater than 0 or an
  `httpx.Timeout`. httpx applies it per phase (connect, read, write, pool), and
  the read limit to each chunk of the answer: it limits inactivity, not the
  whole call, and each retry gets its own.
- `max_retries`: how often a request that failed in a way worth retrying (a
  5xx, a 429, a dropped connection) is sent again: a whole number, 0 or more.
- `extra_headers`: headers added to the request. The SDK's own headers are
  refused (`X-Lenz-API-Version`, `Idempotency-Key`, `Authorization`,
  `Content-Type`, `Content-Length`, `Host`, `Transfer-Encoding`): use
  `idempotency_key=` and `api_key=` for the first two. A header with the name
  of a default one (`User-Agent`, `Accept`) replaces it.

What each option reaches:

| Methods | `timeout` | `max_retries` | `extra_headers` |
|---|---|---|---|
| Plain calls (`verify`, `review`, `get_status`, `cancel`, `usage`, `verifications.*`, `ask.*`, `library.list`, ...) | the attempt | the call's retries | every request |
| `extract`, `assess` | the attempt, used as given | the call's retries | every request |
| Waits (`wait`, `verify_and_wait`, `verify_batch_and_wait`, `review_and_wait`, `citecheck_and_wait`) | **how long to wait** (unchanged) | the submit's retries (`wait` has none: each poll is one request) | the submit and every poll |
| `verifications.iter`, `library.iter` | each page's attempt | each page's retries | every page |
| `with_options` | the default attempt timeout of the copy (also what each poll of a wait uses, capped by what is left of the wait) | the copy's default | added to every request of the copy |

A bad value raises `ValueError` before anything is sent (for the iterators,
when the iterator is created). Precedence, per option: the call's keyword, then
the copy's (`with_options`), then the client's; headers merge, the call's over
the copy's.

`extract` and `assess` wait at least 150 s and 100 s when the timeout comes
from a copy or the client, as they always did. A timeout passed to the call is
used as given, even below that: it can time out a call the server is still
running, so retry it with the same `idempotency_key` to get its answer.

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

A copy of a copy starts from the copy's options. The pool belongs to the
client that created it: `close()` and `with` on a copy do nothing, closing the
original closes the pool for every copy (a copy then raises httpx's
closed-client error), and a client given `http_client=` never closes it. A
copy is as safe to share across threads as the client.

## Compatibility

- Python 3.10, 3.11, 3.12
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
