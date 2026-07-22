"""Keyless public-library reads — the pattern behind the open-source
FactOrFiction quiz demo (https://play.lenz.io).

``library.list`` needs no API key. ``curated=True`` returns the LLM-curated,
trivia-worthy subset; ``verdict`` filters by label; ``sort="random"`` shuffles.

    python examples/library_quiz.py
"""

from lenz_io import Lenz


def main() -> None:
    # No api_key — the library reads are public.
    lenz = Lenz()

    # A round of true/false quiz claims, curated and shuffled.
    rnd = lenz.library.list(curated=True, sort="random", verdict="True,False")

    for item in rnd.items[:5]:
        print(f"\n{item.claim}")
        print(f"  verdict: {item.verdict}  (lenz_score: {item.lenz_score})")
        print(f"  {item.executive_summary}")
        # Library items carry no url/slug — build the link from verification_id.
        print(f"  https://lenz.io/c/{item.verification_id}")


if __name__ == "__main__":
    main()
