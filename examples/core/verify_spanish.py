"""Multi-language output — Spanish verification.

Run:
    export LENZ_API_KEY=lenz_...
    python examples/core/verify_spanish.py

The same API and same SDK; pass ``language='es'`` (or any of the 12
supported codes: en, es, de, fr, it, pt, nl, sv, da, no, fi, bg) and
the response's free-form prose comes back in that language. Verdict
labels (True / False / Misleading / Mostly True) stay English so SDK
consumers can branch on them deterministically.
"""

from __future__ import annotations

import os

from lenz_io import Lenz


def main() -> None:
    client = Lenz(api_key=os.environ.get("LENZ_API_KEY"))

    v = client.verify_and_wait(claim="La Tierra es plana", language="es")
    print(f"verdict: {v.verdict}")  # 'False' (English enum)
    print(f"language: {v.language}")  # 'es'
    print(f"claim: {v.claim}")  # 'La Tierra es plana'
    print(f"executive_summary: {v.executive_summary}")  # Spanish prose


if __name__ == "__main__":
    main()
