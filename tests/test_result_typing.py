"""``Result`` is what the annotations say: ``mypy --strict`` accepts every
top-level call's result as a ``Result`` and its ``http_status`` as an
``int`` (``tests/typecheck/result_adapter.py``)."""

from __future__ import annotations

from pathlib import Path

import pytest

mypy_api = pytest.importorskip("mypy.api")

_ADAPTER = Path(__file__).parent / "typecheck" / "result_adapter.py"


def test_an_adapter_typed_on_result_passes_mypy_strict() -> None:
    out, err, status = mypy_api.run(["--strict", "--no-incremental", str(_ADAPTER)])
    assert status == 0, out + err
