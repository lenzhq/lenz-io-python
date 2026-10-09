"""The wait helpers stop at once on an error no later poll can change.

Before 3.0 ``wait`` (and so ``verify_and_wait`` and ``verify_batch_and_wait``)
retried every failed poll until its deadline, so a revoked key or a wrong
task id surfaced minutes later as a misleading ``LenzTimeoutError``. A 401,
403 or 404 (and a version error) now ends the wait with that error; a 5xx, a
429 or a network failure is still retried on the next round. Each poll is one
HTTP request bounded by what is left of the deadline.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from lenz_io import (
    Lenz,
    LenzAuthError,
    LenzError,
    LenzNotFoundError,
    LenzTimeoutError,
)

BASE = "https://lenz.io/api/v1"
_DONE = {"status": "completed", "task_id": "t", "result": {"verification_id": "v1", "claim": "A."}}
_RUNNING = {"status": "processing", "task_id": "t", "progress": {"step": "research"}}


@pytest.fixture()
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    calls: list[float] = []
    monkeypatch.setattr("lenz_io.client.time.sleep", lambda s: calls.append(s))
    return calls


@pytest.mark.parametrize(
    ("status", "body", "cls"),
    [
        (401, {"detail": "Invalid API key."}, LenzAuthError),
        (403, {"detail": "Forbidden."}, LenzAuthError),
        (404, {"detail": "Not found."}, LenzNotFoundError),
    ],
)
class TestPermanentErrorsStopTheWait:
    def test_wait_raises_it_at_once(self, client: Lenz, slept: list[float], status, body, cls) -> None:
        with respx.mock(base_url=BASE) as r:
            poll = r.get("/verify/status/t")
            poll.side_effect = [httpx.Response(200, json=_RUNNING), httpx.Response(status, json=body)]
            with pytest.raises(cls) as ei:
                client.wait("t", timeout=300)
        assert poll.call_count == 2
        assert len(slept) == 1, "no sleep after the permanent error"
        assert not isinstance(ei.value, LenzTimeoutError)
        assert ei.value.status_code == status

    def test_verify_and_wait_raises_it(self, client: Lenz, slept: list[float], status, body, cls) -> None:
        with respx.mock(base_url=BASE) as r:
            r.post("/verify").respond(200, json={"task_id": "t"})
            poll = r.get("/verify/status/t").respond(status, json=body)
            with pytest.raises(cls):
                client.verify_and_wait("A.", timeout=300)
        assert poll.call_count == 1
        assert slept == []

    def test_a_batch_item_fails_and_the_others_continue(
        self, client: Lenz, slept: list[float], status, body, cls
    ) -> None:
        with respx.mock(base_url=BASE) as r:
            r.post("/verify/batch").respond(
                200, json={"batch_id": "b", "items": [{"task_id": "a", "claim": "A."}, {"task_id": "t", "claim": "B."}]}
            )
            bad = r.get("/verify/status/a").respond(status, json=body)
            good = r.get("/verify/status/t")
            good.side_effect = [httpx.Response(200, json=_RUNNING), httpx.Response(200, json=_DONE)]
            results = client.verify_batch_and_wait(claims=[{"claim": "A."}, {"claim": "B."}], timeout=300)
        assert [x.status for x in results] == ["failed", "completed"]
        assert results[0].status_detail is None
        assert results[0].verification is None
        assert bad.call_count == 1
        assert good.call_count == 2


class TestTransientErrorsAreRetried:
    @pytest.mark.parametrize(
        "failure",
        [
            httpx.Response(500, json={"detail": "boom"}),
            httpx.Response(502, text="bad gateway"),
            httpx.Response(503, json={"detail": "unavailable"}),
            httpx.Response(429, json={"detail": "slow down"}),
            httpx.ConnectError("refused"),
            httpx.ReadTimeout("slow"),
        ],
    )
    def test_the_next_round_polls_again(self, client: Lenz, slept: list[float], failure) -> None:
        with respx.mock(base_url=BASE) as r:
            poll = r.get("/verify/status/t")
            poll.side_effect = [failure, httpx.Response(200, json=_DONE)]
            out = client.wait("t", timeout=300)
        assert out.verification_id == "v1"
        assert poll.call_count == 2

    def test_a_stated_wait_paces_the_next_poll(self, client: Lenz, slept: list[float]) -> None:
        with respx.mock(base_url=BASE) as r:
            poll = r.get("/verify/status/t")
            poll.side_effect = [
                httpx.Response(429, json={"detail": "slow down"}, headers={"Retry-After": "7"}),
                httpx.Response(200, json=_DONE),
            ]
            client.wait("t", timeout=300)
        assert slept == [7.0]

    def test_a_persistent_5xx_still_ends_in_a_timeout(self, client: Lenz, slept: list[float]) -> None:
        with respx.mock(base_url=BASE) as r:
            r.get("/verify/status/t").respond(500, json={"detail": "boom"})
            with pytest.raises(LenzTimeoutError):
                client.wait("t", timeout=0)

    def test_other_4xx_keep_the_2x_behaviour(self, client: Lenz, slept: list[float]) -> None:
        # Only 401, 403, 404 (and 410, a version error) end a wait early.
        with respx.mock(base_url=BASE) as r:
            poll = r.get("/verify/status/t")
            poll.side_effect = [httpx.Response(409, json={"detail": "busy"}), httpx.Response(200, json=_DONE)]
            client.wait("t", timeout=300)
        assert poll.call_count == 2


class TestEachPollIsBounded:
    def test_one_request_per_poll(self, client: Lenz, slept: list[float]) -> None:
        # A failed poll is retried on the next round, never inside the poll by
        # the client's own retry ladder (which could run past the deadline).
        with respx.mock(base_url=BASE) as r:
            poll = r.get("/verify/status/t")
            poll.side_effect = [httpx.Response(503, json={"detail": "x"}), httpx.Response(200, json=_DONE)]
            client.wait("t", timeout=300)
        assert poll.call_count == 2
        assert len(slept) == 1

    def test_the_request_timeout_never_runs_past_the_deadline(self, slept: list[float]) -> None:
        with Lenz(api_key="lenz_test", timeout=60.0) as client, respx.mock(base_url=BASE) as r:
            poll = r.get("/verify/status/t")
            poll.side_effect = [httpx.Response(200, json=_RUNNING), httpx.Response(200, json=_DONE)]
            client.wait("t", timeout=5)
        timeouts = [c.request.extensions["timeout"]["read"] for c in poll.calls]
        assert all(t <= 5 for t in timeouts), timeouts

    def test_the_final_poll_still_runs_with_a_floor(self, slept: list[float]) -> None:
        # Past the deadline the loop still polls once more, with at least 1s.
        with Lenz(api_key="lenz_test") as client, respx.mock(base_url=BASE) as r:
            poll = r.get("/verify/status/t").respond(200, json=_RUNNING)
            with pytest.raises(LenzTimeoutError):
                client.wait("t", timeout=0)
        assert poll.calls.last.request.extensions["timeout"]["read"] == 1.0

    def test_the_client_timeout_bounds_a_long_wait(self, slept: list[float]) -> None:
        with Lenz(api_key="lenz_test", timeout=20.0) as client, respx.mock(base_url=BASE) as r:
            poll = r.get("/verify/status/t").respond(200, json=_DONE)
            client.wait("t", timeout=300)
        assert poll.calls.last.request.extensions["timeout"]["read"] == 20.0


class TestJobWaits:
    """``review_and_wait`` and ``citecheck_and_wait`` already stopped on a
    401, 403 or 404 and retried the rest; pinned here beside the others."""

    @pytest.mark.parametrize(("status", "cls"), [(401, LenzAuthError), (403, LenzAuthError), (404, LenzNotFoundError)])
    def test_review_wait_raises_at_once(self, client: Lenz, slept: list[float], status, cls) -> None:
        with respx.mock(base_url=BASE) as r:
            r.post("/review").respond(202, json={"review_id": "r1", "status": "queued"})
            poll = r.get("/reviews/r1").respond(status, json={"detail": "x", "code": "not_found"})
            with pytest.raises(cls):
                client.review_and_wait("Draft.", timeout=300)
        assert poll.call_count == 1

    @pytest.mark.parametrize(("status", "cls"), [(401, LenzAuthError), (404, LenzNotFoundError)])
    def test_citecheck_wait_raises_at_once(self, client: Lenz, slept: list[float], status, cls) -> None:
        with respx.mock(base_url=BASE) as r:
            r.post("/citecheck").respond(202, json={"citecheck_id": "c1", "status": "queued"})
            poll = r.get("/citechecks/c1").respond(status, json={"detail": "x"})
            with pytest.raises(cls):
                client.citecheck_and_wait("Draft.", timeout=300)
        assert poll.call_count == 1

    def test_a_connection_error_mid_wait_is_retried(self, client: Lenz, slept: list[float]) -> None:
        with respx.mock(base_url=BASE) as r:
            r.post("/citecheck").respond(202, json={"citecheck_id": "c1", "status": "queued"})
            poll = r.get("/citechecks/c1")
            poll.side_effect = [httpx.ConnectError("refused"), httpx.Response(404, json={"detail": "x"})]
            with pytest.raises(LenzError):
                client.citecheck_and_wait("Draft.", timeout=300)
        assert poll.call_count == 2
