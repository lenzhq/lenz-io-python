---
name: lenz-api
description: >-
  Verify factual claims, quotes, and citations in text, documents, or URLs using the Lenz Fact-Checking API (/extract, /assess, /verify, /citecheck, /review, /ask) and the official lenz-io Python and TypeScript/Node.js SDKs. Use when an agent needs to fact-check statements, evaluate draft accuracy, check source citations (URLs, DOIs, legal citations), or integrate automated verification into research, due-diligence, or document-generation workflows.
---

# Lenz Fact-Checking API (`lenz-api`)

Lenz checks the facts, figures, and citations in your work so you can sign off or publish with confidence: a quick verdict, or a deep check with sources and reasoning.

## Six API Calls

1. **`extract`** (`POST /api/v1/extract`, `POST /api/v1/extract/file`): Pull verifiable factual claims from text, public URLs (web pages, Google Docs, YouTube, direct PDFs/images), or uploaded files (`.docx`, `.pdf`, `.png`, `.jpg`, `.webp`). Free (1,000 calls/account/day).
2. **`assess`** (`POST /api/v1/assess`): Fast 3-model panel verdict on 1 to 20 claims in about 15 seconds (1 credit/claim). Returns `verdict`, `confidence`, `rationale`, `searched_pages`, and `suggested_rewrite` (when `suggest_rewrite=True`).
3. **`verify`** (`POST /api/v1/verify`, `POST /api/v1/verify/batch`): Full multi-model verification pipeline with independent web research and citations in about a minute or two (10 credits standard / 5 credits at `depth="low"`). Returns `verdict`, `lenz_score` (1–10 or `null`), `confidence`, `key_finding`, `executive_summary`, and `sources`.
4. **`citecheck`** (`POST /api/v1/citecheck`): Standalone citation verification. Checks whether cited sources (`url`, `doi`, or legal `cite`) exist and support what the draft says (1 credit/checked citation).
5. **`review`** (`POST /api/v1/review`, `POST /api/v1/review/file`): Full document review in one async call (2–4 minutes). Extracts claims, runs quick `assess` verdicts, escalates doubtful claims to deep `verify`, and optionally checks citations (`max_citations`). Supports annotated `.docx` export (`GET /api/v1/reviews/{review_id}/export?format=docx`).
6. **`ask`** (`POST /api/v1/ask/{verification_id}`): Grounded follow-up questions on a completed verification (1 credit).

## Authentication & Installation

