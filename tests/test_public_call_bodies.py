"""The request bodies of the public calls, frozen.

A request body is part of the contract twice over: the server reads it, and
an ``Idempotency-Key`` replay hashes it, so a body that changes shape between
releases turns a retried request into a 422 (``idempotency_body_mismatch``)
instead of a replay. These tests send each public call with every option
left out, set to its empty / zero / false value, and set to a real value,
always under a pinned key, and compare the body that reached the wire, key
order included, with the one this release sends.

They go through the public methods (never the private submit helpers), so a
change to a signature that forwards an option differently fails here.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import respx
from conftest import AnyClient, make_client

from lenz_io import Lenz

# Every test here runs on both clients (``any_client`` in conftest.py): the
# same calls and assertions against ``Lenz`` and ``AsyncLenz``.
pytestmark = pytest.mark.usefixtures("any_client")


@pytest.fixture()
def client() -> Iterator[Any]:
    with make_client(api_key="lenz_test_abc123") as c:
        yield c


@pytest.fixture()
def unauth_client() -> Iterator[Any]:
    with make_client() as c:
        yield c


BASE = "https://lenz.io/api/v1"
KEY = "pinned-key-1"
FIXTURES = Path(__file__).parent / "fixtures" / "contract"


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


REVIEW_DONE = _load("review_completed.json")
CHECK_DONE = _load("citecheck_completed.json")
_COMPLETED_STATUS = {"status": "completed", "task_id": "t", "result": {"verification_id": "v1", "claim": "A."}}


def _ordered(content: bytes) -> list[tuple[str, Any]]:
    """The body as (key, value) pairs in wire order, nested objects too."""
    return json.loads(content, object_pairs_hook=list)


@pytest.fixture()
def no_sleep(any_client: AnyClient) -> list[float]:
    return any_client.slept


# ── verify / verify_and_wait ───────────────────────────────────────────────

_VERIFY_CASES: list[tuple[str, dict[str, Any], list[tuple[str, Any]]]] = [
    ("omitted", {}, [("text", "A.")]),
    (
        "empty",
        {"language": "", "visibility": "", "depth": "", "source_url": "", "webhook_url": ""},
        [("text", "A.")],
    ),
    ("blank webhook", {"webhook_url": "   "}, [("text", "A.")]),
    (
        "set",
        {
            "language": "es",
            "visibility": "unlisted",
            "depth": "low",
            "source_url": "https://example.com/a",
            "webhook_url": "https://example.com/hook",
        },
        [
            ("text", "A."),
            ("source_url", "https://example.com/a"),
            ("webhook_url", "https://example.com/hook"),
            ("language", "es"),
            ("visibility", "unlisted"),
            ("depth", "low"),
        ],
    ),
]


class TestVerify:
    @pytest.mark.parametrize(("label", "kwargs", "expected"), _VERIFY_CASES)
    def test_verify(self, client: Lenz, label: str, kwargs: dict[str, Any], expected: list[Any]) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.post("/verify").respond(200, json={"task_id": "t"})
            client.verify("A.", idempotency_key=KEY, **kwargs)
        assert _ordered(route.calls.last.request.content) == expected
        assert route.calls.last.request.headers["Idempotency-Key"] == KEY

    @pytest.mark.parametrize(("label", "kwargs", "expected"), _VERIFY_CASES)
    def test_verify_and_wait(self, client: Lenz, label: str, kwargs: dict[str, Any], expected: list[Any]) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.post("/verify").respond(200, json={"task_id": "t"})
            r.get("/verify/status/t").respond(200, json=_COMPLETED_STATUS)
            client.verify_and_wait("A.", idempotency_key=KEY, **kwargs)
        assert _ordered(route.calls.last.request.content) == expected
        assert route.calls.last.request.headers["Idempotency-Key"] == KEY

    def test_text_alias(self, client: Lenz) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.post("/verify").respond(200, json={"task_id": "t"})
            client.verify(text="A.", idempotency_key=KEY)
        assert _ordered(route.calls.last.request.content) == [("text", "A.")]


# ── verify_batch / verify_batch_and_wait ───────────────────────────────────

_BATCH_CASES: list[tuple[str, dict[str, Any], list[tuple[str, Any]]]] = [
    ("omitted", {}, [("claims", [[("text", "A.")], [("text", "B.")]])]),
    (
        "empty",
        {"webhook_url": "", "language": "", "visibility": "", "depth": ""},
        [("claims", [[("text", "A.")], [("text", "B.")]])],
    ),
    (
        "set",
        {"webhook_url": "https://example.com/hook", "language": "de", "visibility": "unlisted", "depth": "low"},
        [
            ("claims", [[("text", "A.")], [("text", "B.")]]),
            ("webhook_url", "https://example.com/hook"),
            ("language", "de"),
            ("visibility", "unlisted"),
            ("depth", "low"),
        ],
    ),
]
_BATCH_ACCEPTED = {"batch_id": "b", "items": [{"task_id": "t", "claim": "A."}]}


class TestVerifyBatch:
    @pytest.mark.parametrize(("label", "kwargs", "expected"), _BATCH_CASES)
    def test_verify_batch(self, client: Lenz, label: str, kwargs: dict[str, Any], expected: list[Any]) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.post("/verify/batch").respond(200, json=_BATCH_ACCEPTED)
            client.verify_batch(claims=[{"claim": "A."}, {"text": "B."}], idempotency_key=KEY, **kwargs)
        assert _ordered(route.calls.last.request.content) == expected
        assert route.calls.last.request.headers["Idempotency-Key"] == KEY

    @pytest.mark.parametrize(("label", "kwargs", "expected"), _BATCH_CASES)
    def test_verify_batch_and_wait(self, client: Lenz, label: str, kwargs: dict[str, Any], expected: list[Any]) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.post("/verify/batch").respond(200, json=_BATCH_ACCEPTED)
            r.get("/verify/status/t").respond(200, json=_COMPLETED_STATUS)
            client.verify_batch_and_wait(claims=[{"claim": "A."}, {"text": "B."}], idempotency_key=KEY, **kwargs)
        assert _ordered(route.calls.last.request.content) == expected
        assert route.calls.last.request.headers["Idempotency-Key"] == KEY

    def test_item_keys_pass_through_and_a_blank_item_webhook_is_left_out(self, client: Lenz) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.post("/verify/batch").respond(200, json=_BATCH_ACCEPTED)
            client.verify_batch(
                claims=[
                    {"claim": "A.", "language": "fr", "depth": "low", "webhook_url": ""},
                    {"text": "B.", "visibility": "unlisted", "webhook_url": "https://example.com/h"},
                ],
                idempotency_key=KEY,
            )
        assert _ordered(route.calls.last.request.content) == [
            (
                "claims",
                [
                    [("language", "fr"), ("depth", "low"), ("text", "A.")],
                    [("text", "B."), ("visibility", "unlisted"), ("webhook_url", "https://example.com/h")],
                ],
            )
        ]


# ── assess / extract / select / ask.send ───────────────────────────────────


class TestSyncCalls:
    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({"claim": "A."}, [("text", "A.")]),
            ({"text": "A.", "language": "", "suggest_rewrite": False}, [("text", "A.")]),
            (
                {"claim": "A.", "language": "auto", "suggest_rewrite": True},
                [("text", "A."), ("language", "auto"), ("suggest_rewrite", True)],
            ),
            ({"claims": ["A.", "B."]}, [("claims", ["A.", "B."])]),
            ({"claims": ["A."], "language": "es"}, [("claims", ["A."]), ("language", "es")]),
        ],
    )
    def test_assess(self, client: Lenz, kwargs: dict[str, Any], expected: list[Any]) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.post("/assess").respond(200, json={"claims": []})
            client.assess(idempotency_key=KEY, **kwargs)
        assert _ordered(route.calls.last.request.content) == expected
        assert route.calls.last.request.headers["Idempotency-Key"] == KEY

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({}, [("text", "Doc.")]),
            ({"language": "", "focus": "", "locate": None}, [("text", "Doc.")]),
            ({"locate": False}, [("text", "Doc."), ("locate", False)]),
            (
                {"language": "it", "focus": "figures", "locate": True},
                [("text", "Doc."), ("language", "it"), ("focus", "figures"), ("locate", True)],
            ),
        ],
    )
    def test_extract(self, client: Lenz, kwargs: dict[str, Any], expected: list[Any]) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.post("/extract").respond(200, json={"claims": [], "status": "ok"})
            client.extract(text="Doc.", idempotency_key=KEY, **kwargs)
        assert _ordered(route.calls.last.request.content) == expected
        assert route.calls.last.request.headers["Idempotency-Key"] == KEY

    @pytest.mark.parametrize(
        "kwargs",
        [{"claims": ["A.", "B."]}, {"texts": ["A.", "B."]}, {"claims": ["A.", "B."], "texts": ["C."]}],
    )
    def test_select(self, client: Lenz, kwargs: dict[str, Any]) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.post("/verify/t/select").respond(200, json={"items": []})
            client.select("t", idempotency_key=KEY, **kwargs)
        assert _ordered(route.calls.last.request.content) == [("texts", ["A.", "B."])]
        assert route.calls.last.request.headers["Idempotency-Key"] == KEY

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({}, [("message", "Why?")]),
            ({"language": ""}, [("message", "Why?")]),
            ({"language": "auto"}, [("message", "Why?"), ("language", "auto")]),
        ],
    )
    def test_ask_send(self, client: Lenz, kwargs: dict[str, Any], expected: list[Any]) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.post("/ask/v1").respond(200, json={"role": "expert", "content": "Because."})
            client.ask.send("v1", message="Why?", idempotency_key=KEY, **kwargs)
        assert _ordered(route.calls.last.request.content) == expected
        assert route.calls.last.request.headers["Idempotency-Key"] == KEY


# ── review / review_and_wait ───────────────────────────────────────────────

_REVIEW_CASES: list[tuple[str, dict[str, Any], list[tuple[str, Any]]]] = [
    ("omitted", {}, [("text", "Draft."), ("visibility", "private")]),
    (
        "empty, zero and false",
        {
            "verdicts": [],
            "confidence": [],
            "max_assessments": 0,
            "max_verifications": 0,
            "depth": None,
            "max_citations": 0,
            "suggest_edits": False,
            "language": "",
            "webhook_url": "",
            "visibility": "",
        },
        [
            ("text", "Draft."),
            ("webhook_url", ""),
            ("escalate", [("verdicts", []), ("confidence", []), ("max_assessments", 0), ("max_verifications", 0)]),
        ],
    ),
    (
        "max_citations None",
        {"max_citations": None, "webhook_url": None},
        [("text", "Draft."), ("visibility", "private")],
    ),
    (
        "set",
        {
            "verdicts": ["False"],
            "confidence": ["low", "medium"],
            "max_assessments": 5,
            "max_verifications": 2,
            "depth": "low",
            "max_citations": 3,
            "suggest_edits": True,
            "language": "nl",
            "webhook_url": "https://example.com/hook",
            "visibility": "unlisted",
        },
        [
            ("text", "Draft."),
            ("language", "nl"),
            ("webhook_url", "https://example.com/hook"),
            ("visibility", "unlisted"),
            (
                "escalate",
                [
                    ("verdicts", ["False"]),
                    ("confidence", ["low", "medium"]),
                    ("max_assessments", 5),
                    ("max_verifications", 2),
                    ("depth", "low"),
                    ("max_citations", 3),
                    ("suggest_edits", True),
                ],
            ),
        ],
    ),
]


class TestReview:
    @pytest.mark.parametrize(("label", "kwargs", "expected"), _REVIEW_CASES)
    def test_review(self, client: Lenz, label: str, kwargs: dict[str, Any], expected: list[Any]) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.post("/review").respond(202, json={"review_id": REVIEW_DONE["review_id"], "status": "queued"})
            client.review("Draft.", idempotency_key=KEY, **kwargs)
        assert _ordered(route.calls.last.request.content) == expected
        assert route.calls.last.request.headers["Idempotency-Key"] == KEY

    @pytest.mark.parametrize(("label", "kwargs", "expected"), _REVIEW_CASES)
    def test_review_and_wait(
        self, client: Lenz, no_sleep: None, label: str, kwargs: dict[str, Any], expected: list[Any]
    ) -> None:
        rid = REVIEW_DONE["review_id"]
        with respx.mock(base_url=BASE) as r:
            route = r.post("/review").respond(202, json={"review_id": rid, "status": "queued"})
            r.get(f"/reviews/{rid}").respond(200, json=REVIEW_DONE)
            client.review_and_wait("Draft.", idempotency_key=KEY, **kwargs)
        assert _ordered(route.calls.last.request.content) == expected
        assert route.calls.last.request.headers["Idempotency-Key"] == KEY


# ── citecheck / citecheck_and_wait ─────────────────────────────────────────

_PAIR = {"statement": "Water boils at 100 C.", "url": "https://example.com/boil"}
_CITECHECK_CASES: list[tuple[str, tuple[Any, ...], dict[str, Any], list[tuple[str, Any]]]] = [
    ("text omitted", ("Draft.",), {}, [("text", "Draft.")]),
    (
        "text zero and empty",
        ("Draft.",),
        {"max_citations": 0, "language": "", "webhook_url": ""},
        [("text", "Draft."), ("max_citations", 0), ("webhook_url", "")],
    ),
    (
        "text set",
        ("Draft.",),
        {"max_citations": 4, "language": "fr", "webhook_url": "https://example.com/hook"},
        [("text", "Draft."), ("max_citations", 4), ("language", "fr"), ("webhook_url", "https://example.com/hook")],
    ),
    (
        "pairs",
        (),
        {"pairs": [_PAIR], "language": "", "webhook_url": None},
        [("pairs", [[("statement", "Water boils at 100 C."), ("url", "https://example.com/boil")]])],
    ),
    (
        "blank text with pairs",
        ("  ",),
        {"pairs": [_PAIR]},
        [("pairs", [[("statement", "Water boils at 100 C."), ("url", "https://example.com/boil")]])],
    ),
]


class TestCitecheck:
    @pytest.mark.parametrize(("label", "args", "kwargs", "expected"), _CITECHECK_CASES)
    def test_citecheck(
        self, client: Lenz, label: str, args: tuple[Any, ...], kwargs: dict[str, Any], expected: list[Any]
    ) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.post("/citecheck").respond(
                202, json={"citecheck_id": CHECK_DONE["citecheck_id"], "status": "queued"}
            )
            client.citecheck(*args, idempotency_key=KEY, **kwargs)
        assert _ordered(route.calls.last.request.content) == expected
        assert route.calls.last.request.headers["Idempotency-Key"] == KEY

    @pytest.mark.parametrize(("label", "args", "kwargs", "expected"), _CITECHECK_CASES)
    def test_citecheck_and_wait(
        self,
        client: Lenz,
        no_sleep: None,
        label: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        expected: list[Any],
    ) -> None:
        cid = CHECK_DONE["citecheck_id"]
        with respx.mock(base_url=BASE) as r:
            route = r.post("/citecheck").respond(202, json={"citecheck_id": cid, "status": "queued"})
            r.get(f"/citechecks/{cid}").respond(200, json=CHECK_DONE)
            client.citecheck_and_wait(*args, idempotency_key=KEY, **kwargs)
        assert _ordered(route.calls.last.request.content) == expected
        assert route.calls.last.request.headers["Idempotency-Key"] == KEY


# ── cancel / cancel_review / cancel_citecheck ──────────────────────────────


_ID = {"cancel": "task_id", "cancel_review": "review_id", "cancel_citecheck": "citecheck_id"}


class TestCancels:
    """A cancel is a POST with no body and no key: it is safe to repeat, so
    there is nothing for an ``Idempotency-Key`` to replay."""

    @pytest.mark.parametrize(
        ("method", "path", "fixture"),
        [
            ("cancel", "/verify/3f2a9c1e5b7d4a608c1d2e3f4a5b6c7d/cancel", "cancel_verify_cancelled.json"),
            ("cancel_review", "/reviews/d6b2bd72/cancel", "cancel_review_cancelled.json"),
            ("cancel_citecheck", "/citechecks/12bbbf65/cancel", "cancel_citecheck_cancelled.json"),
        ],
    )
    def test_no_body_no_key(self, client: Lenz, method: str, path: str, fixture: str) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.post(path).respond(200, json=_load(fixture))
            getattr(client, method)(path.split("/")[2])
        request = route.calls.last.request
        assert request.content == b""
        assert "Idempotency-Key" not in request.headers
        # The id is the only parameter besides the request options, which are
        # keyword-only and reach the wire only as the headers asked for.
        params = inspect.signature(getattr(Lenz, method)).parameters
        assert list(params) == ["self", _ID[method], "timeout", "max_retries", "extra_headers"]
        assert all(
            params[n].kind is inspect.Parameter.KEYWORD_ONLY for n in ("timeout", "max_retries", "extra_headers")
        )


# ── 3.0: every forwarded option is a named, keyword-only parameter ─────────


@pytest.mark.parametrize(
    ("outer", "inner", "own"),
    [
        ("verify", "_verify_submit", {"claim", "text", "idempotency", "idempotency_key"}),
        ("review_and_wait", "review", {"text", "timeout", "on_update"}),
        ("citecheck_and_wait", "citecheck", {"text", "timeout", "on_update"}),
    ],
)
def test_forwarded_options_are_listed(outer: str, inner: str, own: set[str]) -> None:
    import inspect

    outer_params = inspect.signature(getattr(Lenz, outer)).parameters
    inner_params = inspect.signature(getattr(Lenz, inner)).parameters
    assert not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in outer_params.values()), "no **kwargs"
    forwarded = {n for n in inner_params if n != "self"} - own
    for name in forwarded:
        assert name in outer_params, name
        assert outer_params[name].kind is inspect.Parameter.KEYWORD_ONLY, name
        assert outer_params[name].default == inner_params[name].default, name
    first = next(n for n in outer_params if n != "self")
    assert outer_params[first].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD


def test_an_unknown_option_is_still_a_type_error(client: Lenz) -> None:
    for call in (
        lambda: client.verify("A.", sorce_url="x"),  # type: ignore[call-arg]
        lambda: client.review_and_wait("Draft.", max_asessments=1),  # type: ignore[call-arg]
        lambda: client.citecheck_and_wait("Draft.", max_citation=1),  # type: ignore[call-arg]
    ):
        with pytest.raises(TypeError):
            call()
