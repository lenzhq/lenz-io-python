"""The request bodies the SDK sends, for ``tests/test_parity.py``.

Like ``parity_observe``, this uses only what the previous release had, so
``parity_generate.py`` can freeze the previous release's bodies as the
oracle (``tests/fixtures/parity/requests.json``).
"""

from __future__ import annotations

import json
from typing import Any

import respx

from lenz_io.client import DEFAULT_BASE_URL, Lenz

DRAFT = "The Eiffel Tower is in Berlin. See https://example.org/eiffel."

#: (name, method, path, response, kwargs). The /review and /citecheck rows pin
#: ``webhook_url`` in all three states: unset, ``""`` (no webhook) and a URL.
CALLS: list[tuple[str, str, str, dict[str, Any], dict[str, Any]]] = [
    ("review_default", "review", "/review", {"review_id": "r1", "status": "queued"}, {"text": DRAFT}),
    ("review_webhook_empty", "review", "/review", {"review_id": "r1"}, {"text": DRAFT, "webhook_url": ""}),
    (
        "review_webhook_url",
        "review",
        "/review",
        {"review_id": "r1"},
        {"text": DRAFT, "webhook_url": "https://hooks.example.test/x"},
    ),
    (
        "review_options",
        "review",
        "/review",
        {"review_id": "r1"},
        {
            "text": DRAFT,
            "verdicts": ["False"],
            "confidence": ["low"],
            "max_assessments": 5,
            "max_verifications": 2,
            "depth": "low",
            "max_citations": 3,
            "suggest_edits": True,
            "language": "de",
            "visibility": "unlisted",
        },
    ),
    ("citecheck_default", "citecheck", "/citecheck", {"citecheck_id": "c1"}, {"text": DRAFT}),
    ("citecheck_webhook_empty", "citecheck", "/citecheck", {"citecheck_id": "c1"}, {"text": DRAFT, "webhook_url": ""}),
    (
        "citecheck_webhook_url",
        "citecheck",
        "/citecheck",
        {"citecheck_id": "c1"},
        {"text": DRAFT, "webhook_url": "https://hooks.example.test/x", "max_citations": 4},
    ),
    (
        "citecheck_pairs",
        "citecheck",
        "/citecheck",
        {"citecheck_id": "c1"},
        {"pairs": [{"statement": "The tower is in Paris.", "url": "https://example.org/eiffel"}], "language": "fr"},
    ),
    (
        "verify_webhook_url",
        "verify_and_submit",
        "/verify",
        {"task_id": "t1", "status": "queued"},
        {"claim": "The Earth is round.", "webhook_url": "https://hooks.example.test/x"},
    ),
    (
        "verify_batch_webhook_url",
        "verify_batch",
        "/verify/batch",
        {"batch_id": "b1", "items": []},
        {"claims": [{"claim": "The Earth is round."}], "webhook_url": "https://hooks.example.test/x"},
    ),
]


def capture(method: str, path: str, response: dict[str, Any], kwargs: dict[str, Any]) -> Any:
    client = Lenz(api_key="lenz_" + "0" * 32)
    try:
        with respx.mock(base_url=DEFAULT_BASE_URL) as mock:
            route = mock.post(path).respond(202, json=response)
            if method == "verify_and_submit":
                client._verify_submit(claim=kwargs["claim"], webhook_url=kwargs["webhook_url"], idempotency_key="k")
            elif method == "verify_batch":
                client.verify_batch(idempotency_key="k", **kwargs)
            else:
                getattr(client, method)(idempotency_key="k", **kwargs)
            return json.loads(route.calls.last.request.content)
    finally:
        client.close()


def bodies() -> dict[str, Any]:
    return {name: capture(method, path, response, kwargs) for name, method, path, response, kwargs in CALLS}
