"""A call that sends an ``Idempotency-Key``:

* meets a 409 ``idempotency_conflict`` (the first request with that key is
  still running) by sending the SAME key and body again, after the stated
  wait or the usual backoff, within the call's retry budget; if it still
  conflicts, the error is ``retryable``;
* puts the key on every ``LenzError`` it raises (``exc.idempotency_key``), so
  a resend can reuse it and replay instead of running twice.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from lenz_io import (
    Lenz,
    LenzConnectionError,
    LenzError,
    LenzPipelineError,
    LenzRequestTimeoutError,
    LenzTimeoutError,
    LenzValidationError,
    ReviewFailed,
)

BASE = "https://lenz.io/api/v1"
_CONFLICT = {"detail": "A request with this Idempotency-Key is still being processed.", "code": "idempotency_conflict"}
_ACCEPTED = {"task_id": "t1", "claim_text": "A."}
_BATCH = {"batch_id": "b", "items": [{"task_id": "t1", "claim_text": "A."}]}


@pytest.fixture()
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    calls: list[float] = []
    monkeypatch.setattr("lenz_io.client.time.sleep", lambda s: calls.append(s))
    return calls


def _keys(route: respx.Route) -> list[str | None]:
    return [c.request.headers.get("Idempotency-Key") for c in route.calls]


def _bodies(route: respx.Route) -> list[bytes]:
    return [c.request.content for c in route.calls]


_CALLS = [
    ("/verify", _ACCEPTED, lambda c, **kw: c.verify("A.", **kw)),
    ("/verify/batch", _BATCH, lambda c, **kw: c.verify_batch(claims=[{"claim": "A."}], **kw)),
    ("/ask/v1", {"reply": "Yes."}, lambda c, **kw: c.ask.send("v1", message="Why?", **kw)),
    ("/assess", {"claims": []}, lambda c, **kw: c.assess("A.", **kw)),
    ("/extract", {"claims": []}, lambda c, **kw: c.extract(text="A.", **kw)),
    ("/verify/t0/select", _BATCH, lambda c, **kw: c.select("t0", claims=["A."], **kw)),
]


@pytest.mark.parametrize(("path", "ok", "call"), _CALLS)
class TestAConflictIsSentAgainWithTheSameKey:
    def test_then_succeeds(self, client: Lenz, slept: list[float], path, ok, call) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.post(path)
            route.side_effect = [httpx.Response(409, json=_CONFLICT), httpx.Response(200, json=ok)]
            call(client)
        assert route.call_count == 2
        keys = _keys(route)
        assert keys[0] and keys[0] == keys[1]
        assert _bodies(route)[0] == _bodies(route)[1]
        assert slept == [1.0], "the existing backoff"

    def test_a_stated_wait_is_honoured(self, client: Lenz, slept: list[float], path, ok, call) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.post(path)
            route.side_effect = [
                httpx.Response(409, json=_CONFLICT, headers={"Retry-After": "3"}),
                httpx.Response(200, json=ok),
            ]
            call(client)
        assert slept == [3.0]

    def test_a_caller_key_is_kept(self, client: Lenz, slept: list[float], path, ok, call) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.post(path)
            route.side_effect = [httpx.Response(409, json=_CONFLICT), httpx.Response(200, json=ok)]
            call(client, idempotency_key="mine")
        assert _keys(route) == ["mine", "mine"]

    def test_still_conflicting_raises_retryable_with_the_key(
        self, client: Lenz, slept: list[float], path, ok, call
    ) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.post(path).respond(409, json=_CONFLICT)
            with pytest.raises(LenzError) as ei:
                call(client)
        assert route.call_count == 4, "the call's retry budget: 1 + max_retries"
        assert len(set(_keys(route))) == 1
        err = ei.value
        assert type(err) is LenzError
        assert err.status_code == 409
        assert err.retryable is True
        assert err.idempotency_key == _keys(route)[0]
        # The 2.x attributes of that error are unchanged.
        assert err.code == ""
        assert err.message == _CONFLICT["detail"]

    def test_without_a_key_a_conflict_is_not_resent(self, client: Lenz, slept: list[float], path, ok, call) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.post(path).respond(409, json=_CONFLICT)
            with pytest.raises(LenzError) as ei:
                call(client, idempotency=False)
        assert route.call_count == 1
        assert ei.value.idempotency_key is None


def test_the_retry_budget_bounds_the_resends(slept: list[float]) -> None:
    with Lenz(api_key="lenz_test", max_retries=0) as client, respx.mock(base_url=BASE) as r:
        route = r.post("/verify").respond(409, json=_CONFLICT)
        with pytest.raises(LenzError):
            client.verify("A.")
    assert route.call_count == 1


def test_a_long_stated_wait_is_capped(client: Lenz, slept: list[float]) -> None:
    with respx.mock(base_url=BASE) as r:
        route = r.post("/verify")
        route.side_effect = [
            httpx.Response(409, json=_CONFLICT, headers={"Retry-After": "3600"}),
            httpx.Response(200, json=_ACCEPTED),
        ]
        client.verify("A.")
    assert slept == [1.0], "a wait past the cap falls back to the backoff"


def test_other_409s_are_not_resent(client: Lenz, slept: list[float]) -> None:
    with respx.mock(base_url=BASE) as r:
        route = r.post("/verify/t0/select").respond(409, json={"detail": "Nothing to select.", "code": "x"})
        with pytest.raises(LenzError):
            client.select("t0", claims=["A."])
    assert route.call_count == 1


class TestReviewAndCitecheck:
    def test_a_conflict_naming_the_review_returns_it_at_once(self, client: Lenz, slept: list[float]) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.post("/review").respond(409, json={**_CONFLICT, "review_id": "r1"})
            started = client.review("Draft.")
        assert started.review_id == "r1"
        assert route.call_count == 1

    def test_a_conflict_naming_the_check_returns_it_at_once(self, client: Lenz, slept: list[float]) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.post("/citecheck").respond(409, json={**_CONFLICT, "citecheck_id": "c1"})
            started = client.citecheck("Draft.")
        assert started.citecheck_id == "c1"
        assert route.call_count == 1

    def test_a_conflict_without_the_id_is_sent_again(self, client: Lenz, slept: list[float]) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.post("/review")
            route.side_effect = [
                httpx.Response(409, json=_CONFLICT),
                httpx.Response(202, json={"review_id": "r1", "status": "queued"}),
            ]
            started = client.review("Draft.")
        assert started.review_id == "r1"
        keys = _keys(route)
        assert keys[0] == keys[1]


class TestTheErrorCarriesTheKey:
    def test_a_refused_call(self, client: Lenz) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.post("/verify").respond(422, json={"detail": "Bad.", "code": "validation_error"})
            with pytest.raises(LenzValidationError) as ei:
                client.verify("A.")
        assert ei.value.idempotency_key == route.calls.last.request.headers["Idempotency-Key"]

    @pytest.mark.parametrize(
        ("failure", "cls"),
        [(httpx.ConnectError("refused"), LenzConnectionError), (httpx.ReadTimeout("slow"), LenzRequestTimeoutError)],
    )
    def test_a_connection_failure(self, client: Lenz, slept: list[float], failure, cls) -> None:
        with respx.mock(base_url=BASE) as r:
            r.post("/assess").mock(side_effect=failure)
            with pytest.raises(cls) as ei:
                client.assess("A.", idempotency_key="k-1")
        assert ei.value.idempotency_key == "k-1"

    def test_the_wait_of_a_submit_and_wait_call(self, client: Lenz) -> None:
        with respx.mock(base_url=BASE) as r:
            submit = r.post("/verify").respond(200, json=_ACCEPTED)
            r.get("/verify/status/t1").respond(200, json={"status": "failed", "failure_reason": "x"})
            with pytest.raises(LenzPipelineError) as ei:
                client.verify_and_wait("A.")
        assert ei.value.idempotency_key == submit.calls.last.request.headers["Idempotency-Key"]

    def test_a_wait_timeout_of_a_submit_and_wait_call(self, client: Lenz, slept: list[float]) -> None:
        with respx.mock(base_url=BASE) as r:
            r.post("/verify").respond(200, json=_ACCEPTED)
            r.get("/verify/status/t1").respond(200, json={"status": "processing", "progress": {}})
            with pytest.raises(LenzTimeoutError) as ei:
                client.verify_and_wait("A.", timeout=0, idempotency_key="k-2")
        assert ei.value.idempotency_key == "k-2"

    def test_a_failed_review(self, client: Lenz, slept: list[float]) -> None:
        failed = {
            "review_id": "r1",
            "status": "failed",
            "issues": [],
            "failures": [],
            "claims": [],
            "failure": {"failure_reason": "internal"},
        }
        with respx.mock(base_url=BASE) as r:
            submit = r.post("/review").respond(202, json={"review_id": "r1", "status": "queued"})
            r.get("/reviews/r1").respond(200, json=failed)
            with pytest.raises(ReviewFailed) as ei:
                client.review_and_wait("Draft.")
        assert ei.value.idempotency_key == submit.calls.last.request.headers["Idempotency-Key"]

    def test_a_batch_submit_and_wait(self, client: Lenz) -> None:
        with respx.mock(base_url=BASE) as r:
            r.post("/verify/batch").respond(402, json={"detail": "No credits.", "code": "no_credits"})
            with pytest.raises(LenzError) as ei:
                client.verify_batch_and_wait(claims=[{"claim": "A."}], idempotency_key="k-3")
        assert ei.value.idempotency_key == "k-3"

    def test_a_call_that_sent_no_key(self, client: Lenz) -> None:
        with respx.mock(base_url=BASE) as r:
            r.get("/me/usage").respond(404, json={"detail": "x"})
            r.get("/verify/status/t1").respond(404, json={"detail": "x"})
            r.post("/verify").respond(422, json={"detail": "Bad."})
            with pytest.raises(LenzError) as usage:
                client.usage()
            with pytest.raises(LenzError) as wait:
                client.wait("t1")
            with pytest.raises(LenzError) as opted_out:
                client.verify("A.", idempotency=False)
        assert usage.value.idempotency_key is None
        assert wait.value.idempotency_key is None
        assert opted_out.value.idempotency_key is None


def test_the_body_sent_again_is_byte_identical(client: Lenz, slept: list[float]) -> None:
    with respx.mock(base_url=BASE) as r:
        route = r.post("/verify/batch")
        route.side_effect = [httpx.Response(409, json=_CONFLICT), httpx.Response(200, json=_BATCH)]
        client.verify_batch(claims=[{"claim": "A.", "depth": "low"}], language="de")
    first, second = _bodies(route)
    assert first == second
    assert json.loads(first)["language"] == "de"
