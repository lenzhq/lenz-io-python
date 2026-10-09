"""Review a draft: every claim quick-checked, the doubtful ones deep-checked.

Run:
    export LENZ_API_KEY=lenz_...
    python examples/core/review_draft.py

One call pulls the claims out of the draft, gives each a quick verdict, sends
the ones that look wrong or uncertain through the deep check (up to five by
default) and returns the issues with suggested rewrites. It takes two to four
minutes; ``on_update`` shows the quick verdicts as soon as they are in.

Credits: 1 per claim assessed, plus 10 per deep check (5 at ``depth="low"``),
plus 1 per checked citation; ``review.credits.charged`` says what it cost.
"""

from __future__ import annotations

import os

from lenz_io import Lenz, LenzError, ReviewFailedError, ReviewFull, ReviewTimeoutError

DRAFT = """
The EU AI Act entered into force on 1 August 2024, and its obligations for
general-purpose models applied from 2 August 2025. Fines for prohibited
practices reach 7% of global annual turnover, according to
[the regulation](https://eur-lex.europa.eu/eli/reg/2024/1689/oj).
"""


def show_progress(review: ReviewFull) -> None:
    done = sum(1 for c in review.claims if c.assessment is not None and c.assessment.status != "pending")
    print(f"  {review.status}: {done}/{len(review.claims)} claims have a quick verdict")


def main() -> None:
    client = Lenz(api_key=os.environ.get("LENZ_API_KEY"))
    try:
        review = client.review_and_wait(
            DRAFT,
            max_citations=5,  # also check the draft's first 5 sources (1 credit each)
            suggest_edits=True,  # the smallest edits that make the draft say what each rewrite says
            on_update=show_progress,
        )
    except ReviewTimeoutError as exc:
        # The review keeps running: read it later by its id, never resubmit.
        print("Still running; read it later with client.get_review:", exc.review_id)
        return
    except ReviewFailedError as exc:
        print("The review failed:", exc.error_code, exc.hint)
        return
    except LenzError as exc:
        print("Could not run the review:", exc.message, "(retryable)" if exc.retryable else "")
        return

    print(f"\nOutcome: {review.outcome}")  # clean | issues_found | incomplete | unchecked
    for issue in review.issues:
        print(f"- {issue.verdict} ({issue.confidence}): {issue.claim}")
        if issue.suggested_rewrite:
            print(f"  Suggested rewrite: {issue.suggested_rewrite}")
    for citation in review.citation_issues:  # most serious first
        print(f"- Source issue {citation.finding}: {citation.cited_url or citation.doi}")
    print(f"Credits charged: {review.credits.charged if review.credits else 'unknown'}")


if __name__ == "__main__":
    main()
