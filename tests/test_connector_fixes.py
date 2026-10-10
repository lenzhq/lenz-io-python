"""Fixes for callers that act for many users (a connector, a gateway), on
both clients:

* An error answer in another API version raises its own error, with
  ``served_version``; ``LenzApiVersionError`` names both versions.
* ``legacy_aliases=False``: an error's ``code`` is the body's own.
* Every error read from a response carries its ``headers``.
* A 2xx whose JSON is not an object raises ``LenzInvalidResponseError``.
* An API key that cannot be sent raises ``LenzAuthError`` before any request.
* Blank input is refused before sending; an empty ``source_url`` is left out.
* ``review`` / ``citecheck`` take ``idempotency=False``.
* Every result carries ``raw``, the JSON object it was read from.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
import respx
from conftest import make_client

from lenz_io import (
    LenzAPIError,
    LenzAuthError,
    LenzError,
    LenzInvalidResponseError,
    LenzNotFoundError,
    LenzValidationError,
)
from lenz_io.errors import ResponseHeaders

pytestmark = pytest.mark.usefixtures("any_client")

BASE = "https://lenz.io/api/v1"
KEY = "lenz_test_abc123"
_USAGE = {"plan": "free", "credits": {"total": 100, "used": 40, "remaining": 60, "extra": 5}, "costs": {"verify": 10}}


@pytest.fixture()
def client() -> Iterator[Any]:
    with make_client(api_key=KEY, max_retries=0) as c:
        yield c


@pytest.fixture()
def wire() -> Iterator[Any]:
    with make_client(api_key=KEY, max_retries=0, legacy_aliases=False) as c:
        yield c


# ── 1. legacy_aliases=False: the code as sent ─────────────────────────────


_CODES = [
    ("GET", "/verifications/v1", 404, "not_found", lambda c: c.verifications.get("v1")),
    ("POST", "/verify", 409, "idempotency_conflict", lambda c: c.verify("A.", idempotency=False)),
    ("POST", "/verify", 422, "validation_error", lambda c: c.verify("A.")),
    ("POST", "/assess", 422, "blank_input", lambda c: c.assess("A.")),
    ("POST", "/assess", 422, "unsupported_language", lambda c: c.assess("A.", language="xx")),
    ("POST", "/assess", 422, "too_many_items", lambda c: c.assess(claims=["A."])),
    ("POST", "/extract", 400, "invalid_request", lambda c: c.extract(text="A.")),
    ("GET", "/me/usage", 500, "internal_error", lambda c: c.usage()),
    ("GET", "/me/usage", 401, "not_authenticated", lambda c: c.usage()),
    ("POST", "/ask/v1", 409, "verification_not_ready", lambda c: c.ask.send("v1", message="Why?")),
    ("POST", "/review", 401, "not_authenticated", lambda c: c.review("Draft.")),
]


@pytest.mark.parametrize(("method", "path", "status", "code", "call"), _CODES)
def test_without_legacy_aliases_the_code_is_the_bodys(
    wire: Any, method: str, path: str, status: int, code: str, call: Any
) -> None:
    body = {"detail": "Refused.", "code": code}
    if status == 422:
        body["errors"] = [{"type": code, "loc": ["body", "claim"], "msg": "Refused."}]
    with respx.mock(base_url=BASE) as r:
        getattr(r, method.lower())(path).respond(status, json=body)
        with pytest.raises(LenzError) as ei:
            call(wire)
    assert ei.value.code == code
    assert ei.value.body == body


def test_with_legacy_aliases_the_code_is_the_2x_one(client: Any) -> None:
    with respx.mock(base_url=BASE) as r:
        r.get("/verifications/v1").respond(404, json={"detail": "Not found.", "code": "not_found"})
        with pytest.raises(LenzNotFoundError) as ei:
            client.verifications.get("v1")
    assert ei.value.code == ""
    assert ei.value.body == {"detail": "Not found.", "code": "not_found"}


def test_without_legacy_aliases_a_bodys_missing_or_odd_code_reads_empty(wire: Any) -> None:
    with respx.mock(base_url=BASE) as r:
        r.get("/me/usage").respond(500, json={"detail": "x", "code": 42})
        with pytest.raises(LenzAPIError) as ei:
            wire.usage()
        assert ei.value.code == ""
        r.get("/me/usage").respond(502, content=b"<html>")
        with pytest.raises(LenzAPIError) as ei:
            wire.usage()
        assert ei.value.code == ""


def test_without_legacy_aliases_a_validation_error_keeps_its_class(wire: Any) -> None:
    body = {
        "detail": "depth: Input should be 'standard' or 'low'",
        "code": "validation_error",
        "errors": [{"type": "literal_error", "loc": ["body", "depth"], "msg": "Input should be 'standard' or 'low'"}],
    }
    with respx.mock(base_url=BASE) as r:
        r.post("/verify").respond(422, json=body)
        with pytest.raises(LenzValidationError) as ei:
            wire.verify("A.", depth="deep")
    assert ei.value.code == "validation_error"


# ── 2. headers on an error ────────────────────────────────────────────────


def test_an_error_carries_the_response_headers(client: Any) -> None:
    with respx.mock(base_url=BASE) as r:
        r.get("/me/usage").respond(
            429, json={"detail": "slow", "code": "rate_limited"}, headers={"Retry-After": "90", "X-Request-ID": "rq1"}
        )
        with pytest.raises(LenzError) as ei:
            client.usage()
    headers = ei.value.headers
    assert isinstance(headers, ResponseHeaders)
    assert headers["retry-after"] == headers["Retry-After"] == "90"
    assert "x-request-id" in headers and "X-Missing" not in headers
    assert ei.value.retry_after == 90
    with pytest.raises(TypeError):
        headers["X-New"] = "1"  # type: ignore[index]


def test_an_error_without_a_response_has_no_headers() -> None:
    assert dict(LenzError(message="m").headers) == {}
    assert LenzError(message="m").served_version is None


def test_response_headers_pickle() -> None:
    import pickle

    headers = ResponseHeaders({"Retry-After": "5"})
    assert pickle.loads(pickle.dumps(headers))["retry-after"] == "5"


# ── 7. a 2xx whose JSON is not an object ──────────────────────────────────


@pytest.mark.parametrize("content", [b"null", b"[]", b"[1, 2]", b"42", b'"x"', b"true"])
def test_json_that_is_not_an_object_is_an_invalid_response(client: Any, content: bytes) -> None:
    with respx.mock(base_url=BASE) as r:
        r.get("/me/usage").respond(200, content=content, headers={"X-Request-ID": "rq9"})
        with pytest.raises(LenzInvalidResponseError) as ei:
            client.usage()
    exc = ei.value
    assert exc.status_code == 200
    assert exc.body is None
    assert exc.body_text == content.decode()
    assert exc.request_id == "rq9"
    assert "not an object" in exc.message
    assert isinstance(exc, json.JSONDecodeError) and isinstance(exc, LenzAPIError)


def test_a_submit_answered_with_a_list_says_which_key_it_sent(client: Any) -> None:
    with respx.mock(base_url=BASE) as r:
        r.post("/assess").respond(200, json=[])
        with pytest.raises(LenzInvalidResponseError) as ei:
            client.assess("A.", idempotency_key="pinned")
    assert ei.value.idempotency_key == "pinned"


# ── 8. an API key that cannot be sent ─────────────────────────────────────


_BAD_KEYS = ["lenz_a bc", "lenz_ab\tc", "lenz_\x00abc", "lenz_abcé", "lenz_a\u2028bc", "lenz_a\nbc"]


@pytest.mark.parametrize("key", _BAD_KEYS)
def test_a_bad_key_is_refused_by_the_constructor(key: str) -> None:
    with respx.mock(base_url=BASE, assert_all_called=False) as r:
        route = r.get("/me/usage").respond(200, json=_USAGE)
        with pytest.raises(LenzAuthError) as ei:
            make_client(api_key=key)
    assert not route.called
    assert key.strip() not in str(ei.value)
    assert ei.value.status_code == 0


@pytest.mark.parametrize("key", _BAD_KEYS)
def test_a_bad_key_is_refused_by_with_options(client: Any, key: str) -> None:
    with pytest.raises(LenzAuthError):
        client.with_options(api_key=key)


def test_a_bad_key_in_the_environment_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LENZ_API_KEY", "lenz_a bc")
    with pytest.raises(LenzAuthError) as ei:
        make_client()
    assert "LENZ_API_KEY" in str(ei.value)


@pytest.mark.parametrize("key", ["", "   ", "\n", None])
def test_a_blank_key_is_still_no_key(key: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LENZ_API_KEY", raising=False)
    with make_client(api_key=key) as c, pytest.raises(LenzAuthError) as ei:
        c.usage()
    assert ei.value.message == "API key required"


def test_any_printable_ascii_key_is_sent(client: Any) -> None:
    key = "lat_" + "".join(chr(c) for c in range(0x21, 0x7F))
    with respx.mock(base_url=BASE) as r:
        route = r.get("/me/usage").respond(200, json=_USAGE)
        client.with_options(api_key=key).usage()
    assert route.calls.last.request.headers["Authorization"] == f"Bearer {key}"


# ── 10. blank input and empty optional fields ─────────────────────────────


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.assess(""),
        lambda c: c.assess("   "),
        lambda c: c.assess(text="\n"),
        lambda c: c.assess(claims=[]),
        lambda c: c.assess(claims=["A.", " "]),
        lambda c: c.assess(claims=[""]),
        lambda c: c.verify(""),
        lambda c: c.verify(text="  "),
        lambda c: c.verify_and_wait("\t"),
    ],
)
def test_blank_input_is_refused_before_sending(client: Any, call: Any) -> None:
    with respx.mock(base_url=BASE, assert_all_called=False) as r:
        sent = r.route().respond(200, json={})
        with pytest.raises(ValueError, match="claim"):
            call(client)
    assert not sent.called


def test_verify_sends_source_url_only_when_given(client: Any) -> None:
    with respx.mock(base_url=BASE) as r:
        route = r.post("/verify").respond(202, json={"task_id": "t1", "claim": "A."})
        client.verify("A.", idempotency=False)
        client.verify("A.", source_url="https://e.x/a", idempotency=False)
    assert [json.loads(c.request.content) for c in route.calls] == [
        {"text": "A."},
        {"text": "A.", "source_url": "https://e.x/a"},
    ]


# ── 9. review / citecheck: idempotency=False ──────────────────────────────


@pytest.mark.parametrize(
    ("path", "answer", "call"),
    [
        ("/review", {"review_id": "r1", "status": "queued"}, lambda c, **kw: c.review("Draft.", **kw)),
        ("/citecheck", {"citecheck_id": "c1", "status": "queued"}, lambda c, **kw: c.citecheck("Draft.", **kw)),
    ],
)
def test_a_job_submit_sends_a_key_unless_told_not_to(client: Any, path: str, answer: Any, call: Any) -> None:
    with respx.mock(base_url=BASE) as r:
        route = r.post(path).respond(202, json=answer)
        call(client)
        call(client, idempotency_key="pinned")
        call(client, idempotency=False)
        call(client, idempotency=False, idempotency_key="pinned")
    keys = [c.request.headers.get("Idempotency-Key") for c in route.calls]
    assert len(keys[0] or "") == 32
    assert keys[1:] == ["pinned", None, "pinned"]


def test_review_and_wait_passes_idempotency_false_on(client: Any) -> None:
    done = json.loads(
        (__import__("pathlib").Path(__file__).parent / "fixtures" / "contract" / "review_completed.json").read_text()
    )
    with respx.mock(base_url=BASE) as r:
        submit = r.post("/review").respond(202, json={"review_id": done["review_id"], "status": "queued"})
        r.get(f"/reviews/{done['review_id']}").respond(200, json=done)
        client.review_and_wait("Draft.", idempotency=False)
    assert "Idempotency-Key" not in submit.calls.last.request.headers


def test_a_409_naming_the_job_is_the_started_job_with_that_body_as_raw(client: Any) -> None:
    conflict = {"detail": "in flight", "code": "idempotency_conflict", "review_id": "r9"}
    with respx.mock(base_url=BASE) as r:
        r.post("/review").respond(409, json=conflict)
        started = client.review("Draft.", idempotency_key="pinned")
    assert (started.review_id, started.status) == ("r9", "queued")
    assert started.raw == conflict


# ── 3. raw ────────────────────────────────────────────────────────────────


def test_raw_is_the_body_as_received_whatever_legacy_aliases(client: Any, wire: Any) -> None:
    body = {
        "claims": [
            {"claim": "A.", "status": "failed", "failure": {"code": "timeout", "hint": "Retry."}, "future": [1]},
        ],
        "more_claims": [],
    }
    for c in (client, wire):
        with respx.mock(base_url=BASE) as r:
            r.post("/assess").respond(200, json=body)
            out = c.assess("A.")
        assert out.raw == body
        assert out.claims[0].raw == body["claims"][0]
        # A deep copy: changing it changes nothing.
        copy = out.raw
        copy["claims"][0]["claim"] = "B."
        assert out.raw == body
        assert "error_code" not in out.claims[0].raw
    # The 2.x reading (the default) filled in a field the body did not carry.
    with respx.mock(base_url=BASE) as r:
        r.post("/assess").respond(200, json=body)
        assert client.assess("A.").claims[0].error_code == "timeout"


def test_raw_on_a_poll_result_and_its_nested_verification(client: Any) -> None:
    body = {"status": "completed", "task_id": "t1", "result": {"verification_id": "v1", "claim": "A.", "x": None}}
    with respx.mock(base_url=BASE) as r:
        r.get("/verify/status/t1").respond(200, json=body)
        status = client.get_status("t1")
    assert status.raw == body
    assert status.result is not None and status.result.raw == body["result"]


def test_a_batch_item_has_no_raw() -> None:
    from lenz_io.models import BatchItemResult

    assert BatchItemResult(task_id="t", status="timeout").raw is None


def test_a_model_built_in_code_holds_its_fields() -> None:
    from lenz_io.models import ReviewStarted, TaskAccepted

    assert ReviewStarted(review_id="r1", status="queued").raw == {"review_id": "r1", "status": "queued"}
    assert TaskAccepted.model_validate({"task_id": "t", "claim": "A."}).raw == {"task_id": "t", "claim": "A."}


@pytest.mark.parametrize("key", ["lenz_abc\n", " lenz_abc", "\tlenz_abc \r\n"])
def test_whitespace_around_a_key_is_dropped(key: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LENZ_API_KEY", key)
    with respx.mock(base_url=BASE) as r:
        route = r.get("/me/usage").respond(200, json=_USAGE)
        with make_client(api_key=key, max_retries=0) as c:
            c.usage()
            c.with_options(api_key=key).usage()
        with make_client(max_retries=0) as c:
            c.usage()
    assert {call.request.headers["Authorization"] for call in route.calls} == {"Bearer lenz_abc"}


def test_nested_raw_is_the_part_as_received_not_the_2x_reading() -> None:
    from lenz_io.models import ReviewVerification

    body = {"failure": {"code": "no_checkable_claim"}}
    model = ReviewVerification.model_validate(body)
    assert model.failure is not None and model.failure.failure_reason == "not_a_claim"
    assert model.raw == body
    assert model.failure.raw == {"code": "no_checkable_claim"}


def test_raw_is_a_snapshot_taken_when_read() -> None:
    from lenz_io.models import TaskAccepted

    data = {"task_id": "t", "claim": "A.", "future": {"a": 1}}
    model = TaskAccepted.model_validate(data)
    model.future["a"] = 2  # type: ignore[attr-defined]
    data["claim"] = "B."
    assert model.raw == {"task_id": "t", "claim": "A.", "future": {"a": 1}}


def test_raw_on_a_failure_block_read_by_a_property() -> None:
    from lenz_io.models import AssessClaim, ExtractedClaims, TaskStatus

    status = TaskStatus.model_validate({"status": "failed", "task_id": "t", "failure": {"code": "no_checkable_claim"}})
    assert status.failure is not None and status.failure.failure_reason == "not_a_claim"
    assert status.failure.raw == {"code": "no_checkable_claim"}
    cancelled = TaskStatus.model_validate({"status": "cancelled", "task_id": "t"})
    assert cancelled.failure is not None and cancelled.failure.raw is None  # made up by the 2.x reading
    row = AssessClaim.model_validate({"claim": "A.", "status": "failed", "failure": {"code": "timeout"}})
    assert row.failure is not None and row.failure.raw == {"code": "timeout"}
    out = ExtractedClaims.model_validate({"claims": [{"claim": "A.", "x": 1}], "status": "ok"})
    assert out.claims[0].raw == {"claim": "A.", "x": 1}


# ── batch 2 ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.verifications.list(page_size=0),
        lambda c: c.assess(""),
        lambda c: c.verify("  "),
        lambda c: c.usage(timeout=0),
        lambda c: c.usage(extra_headers={"Authorization": "x"}),
        lambda c: c.usage(extra_headers={"X-A": "café"}),
        lambda c: c.usage(extra_headers={"X-A": "a\r\nb"}),
        lambda c: c.with_options(max_retries=-1),
        lambda c: c.get_status(""),
        lambda c: c.citecheck("Draft.", pairs=[]),
    ],
)
def test_an_argument_error_is_a_usage_error_and_a_value_error(client: Any, call: Any) -> None:
    from lenz_io import LenzUsageError

    with respx.mock(base_url=BASE, assert_all_called=False) as r:
        sent = r.route().respond(200, json={})
        with pytest.raises(LenzUsageError) as ei:
            call(client)
    assert not sent.called
    assert isinstance(ei.value, ValueError) and not isinstance(ei.value, LenzError)


@pytest.mark.parametrize("agent", ["café/1", "a\nb", " lead", 7])
def test_a_user_agent_that_cannot_be_sent_is_refused(agent: Any) -> None:
    from lenz_io import LenzUsageError

    with pytest.raises(LenzUsageError):
        make_client(api_key=KEY, user_agent=agent)


def test_the_blank_input_sentences_are_the_apis() -> None:
    with make_client(api_key=KEY) as c:
        with pytest.raises(ValueError, match=r"^claims\[1\] is blank\.$"):
            c.assess(claims=["A.", " "])
        with pytest.raises(ValueError, match=r"^claim is required\.$"):
            c.verify("")


@pytest.mark.parametrize(("claim", "text"), [("  ", "B."), ("", "B."), ("A.", "B."), ("A.", "  ")])
def test_the_alias_with_content_is_sent(client: Any, claim: str, text: str) -> None:
    with respx.mock(base_url=BASE) as r:
        v = r.post("/verify").respond(202, json={"task_id": "t1", "claim": "x"})
        a = r.post("/assess").respond(200, json={"claims": []})
        client.verify(claim, text=text, idempotency=False)
        client.assess(claim, text=text, idempotency=False)
    expected = claim if claim.strip() else text
    assert json.loads(v.calls.last.request.content)["text"] == expected
    assert json.loads(a.calls.last.request.content)["text"] == expected


@pytest.mark.parametrize("key", ["\u00a0lenz_abc", "\ufefflenz_abc", "lenz_abc\x85"])
def test_only_ascii_whitespace_is_dropped_around_a_key(key: str) -> None:
    with pytest.raises(LenzAuthError):
        make_client(api_key=key)


def test_content_type_goes_only_with_a_body(client: Any) -> None:
    with respx.mock(base_url=BASE) as r:
        get = r.get("/me/usage").respond(200, json=_USAGE)
        post = r.post("/verify/t1/cancel").respond(200, json={"task_id": "t1", "cancelled": True, "status": "x"})
        ver = r.post("/verify").respond(202, json={"task_id": "t1", "claim": "A."})
        client.usage()
        client.cancel("t1")
        client.verify("A.")
    assert "content-type" not in get.calls.last.request.headers
    assert "content-type" not in post.calls.last.request.headers
    assert ver.calls.last.request.headers["content-type"] == "application/json"


def test_a_wait_on_a_client_that_cannot_send_raises_at_once(client: Any) -> None:
    from lenz_io import LenzConnectionError

    with make_client(api_key=KEY, base_url="ftp://lenz.io/api/v1") as c:
        with pytest.raises(LenzConnectionError):
            c.wait("t1", timeout=60)
        with pytest.raises(LenzConnectionError):
            c._wait_review("r1", timeout=60)
