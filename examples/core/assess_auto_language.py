"""Answer in the language of the text — ``language='auto'``.

Run:
    export LENZ_API_KEY=lenz_...
    python examples/core/assess_auto_language.py

Pass ``language='auto'`` on ``assess``, ``verify`` / ``verify_and_wait`` or
``ask.send`` and the answer comes back in the language of the text you
submitted (on ``ask.send``, the language of the claim being discussed). A
concrete code such as ``'es'`` always wins; leaving ``language`` out still
means English. ``extract``, ``verify_batch``, ``citecheck`` and ``review`` take
the codes only, not ``'auto'``. Verdict labels stay English.
"""

from __future__ import annotations

import os

from lenz_io import Lenz


def main() -> None:
    client = Lenz(api_key=os.environ.get("LENZ_API_KEY"))

    r = client.assess(claim="Die Erde ist flach.", language="auto")
    row = r.claims[0]
    print(f"verdict: {row.verdict}")  # 'False' (English enum)
    print(f"language: {row.language}")  # 'de'


if __name__ == "__main__":
    main()
