"""Extract claims from an LLM response, assess them in one call, escalate the doubtful ones.

The headline integration story: your model emits an answer, Lenz pulls
the verifiable claims out of it (``extract``), ONE ``assess`` call gives
you a fast verdict per claim (up to 20 claims, one row per claim, in the
order sent), and you escalate only the low-confidence rows to the full
multi-model pipeline (``verify``). Cheaper and faster than verifying every
claim outright.

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

    # Step 1: extract — pull the verifiable claims out of the answer (free).
    out = client.extract(text=LLM_OUTPUT)
    claims = out.identified_claims or [out.claim]
    print(f"Extracted {len(claims)} claims.\n")

    # Step 2: assess — one call over the extracted claims, one row per claim,
    # same order. A row with verdict "Error" got no verdict: ``error_code``
    # says why (``upstream_unavailable`` is worth a retry) and ``hint`` says
    # what to send next. A compound item is assessed on its main claim and
    # lists the rest in ``identified_claims``.
    quick = client.assess(claims=claims).claims
    print(f"Assessed {len(quick)} claims:\n")
    for c in quick:
        print(f"  {c.verdict:<12}  conf={c.confidence:<7}  {c.claim}")
        if c.verdict == "Error":
            print(f"    {c.error_code}: {c.hint}")
        elif c.identified_claims:
            print(f"    also found (not assessed): {c.identified_claims}")
    print()

    # Step 3: verify — escalate the low-confidence rows to the full pipeline.
    # ``assess`` and ``verify`` share a result cache server-side, so a
    # claim that already has a deep verification surfaces immediately
    # via ``verification_url`` and you can skip the escalation.
    doubtful = [{"claim": c.claim} for c in quick if c.verdict != "Error" and c.confidence == "low"]
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
