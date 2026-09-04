"""Lenz quickstart — the canonical four-primitive integration.

Run:
    export LENZ_API_KEY=lenz_...
    python examples/core/quickstart.py

The pattern: ``extract`` pulls claims out of any text, ONE ``assess`` call
over those claims returns a fast 3-model verdict per claim (up to 20, one
row per claim, same order), ``verify`` escalates the low-confidence rows
to the full multi-model panel with citations, and ``ask`` lets you follow up
on a verification.

The demo claim is pre-cached, so the verify call returns in ~1.5s. Your
own claims hit the full pipeline (~60-90s) — use webhooks for production
async flows.
"""

from __future__ import annotations

import os

from lenz_io import Lenz


def main() -> None:
    client = Lenz(api_key=os.environ.get("LENZ_API_KEY"))

    # 1. extract — pull verifiable claims out of any text (free)
    out = client.extract(text="Sharks don't get cancer. The Eiffel Tower is 330m tall.")
    claims = out.identified_claims or [out.claim]
    print(f"Extracted {len(claims)} claims:")
    for c in claims:
        print(f"  - {c}")
    print()

    # 2. assess — one call over the extracted claims, one row per claim (sync)
    quick = client.assess(claims=claims).claims
    for c in quick:
        print(f"  {c.verdict:<12}  conf={c.confidence:<7}  {c.claim}")
        if c.verdict == "Error":
            # No verdict for this item — error_code says why, hint says what to send next.
            print(f"    {c.error_code}: {c.hint}")
        if c.identified_claims:
            # A compound item: only its main claim was assessed.
            print(f"    also found (not assessed): {c.identified_claims}")
    print()

    # 3. verify — escalate the low-confidence rows to the full multi-model panel
    doubtful = [{"claim": c.claim} for c in quick if c.verdict != "Error" and c.confidence == "low"]
    # The demo claim is pre-cached; verify it explicitly so the walkthrough
    # always reaches steps 3 and 4 even when every row came back confident.
    doubtful = doubtful or [{"claim": "Sharks don't get cancer"}]
    results = client.verify_batch_and_wait(claims=doubtful)
    v = next(r.verification for r in results if r.verification is not None)
    print(f"Verdict: {v.verdict} (lenz_score {v.lenz_score}, confidence {v.confidence})")
    print(f"Summary: {v.executive_summary}")
    print()
    print("Top sources:")
    for source in v.sources[:3]:
        print(f"  - {source.title}")
        print(f"    {source.url}")

    # 4. ask — follow-up question on the verification
    reply = client.ask.send(v.verification_id, message="Which source is strongest?")
    print()
    print("Q: Which source is strongest?")
    print(f"A: {reply.reply}")


if __name__ == "__main__":
    main()
