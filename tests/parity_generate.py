"""Freeze what the previous release made of every original-shape response.

Run once against the code BEFORE this SDK read the newer response shape, with
that code first on the path, e.g. from a checkout of the parent commit::

    PYTHONPATH=/path/to/parent-checkout/src:tests python tests/parity_generate.py

It writes ``tests/fixtures/parity/expected/<name>.json`` and
``tests/fixtures/parity/requests.json``: the oracle
``tests/test_parity.py`` holds both response shapes to. Do not regenerate it
with the current code; that would make the test compare the code with itself.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from parity_observe import FIXTURES, load, names, observe
from parity_requests import bodies
from parity_static import pickles, schemas

import lenz_io


def main() -> None:
    print(f"observing with lenz_io from {lenz_io.__file__}", file=sys.stderr)
    if "--allow-current" not in sys.argv and Path(lenz_io.__file__).resolve().is_relative_to(
        Path(__file__).resolve().parents[1]
    ):
        raise SystemExit("refusing: this is the current code; put the previous release first on PYTHONPATH")
    out = FIXTURES / "expected"
    out.mkdir(exist_ok=True)
    for name in names():
        result = observe(name, load("legacy", name))
        (out / name).write_text(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    (FIXTURES / "requests.json").write_text(json.dumps(bodies(), indent=2, sort_keys=True) + "\n")
    (FIXTURES / "schemas.json").write_text(json.dumps(schemas(), indent=2, sort_keys=True) + "\n")
    (FIXTURES / "pickles.json").write_text(json.dumps(pickles(), indent=2, sort_keys=True) + "\n")
    print(f"wrote {len(names())} files and requests.json", file=sys.stderr)


if __name__ == "__main__":
    main()
