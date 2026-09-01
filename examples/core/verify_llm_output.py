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
from concurrent.futures import ThreadPoolExecutor

from lenz_io import Lenz

LLM_OUTPUT = """
The Eiffel Tower was completed in 1889 and stands 330 meters tall.
It was originally intended to be a temporary structure for the World's Fair.
Today it receives roughly 7 million visitors per year.
"""


def main() -> None:
    client = Lenz(api_key=os.environ.get("LENZ_API_KEY"))

    # Step 1: extract the atomic claims (free, ~1s). A document handed
    # straight to ``assess`` is framed with a fixed claim budget and the
    # rest is dropped; ``extract`` enumerates everything.
    out = client.extract(text=LLM_OUTPUT)
    claims = out.identified_claims or [out.claim]

    # Step 2: assess each claim (~10s, one credit per claim). The calls are
    # independent, so run them side by side.
    with ThreadPoolExecutor() as pool:
        quick = list(pool.map(lambda c: client.assess(claim=c).claims[0], claims))
    print(f"Assessed {len(quick)} claims:\n")
    for c in quick:
        print(f"  {c.verdict:<12}  conf={c.confidence:<7}  {c.claim}")
    print()

    # Step 3: escalate the low-confidence ones to the full pipeline, in one
    # batch. ``assess`` and ``verify`` share a result cache server-side, so a
    # claim that already has a deep verification surfaces immediately via
    # ``verification_url`` and you can skip the escalation.
    doubtful = [{"claim": c.claim} for c in quick if c.confidence == "low"]
    print(f"Escalating {len(doubtful)} low-confidence claims to full verification:\n")
    results = client.verify_batch_and_wait(claims=doubtful, timeout=180) if doubtful else []
    for r in results:
        v = r.verification
        if v is None:
            print(f"{r.status.upper():<14} {r.claim_text}")
            continue
        print(f"{v.verdict.upper():<14} (lenz_score {v.lenz_score}) {r.claim_text}")
        if v.verdict.lower() in ("false", "misleading") and v.sources:
            print(f"  ↳ {v.sources[0].title}")
            print(f"    {v.sources[0].url}")


if __name__ == "__main__":
    main()
