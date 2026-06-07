"""Verify several claims in parallel and wait for all of them.

Run:
    export LENZ_API_KEY=lenz_...
    python examples/core/verify_batch.py

`verify_batch_and_wait` fans out up to 20 claims in one call, polls every
one to completion, and returns a `BatchItemResult` per claim — in input
order. It never raises because one claim failed: inspect each item's
`status` ("completed" | "needs_input" | "failed" | "timeout").
"""

from __future__ import annotations

import os

from lenz_io import Lenz


def main() -> None:
    client = Lenz(api_key=os.environ.get("LENZ_API_KEY"))

    results = client.verify_batch_and_wait(
        claims=[
            {"text": "Sharks don't get cancer"},
            {"text": "The Eiffel Tower is 330m tall"},
            {"text": "Humans only use 10% of their brains"},
        ],
        timeout=180,
    )

    for r in results:
        if r.status == "completed" and r.verification is not None:
            print(f"[completed] {r.claim_text} → {r.verification.verdict} ({r.verification.lenz_score})")
        elif r.status == "failed":
            detail = r.status_detail
            reason = (detail.error or detail.failure_detail) if detail else "unknown"
            print(f"[failed]    {r.claim_text} → {reason}")
        else:
            # needs_input (resolve with client.select) or timeout (poll later)
            print(f"[{r.status}] {r.claim_text}")


if __name__ == "__main__":
    main()