Set `LENZ_API_KEY` (`lenz_...`, issued at [https://lenz.io/api-credentials](https://lenz.io/api-credentials)):

```bash
export LENZ_API_KEY=lenz_...
pip install lenz-io           # Python SDK
pipx install "lenz-io[cli]"   # CLI
npm install lenz-io           # TypeScript / Node.js SDK
```

---

## Code Examples

### 1. Fast Assessment (`/assess`)

#### Python
```python
from lenz_io import Lenz

client = Lenz()  # reads LENZ_API_KEY

res = client.assess(claim="The Great Wall of China is visible from space.")
for row in res.claims:
    if row.status == "failed":
        print(row.failure.code, row.failure.hint)
    else:
        print(row.verdict, row.confidence)  # e.g. False high
        if row.rationale:
            print(row.rationale)
```

#### TypeScript / Node.js
```ts
import { Lenz } from "lenz-io";

const client = new Lenz(); // reads LENZ_API_KEY

const { claims } = await client.assess({
  claim: "The Great Wall of China is visible from space.",
});
for (const row of claims) {
  if (row.status === "failed") {
    console.log(row.failure?.code, row.failure?.hint);
  } else {
    console.log(row.verdict, row.confidence); // e.g. False high
    if (row.rationale) console.log(row.rationale);
  }
}
```

#### REST
```bash
curl -X POST https://lenz.io/api/v1/assess \
  -H "Authorization: Bearer $LENZ_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"claim": "The Great Wall of China is visible from space."}'
```

---

### 2. Deep Sourced Verification (`/verify`)

#### Python
```python
from lenz_io import Lenz

client = Lenz()

v = client.verify_and_wait(claim="Voyager 1 was launched in 1977.")
print(v.verdict, v.lenz_score, v.confidence)  # True 10 high (lenz_score is None on "No Public Record")
print(v.key_finding)
for s in v.sources[:3]:
    print("-", s.title, s.url)
```

#### TypeScript / Node.js
```ts
import { Lenz } from "lenz-io";

const client = new Lenz();

const v = await client.verifyAndWait({ claim: "Voyager 1 was launched in 1977." });
console.log(v.verdict, v.lenz_score, v.confidence); // True 10 high (lenz_score is null on "No Public Record")
console.log(v.key_finding);
for (const s of (v.sources ?? []).slice(0, 3)) {
  console.log("-", s.title, s.url);
}
```

#### REST
```bash
TASK_ID=$(curl -s -X POST https://lenz.io/api/v1/verify \
  -H "Authorization: Bearer $LENZ_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"claim": "Voyager 1 was launched in 1977."}' | jq -r .task_id)

curl -s "https://lenz.io/api/v1/verify/status/$TASK_ID" \
  -H "Authorization: Bearer $LENZ_API_KEY"
```

---

### 3. Review a Draft (`/review`, `/review/file`)

#### Python
```python
from lenz_io import Lenz

client = Lenz()
draft = "The EU AI Act entered into force on 1 August 2024. Fines reach 7% of global annual turnover."

review = client.review_and_wait(
    text=draft,
    max_citations=20,
    suggest_edits=True,
)
print(review.outcome)  # clean | issues_found | incomplete | unchecked
for issue in review.issues:
    print(issue.verdict, issue.confidence, issue.claim)
    if issue.suggested_rewrite:
        print("  Suggested rewrite:", issue.suggested_rewrite)

for c in review.citation_issues:
    print(c.finding, c.cited_url or c.doi, c.statement)

# File upload (.docx, .pdf, .png, .jpg, .webp) and annotated .docx export:
# review_doc = client.review_file_and_wait("report.docx")
# export = client.export_review(review_doc.review_id, format="docx")
```

#### TypeScript / Node.js
```ts
import { Lenz } from "lenz-io";

const client = new Lenz();
const draft = "The EU AI Act entered into force on 1 August 2024. Fines reach 7% of global annual turnover.";

const review = await client.reviewAndWait({
  text: draft,
  maxCitations: 20,
  suggestEdits: true,
});
console.log(review.outcome); // clean | issues_found | incomplete | unchecked
for (const issue of review.issues) {
  console.log(issue.verdict, issue.confidence, issue.claim);
  if (issue.suggested_rewrite) {
    console.log("  Suggested rewrite:", issue.suggested_rewrite);
  }
}
for (const c of review.citation_issues) {
  console.log(c.finding, c.cited_url ?? c.doi, c.statement);
}
```

#### REST
```bash
curl -X POST https://lenz.io/api/v1/review \
  -H "Authorization: Bearer $LENZ_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"text": "Draft text...", "escalate": {"max_citations": 20, "suggest_edits": true}}'
```

---

### 4. Standalone Citation Check (`/citecheck`)

#### Python
```python
from lenz_io import Lenz

client = Lenz()

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
print(check.outcome)  # clean | issues_found | incomplete | unchecked
for c in check.citation_issues:
    print(c.finding, c.cited_url or c.doi, c.statement)
```

#### TypeScript / Node.js
```ts
import { Lenz } from "lenz-io";

const client = new Lenz();

const check = await client.citecheckAndWait({
  pairs: [
    {
      statement: "Water boils at 100 degrees Celsius at sea level.",
      url: "https://en.wikipedia.org/wiki/Boiling_point",
    },
    {
      statement: "Diamond sensors can measure temperature in a living cell.",
      doi: "10.1038/nature12373",
      citedYear: "2013",
    },
  ],
});
console.log(check.outcome);
for (const c of check.citation_issues) {
  console.log(c.finding, c.cited_url ?? c.doi, c.statement);
}
```

---

### 5. Extract Claims (`/extract`, `/extract/file`) & Ask Follow-Ups (`/ask`)

#### Python
```python
from lenz_io import Lenz

client = Lenz()
draft = "Acme Corp announced Q3 earnings of $12M, beating expectations."

extracted = client.extract(text=draft, focus="financial figures", locate=True)
for item in extracted.claims:
    print(item.claim, item.positions)

v = client.verify_and_wait(claim="Voyager 1 was launched in 1977.")
reply = client.ask.send(v.verification_id, message="Which source is strongest?")
print(reply.content)
```

#### TypeScript / Node.js
```ts
import { Lenz } from "lenz-io";

const client = new Lenz();
const draft = "Acme Corp announced Q3 earnings of $12M, beating expectations.";

const extracted = await client.extract({ text: draft, focus: "financial figures", locate: true });
for (const item of extracted.claims ?? []) {
  console.log(item.claim, item.positions);
}

const v = await client.verifyAndWait({ claim: "Voyager 1 was launched in 1977." });
if (v.verification_id) {
  const reply = await client.ask.send(v.verification_id, {
    message: "Which source is strongest?",
  });
  console.log(reply.content);
}
```

---

## Key Semantics

1. **`"No Public Record"` verdict**:
   - Verdict labels are `True`, `Mostly True`, `Mixed`, `Mostly False`, `False`, or `No Public Record`.
   - When `verdict == "No Public Record"`, `lenz_score` is `None` (Python) / `null` (JSON/TypeScript). No public record in the evidence settles the claim — it is neither confirmed nor refuted, so never treat it as false.
2. **Uncheckable inputs (`no_checkable_claim`)**:
   - Inputs with no factual claim (pure opinion, prediction, greeting) return `status: "failed"` with `failure.code == "no_checkable_claim"` and cost 0 credits.
3. **Supplied reference documents (`supplied_sources`)**:
   - Pass up to 50 reference documents (up to 500,000 total characters, `supplied_sources=[{"title": "...", "text": "..."}]`) on `/verify`, `/verify/batch`, `/review`, `/review/file`, or `/citecheck` (or per-pair `source_text` on `/citecheck`) to verify claims against private reference text alongside live web and registry sources.

## Developer Documentation (Markdown & OpenAPI)

- **Overview**: [https://lenz.io/developers.md](https://lenz.io/developers.md)
- **Quickstart**: [https://lenz.io/docs/quickstart.md](https://lenz.io/docs/quickstart.md)
- **Going to Production**: [https://lenz.io/docs/production.md](https://lenz.io/docs/production.md)
- **Error Reference**: [https://lenz.io/docs/errors.md](https://lenz.io/docs/errors.md)
- **Citation Checks**: [https://lenz.io/docs/citations.md](https://lenz.io/docs/citations.md)
- **Covered Verification**: [https://lenz.io/docs/coverage.md](https://lenz.io/docs/coverage.md)
- **OpenAPI 3.1 Spec**: [https://lenz.io/api/v1/openapi.json](https://lenz.io/api/v1/openapi.json)
