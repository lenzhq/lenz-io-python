"""Type-checked only (``tests/test_result_typing.py`` runs ``mypy --strict``
on it): every call that returns a top-level result is a ``Result``, whose
``http_status`` and ``headers`` need no ``None`` check."""

from __future__ import annotations

from lenz_io import AsyncLenz, Lenz, Result
from lenz_io.errors import ResponseHeaders


def describe(result: Result) -> str:
    status: int = result.http_status
    headers: ResponseHeaders = result.headers
    return f"{status} {headers.get('x-request-id', '')}"


def every_call(client: Lenz) -> list[Result]:
    return [
        client.verify("A."),
        client.verify_batch(claims=[{"claim": "A."}]),
        client.select("t1", claims=["A."]),
        client.extract(text="Text."),
        client.assess("A."),
        client.get_status("t1"),
        client.cancel("t1"),
        client.get_review("r1"),
        client.get_review("r1", view="issues"),
        client.review("Draft."),
        client.review_and_wait("Draft."),
        client.cancel_review("r1"),
        client.citecheck("Draft."),
        client.get_citecheck("c1"),
        client.cancel_citecheck("c1"),
        client.citecheck_and_wait("Draft."),
        client.usage(),
        client.verifications.list(),
        client.verifications.get("v1"),
        client.verifications.get_certificate("v1"),
        client.verifications.related("v1"),
        client.ask.history("v1"),
        client.ask.send("v1", message="Why?"),
        client.library.list(),
    ]


async def every_async_call(client: AsyncLenz) -> list[Result]:
    return [
        await client.verify("A."),
        await client.verify_batch(claims=[{"claim": "A."}]),
        await client.select("t1", claims=["A."]),
        await client.extract(text="Text."),
        await client.assess("A."),
        await client.get_status("t1"),
        await client.cancel("t1"),
        await client.get_review("r1"),
        await client.get_review("r1", view="issues"),
        await client.review("Draft."),
        await client.review_and_wait("Draft."),
        await client.cancel_review("r1"),
        await client.citecheck("Draft."),
        await client.get_citecheck("c1"),
        await client.cancel_citecheck("c1"),
        await client.citecheck_and_wait("Draft."),
        await client.usage(),
        await client.verifications.list(),
        await client.verifications.get("v1"),
        await client.verifications.get_certificate("v1"),
        await client.verifications.related("v1"),
        await client.ask.history("v1"),
        await client.ask.send("v1", message="Why?"),
        await client.library.list(),
    ]
