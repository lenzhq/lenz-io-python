"""Lenz quickstart — verify a single claim and print the verdict.

Run:
    export LENZ_API_KEY=lenz_...
    python examples/core/quickstart.py

Expected output:
    Verdict: false (score 2.0, confidence 0.92)
    Top sources:
     - National Cancer Institute …
     - …

This example uses a pre-cached claim, so the call returns in ~1.5s.
Verify your own text and the full pipeline runs (~60-90s).
"""

from __future__ import annotations

import os

from lenz_io import Lenz


def main() -> None:
    client = Lenz(api_key=os.environ.get("LENZ_API_KEY"))

    v = client.verify_and_wait(claim="Sharks don't get cancer")

    label, score, conf = v.verdict.label, v.verdict.score, v.verdict.confidence
    print(f"Verdict: {label} (score {score}, confidence {conf})")
    print()
    print(f"Claim: {v.claim}")
    print(f"Summary: {v.executive_summary}")
    print()
    print("Top sources:")
    for source in v.sources[:3]:
        print(f"  - {source.title}")
        print(f"    {source.url}")


if __name__ == "__main__":
    main()
