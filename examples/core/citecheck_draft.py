"""Check a draft's citations on their own: does each source say what the
draft says it does?

Run:
    export LENZ_API_KEY=lenz_...
    python examples/core/citecheck_draft.py

Send a draft (its links and DOIs are read from the text; keep a link as a
markdown link) or the statement-source pairs yourself. 1 credit per checked
citation; a citation that could not be checked is not charged.
"""

from __future__ import annotations

import os

from lenz_io import CitationPair, Citecheck, CitecheckFailedError, CitecheckTimeoutError, Lenz

DRAFT = (
    "Water boils at 100 degrees Celsius at sea level, according to "
    "[the encyclopedia entry](https://en.wikipedia.org/wiki/Boiling_point)."
)

PAIRS: list[CitationPair] = [
    {
        "statement": "Diamond sensors can measure temperature inside a living cell.",
        "doi": "10.1038/nature12373",
        "cited_year": "2013",
    },
]


def report(label: str, check: Citecheck) -> None:
    print(f"{label}: {check.outcome}")  # clean | issues_found | incomplete | unchecked
    for row in check.citations:
        finding = row.result.finding if row.result else "pending"
        print(f"  {finding}: {row.cited_url or row.doi}")
    for issue in check.citation_issues:  # most serious first
        print(f"  ISSUE {issue.finding}: {issue.statement}")
        if issue.snippet:
            print(f"    The source says: {issue.snippet}")


def main() -> None:
    client = Lenz(api_key=os.environ.get("LENZ_API_KEY"))
    try:
        report("draft", client.citecheck_and_wait(DRAFT, max_citations=10))
        report("pairs", client.citecheck_and_wait(pairs=PAIRS))
    except CitecheckTimeoutError as exc:
        # The check keeps running: read it later by its id, never resubmit.
        print("Still running; read it later with client.get_citecheck:", exc.citecheck_id)
    except CitecheckFailedError as exc:
        print("The check failed:", exc.error_code, exc.hint)


if __name__ == "__main__":
    main()
