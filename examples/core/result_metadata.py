"""Read any call's HTTP status and headers — the ``Result`` type.

Run:
    export LENZ_API_KEY=lenz_...
    python examples/core/result_metadata.py

Every call that returns a result read from one answer returns a ``Result``
subclass: its ``http_status`` is an ``int`` and its ``headers`` a
``ResponseHeaders`` (never ``None``), so a helper typed on ``Result`` works
for all of them without a check. Models nested in a result (an
``AssessResponse.claims`` row, ``TaskStatus.result``) are not ``Result`` instances:
theirs stay ``None``.
"""

from __future__ import annotations

import os

from lenz_io import Lenz, LenzMissingKeyError, Result


def log_answer(call: str, result: Result) -> None:
    request_id = result.headers.get("x-request-id", "-")
    print(f"{call}: HTTP {result.http_status} (request {request_id})")


def main() -> None:
    client = Lenz(api_key=os.environ.get("LENZ_API_KEY"))
    try:
        accepted = client.verify("The Eiffel Tower is in Paris.")
    except LenzMissingKeyError:
        print("Set LENZ_API_KEY first.")
        return
    log_answer("verify", accepted)  # HTTP 202

    started = client.review("The Eiffel Tower is in Paris. It opened in 1889.")
    log_answer("review", started)  # HTTP 202
    print("read it at:", started.headers.get("location"))  # a review receipt names it

    assessed = client.assess("Water boils at 100 C at sea level.")
    log_answer("assess", assessed)  # HTTP 200


if __name__ == "__main__":
    main()
