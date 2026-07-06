"""Extract claims from an LLM response, assess them, escalate the doubtful ones.

The headline integration story: your model emits an answer, Lenz pulls
the verifiable claims out of it (``extract``), gives you a fast verdict
on each (``assess``), and you escalate only the low-confidence ones to
the full 8-model pipeline (``verify``). Cheaper and faster than
verifying every claim outright.

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

    # Step 1: assess everything in one sync call (~5-10s for the whole batch)
    # ``/assess`` runs framing internally, so passing the raw LLM output
    # is equivalent to ``extract`` -> per-claim ``assess`` but in one trip.
    quick = client.assess(text=LLM_OUTPUT)
    print(f"Assessed {len(quick.claims)} claims:\n")
    for c in quick.claims:
        print(f"  {c.verdict:<12}  conf={c.confidence:<7}  {c.claim}")
    print()

    # Step 2: escalate low-confidence claims to the full pipeline.
    # ``assess`` and ``verify`` share a result cache server-side, so a
    # claim that already has a deep verification surfaces immediately
    # via ``verification_url`` and you can skip the escalation.
    doubtful = [c for c in quick.claims if c.confidence == "low"]
    print(f"Escalating {len(doubtful)} low-confidence claims to full verification:\n")
    for c in doubtful:
        v = client.verify_and_wait(claim=c.claim, timeout=120)
        print(f"{v.verdict.upper():<14} (lenz_score {v.lenz_score}) {c.claim}")
        if v.verdict.lower() in ("false", "misleading") and v.sources:
            print(f"  ↳ {v.sources[0].title}")
            print(f"    {v.sources[0].url}")


if __name__ == "__main__":
    main()
