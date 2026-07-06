"""Lenz quickstart — the canonical four-primitive integration.

Run:
    export LENZ_API_KEY=lenz_...
    python examples/core/quickstart.py

The pattern: ``extract`` pulls claims out of any text, ``assess`` returns
a fast 3-model verdict on each, ``verify`` escalates the low-confidence
ones to the full 8-model panel with citations, and ``ask`` lets you
follow up on a verification.

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
    print(f"Extracted {len(out.identified_claims)} claims:")
    for c in out.identified_claims:
        print(f"  - {c}")
    print()

    # 2. assess — fast 3-model verdict on each (~5-10s, sync)
    quick = client.assess(text="Sharks don't get cancer")
    for c in quick.claims:
        print(f"  {c.verdict:<12}  conf={c.confidence:<7}  {c.claim}")
    print()

    # 3. verify — escalate to the full 8-model panel for citations + audit
    v = client.verify_and_wait(claim="Sharks don't get cancer")
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
