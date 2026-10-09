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
    LenzApiVersionError,
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

    def test_a_batch(self, client: Lenz, slept: list[float], status, body, cls) -> None:
        # A 404 is that item's outcome; a 401 / 403 is the whole account's, so
        # the batch wait raises it (before the other item is polled).
        with respx.mock(base_url=BASE, assert_all_called=False) as r:
            r.post("/verify/batch").respond(
                200, json={"batch_id": "b", "items": [{"task_id": "a", "claim": "A."}, {"task_id": "t", "claim": "B."}]}
            )
            bad = r.get("/verify/status/a").respond(status, json=body)
            good = r.get("/verify/status/t")
            good.side_effect = [httpx.Response(200, json=_RUNNING), httpx.Response(200, json=_DONE)]
            if status in (401, 403):
                with pytest.raises(cls):
                    client.verify_batch_and_wait(claims=[{"claim": "A."}, {"claim": "B."}], timeout=300)
                assert bad.call_count == 1
                return
            results = client.verify_batch_and_wait(claims=[{"claim": "A."}, {"claim": "B."}], timeout=300)
        assert [x.status for x in results] == ["failed", "completed"]
        assert results[0].status_detail is None
        assert results[0].verification is None
        assert bad.call_count == 1
        assert good.call_count == 2


_OLD = {"X-Lenz-API-Version": "2026-05-13"}


class TestVersionErrorsInAWait:
    def test_a_batch_item_fails_and_the_others_continue(self, client: Lenz, slept: list[float]) -> None:
        with respx.mock(base_url=BASE) as r:
            r.post("/verify/batch").respond(
                200, json={"batch_id": "b", "items": [{"task_id": "a", "claim": "A."}, {"task_id": "t", "claim": "B."}]}
            )
            bad = r.get("/verify/status/a").respond(200, json=_DONE, headers=_OLD)
            good = r.get("/verify/status/t")
            good.side_effect = [httpx.Response(200, json=_RUNNING), httpx.Response(200, json=_DONE)]
            results = client.verify_batch_and_wait(claims=[{"claim": "A."}, {"claim": "B."}], timeout=300)
        assert [x.status for x in results] == ["failed", "completed"]
        assert results[0].status_detail is None
        assert bad.call_count == 1

    def test_a_single_wait_raises_it(self, client: Lenz, slept: list[float]) -> None:
        with respx.mock(base_url=BASE) as r:
            poll = r.get("/verify/status/t").respond(200, json=_DONE, headers=_OLD)
            with pytest.raises(LenzApiVersionError):
                client.wait("t", timeout=300)
        assert poll.call_count == 1


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

    def test_a_zero_timeout_still_reads_once(self, slept: list[float]) -> None:
        # As in 2.x, ``timeout=0`` reads the status once (bounded by the
        # client timeout, there being no deadline left to bound it), then stops.
        with Lenz(api_key="lenz_test", timeout=20.0) as client, respx.mock(base_url=BASE) as r:
            poll = r.get("/verify/status/t").respond(200, json=_RUNNING)
            with pytest.raises(LenzTimeoutError):
                client.wait("t", timeout=0)
        assert poll.call_count == 1
        assert poll.calls.last.request.extensions["timeout"]["read"] == 20.0
        assert slept == []

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


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """A fake clock: ``time.sleep`` moves it, requests take no time unless a
    test moves it."""
    now = [0.0]
    monkeypatch.setattr("lenz_io.client.time.monotonic", lambda: now[0])
    monkeypatch.setattr("lenz_io.client.time.sleep", lambda s: now.__setitem__(0, now[0] + s))
    return now


def _read_timeouts(route: respx.Route) -> list[float | None]:
    return [c.request.extensions["timeout"]["read"] for c in route.calls]


