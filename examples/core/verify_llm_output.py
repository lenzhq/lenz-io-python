"""Extract claims from an LLM response and verify the ones that matter.

The headline integration story: your model emits an answer, Lenz pulls the
verifiable claims out of it, then you verify the ones you care about and
surface, suppress, or flag the output.

Run:
    export LENZ_API_KEY=lenz_...
    python examples/core/verify_llm_output.py
"""

from __future__ import annotations

import os

from lenz_io import Lenz

LLM_OUTPUT = """
The Eiffel Tower was completed in 1889 and stands 330 meters tall.
It was originally intended to be a temporary structure for the World's Fair.
Today it receives roughly 7 million visitors per year.
"""


def main() -> None:
    client = Lenz(api_key=os.environ.get("LENZ_API_KEY"))

    # Step 1: extract the verifiable claims (free; ~3s)
    extracted = client.extract(text=LLM_OUTPUT)
    print(f"Extracted {len(extracted.identified_claims)} claims:")
    for c in extracted.identified_claims:
        print(f"  - {c}")
    print()

    # Step 2: verify each one. In production, fan-out with verify_batch.
    for claim_text in extracted.identified_claims:
        v = client.verify_and_wait(claim=claim_text, timeout=120)
        print(f"{v.verdict.label.upper():14}  {claim_text}")
        if v.verdict.label in ("false", "mostly_false") and v.sources:
            print(f"  ↳ {v.sources[0].title}")
            print(f"    {v.sources[0].url}")


if __name__ == "__main__":
    main()