class TestNoPollPastTheDeadline:
    def test_no_poll_starts_once_the_deadline_is_spent(self, clock: list[float]) -> None:
        with Lenz(api_key="lenz_test", timeout=30.0) as client, respx.mock(base_url=BASE) as r:
            poll = r.get("/verify/status/t").respond(200, json=_RUNNING)
            with pytest.raises(LenzTimeoutError):
                client.wait("t", timeout=5)
        # t=0 (5s left), sleep 2, t=2 (3s left), sleep 3, t=5: nothing left.
        assert _read_timeouts(poll) == [5.0, 3.0]
        assert clock[0] == 5.0

    def test_a_smaller_client_timeout_bounds_every_poll(self, clock: list[float]) -> None:
        with Lenz(api_key="lenz_test", timeout=2.5) as client, respx.mock(base_url=BASE) as r:
            poll = r.get("/verify/status/t").respond(200, json=_RUNNING)
            with pytest.raises(LenzTimeoutError):
                client.wait("t", timeout=5)
        assert _read_timeouts(poll) == [2.5, 2.5]

    def test_a_batch_marks_the_items_it_had_no_time_for_as_timed_out(self, client: Lenz, clock: list[float]) -> None:
        polls: list[str] = []

        def answer(request: httpx.Request) -> httpx.Response:
            polls.append(request.url.path.rsplit("/", 1)[-1])
            if len(polls) == 3:
                clock[0] += 100.0  # the second round's first poll is slow
            return httpx.Response(200, json=_RUNNING)

        with respx.mock(base_url=BASE) as r:
            r.post("/verify/batch").respond(
                200, json={"batch_id": "b", "items": [{"task_id": "a", "claim": "A."}, {"task_id": "b", "claim": "B."}]}
            )
            r.get(url__regex=r"/verify/status/.*").mock(side_effect=answer)
            results = client.verify_batch_and_wait(claims=[{"claim": "A."}, {"claim": "B."}], timeout=10)
        assert polls == ["a", "b", "a"], "b is not polled after the deadline"
        assert [x.status for x in results] == ["timeout", "timeout"]

    @pytest.mark.parametrize(
        ("submit", "path", "body", "call"),
        [
            (
                "/review",
                "/reviews/r1",
                {"review_id": "r1", "status": "verifying", "issues": [], "failures": [], "claims": []},
                lambda c: c.review_and_wait("Draft.", timeout=12),
            ),
            (
                "/citecheck",
                "/citechecks/c1",
                {
                    "citecheck_id": "c1",
                    "status": "checking",
                    "citations": [],
                    "citation_issues": [],
                    "citation_failures": [],
                },
                lambda c: c.citecheck_and_wait("Draft.", timeout=12),
            ),
        ],
    )
    def test_job_waits_stop_at_the_deadline(self, clock: list[float], submit, path, body, call) -> None:
        accepted = {"review_id": "r1", "citecheck_id": "c1", "status": "queued"}
        with Lenz(api_key="lenz_test", timeout=30.0) as client, respx.mock(base_url=BASE) as r:
            r.post(submit).respond(202, json=accepted)
            poll = r.get(path).respond(200, json=body)
            with pytest.raises(LenzTimeoutError):
                call(client)
        # t=0 (12 left), sleep 10, t=10 (2 left), sleep 2, t=12: nothing left.
        assert _read_timeouts(poll) == [12.0, 2.0]


class TestTheClientTimeoutSetting:
    """The per-poll bound reads the client's own timeout, whatever form it
    took (2.x accepted ``None`` and an ``httpx.Timeout`` there)."""

    @pytest.mark.parametrize(
        ("timeout", "expected"),
        [(None, 300.0), (4.0, 4.0), (httpx.Timeout(7.0), 7.0), (httpx.Timeout(10.0, read=6.0), 6.0)],
    )
    def test_wait(self, slept: list[float], timeout, expected) -> None:
        with Lenz(api_key="lenz_test", timeout=timeout) as client, respx.mock(base_url=BASE) as r:
            poll = r.get("/verify/status/t").respond(200, json=_DONE)
            client.wait("t", timeout=300)
        assert _read_timeouts(poll) == [pytest.approx(expected, abs=1.0)]

    @pytest.mark.parametrize("timeout", [None, httpx.Timeout(7.0)])
    def test_review_and_citecheck_waits(self, slept: list[float], timeout) -> None:
        done_review = {"review_id": "r1", "status": "completed", "issues": [], "failures": [], "claims": []}
        done_check = {
            "citecheck_id": "c1",
            "status": "completed",
            "citations": [],
            "citation_issues": [],
            "citation_failures": [],
        }
        with Lenz(api_key="lenz_test", timeout=timeout) as client, respx.mock(base_url=BASE) as r:
            r.post("/review").respond(202, json={"review_id": "r1", "status": "queued"})
            r.get("/reviews/r1").respond(200, json=done_review)
            r.post("/citecheck").respond(202, json={"citecheck_id": "c1", "status": "queued"})
            r.get("/citechecks/c1").respond(200, json=done_check)
            assert client.review_and_wait("Draft.").status == "completed"
            assert client.citecheck_and_wait("Draft.").status == "completed"

    def test_an_injected_http_client_keeps_its_own_timeout(self, slept: list[float]) -> None:
        with httpx.Client(timeout=9.0) as http, Lenz(api_key="lenz_test", http_client=http) as client:
            with respx.mock(base_url=BASE) as r:
                poll = r.get("/verify/status/t").respond(200, json=_DONE)
                client.wait("t", timeout=300)
        assert _read_timeouts(poll) == [9.0]

    def test_an_injected_unbounded_http_client(self, slept: list[float]) -> None:
        with httpx.Client(timeout=None) as http, Lenz(api_key="lenz_test", http_client=http) as client:
            with respx.mock(base_url=BASE) as r:
                poll = r.get("/verify/status/t").respond(200, json=_DONE)
                client.wait("t", timeout=300)
        assert _read_timeouts(poll) == [pytest.approx(300.0, abs=1.0)]
