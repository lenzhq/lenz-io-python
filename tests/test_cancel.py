"""Stopping a run: ``cancel``, ``cancel_review`` and ``cancel_citecheck``.

The bodies are the API's own (``tests/fixtures/contract/cancel_*.json`` and
``error_cancel_*.json``). A cancel is safe to repeat, so it carries no
``Idempotency-Key`` and no body; it answers 200 whatever the state of the run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from lenz_io import (
    CancelResult,
    Citecheck,
    Lenz,
    LenzApiVersionError,
    LenzAuthError,
    LenzError,
    LenzNotFoundError,
    LenzPipelineError,
    ReviewFull,
)

BASE = "https://lenz.io/api/v1"
FIXTURES = Path(__file__).parent / "fixtures" / "contract"
TASK = "3f2a9c1e5b7d4a608c1d2e3f4a5b6c7d"
REVIEW = "d6b2bd72"
CHECK = "12bbbf65"


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture()
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    calls: list[float] = []
    monkeypatch.setattr("lenz_io.client.time.sleep", lambda s: calls.append(s))
    return calls


# ── cancel(task_id) ──


class TestCancel:
    def test_a_run_it_stopped(self, client: Lenz) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.post(f"/verify/{TASK}/cancel").respond(200, json=_load("cancel_verify_cancelled.json"))
            result = client.cancel(TASK)
        assert isinstance(result, CancelResult)
        assert (result.task_id, result.cancelled, result.status) == (TASK, True, "cancelled")
        assert route.call_count == 1

    def test_a_run_that_finished_first_is_not_an_error(self, client: Lenz) -> None:
        with respx.mock(base_url=BASE) as r:
            r.post(f"/verify/{TASK}/cancel").respond(200, json=_load("cancel_verify_completed.json"))
            result = client.cancel(TASK)
        assert (result.task_id, result.cancelled, result.status) == (TASK, False, "completed")

    def test_the_request_is_a_bare_post_with_no_key(self, client: Lenz) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.post(f"/verify/{TASK}/cancel").respond(200, json=_load("cancel_verify_cancelled.json"))
            client.cancel(TASK)
        request = route.calls.last.request
        assert request.method == "POST"
        assert request.content == b""
        assert "Idempotency-Key" not in request.headers
        assert request.headers["X-Lenz-API-Version"] == "2026-10-11"
        assert request.headers["Authorization"].startswith("Bearer ")

    def test_a_field_the_server_adds_is_kept(self, client: Lenz) -> None:
        body = _load("cancel_verify_cancelled.json") | {"refunded": 10}
        with respx.mock(base_url=BASE) as r:
            r.post(f"/verify/{TASK}/cancel").respond(200, json=body)
            result = client.cancel(TASK)
        assert result.model_dump()["refunded"] == 10

    @pytest.mark.parametrize("task_id", ["", None])
    def test_an_empty_id_sends_nothing(self, client: Lenz, task_id: Any) -> None:
        with respx.mock(base_url=BASE, assert_all_called=False) as r:
            route = r.post(url__regex=r".*").respond(200, json={})
            with pytest.raises(ValueError, match="cancel"):
                client.cancel(task_id)
        assert route.call_count == 0

    def test_a_wait_after_a_cancel_raises_the_failed_error(self, client: Lenz, slept: list[float]) -> None:
        status = {"status": "cancelled", "task_id": TASK}
        with respx.mock(base_url=BASE) as r:
            r.post(f"/verify/{TASK}/cancel").respond(200, json=_load("cancel_verify_cancelled.json"))
            r.get(f"/verify/status/{TASK}").respond(200, json=status)
            assert client.cancel(TASK).cancelled is True
            with pytest.raises(LenzPipelineError) as ei:
                client.wait(TASK, timeout=30)
        assert (ei.value.failure_class, ei.value.retryable) == ("cancelled", False)

    @pytest.mark.parametrize("name", ["error_cancel_task_404.json"])
    def test_an_unknown_other_account_or_web_task_is_a_not_found(
        self, client: Lenz, slept: list[float], name: str
    ) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.post(f"/verify/{TASK}/cancel").respond(404, json=_load(name))
            with pytest.raises(LenzNotFoundError) as ei:
                client.cancel(TASK)
        assert (ei.value.status_code, ei.value.code, ei.value.retryable) == (404, "not_found", False)
        assert ei.value.body["code"] == "not_found"
        assert route.call_count == 1 and slept == []

    def test_a_reviews_deep_check_is_a_409_that_names_the_review_door(self, client: Lenz, slept: list[float]) -> None:
        body = _load("error_cancel_use_review_cancel_409.json")
        with respx.mock(base_url=BASE) as r:
            route = r.post(f"/verify/{TASK}/cancel").respond(409, json=body)
            with pytest.raises(LenzError) as ei:
                client.cancel(TASK)
        err = ei.value
        assert (err.status_code, err.code) == (409, "use_review_cancel")
        assert err.body == body
        assert err.retryable is False
        assert err.idempotency_key is None
        assert "cancel_review" in err.fix
        # Not the in-flight 409: one request, no wait, no resend.
        assert route.call_count == 1
        assert slept == []

    def test_the_review_409_is_not_resent_even_when_it_states_a_wait(self, client: Lenz, slept: list[float]) -> None:
        body = _load("error_cancel_use_review_cancel_409.json") | {"retry_after": 1}
        with respx.mock(base_url=BASE) as r:
            route = r.post(f"/verify/{TASK}/cancel").respond(409, json=body, headers={"Retry-After": "1"})
            with pytest.raises(LenzError) as ei:
                client.cancel(TASK)
        assert ei.value.code == "use_review_cancel"
        assert route.call_count == 1 and slept == []

    def test_an_in_flight_409_is_not_a_reason_to_resend_either(self, client: Lenz, slept: list[float]) -> None:
        # The resend rule is for a request that carries a key; a cancel has none.
        body = {"detail": "busy", "code": "idempotency_conflict"}
        with respx.mock(base_url=BASE) as r:
            route = r.post(f"/verify/{TASK}/cancel").respond(409, json=body)
            with pytest.raises(LenzError):
                client.cancel(TASK)
        assert route.call_count == 1 and slept == []

    def test_a_server_error_is_retried_like_any_safe_call(self, client: Lenz, slept: list[float]) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.post(f"/verify/{TASK}/cancel")
            route.side_effect = [
                httpx.Response(502, json={"detail": "bad gateway"}),
                httpx.Response(200, json=_load("cancel_verify_cancelled.json")),
            ]
            result = client.cancel(TASK)
        assert result.cancelled is True
        assert route.call_count == 2
        assert len(slept) == 1

    def test_a_dropped_connection_is_retried(self, client: Lenz, slept: list[float]) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.post(f"/verify/{TASK}/cancel")
            route.side_effect = [
                httpx.ConnectError("reset"),
                httpx.Response(200, json=_load("cancel_verify_cancelled.json")),
            ]
            assert client.cancel(TASK).cancelled is True
        assert route.call_count == 2

    def test_a_bad_key_is_an_auth_error(self, client: Lenz) -> None:
        with respx.mock(base_url=BASE) as r:
            r.post(f"/verify/{TASK}/cancel").respond(401, json={"detail": "Invalid API key.", "code": "invalid_key"})
            with pytest.raises(LenzAuthError):
                client.cancel(TASK)

    def test_it_needs_a_key(self, unauth_client: Lenz) -> None:
        with respx.mock(base_url=BASE, assert_all_called=False) as r:
            route = r.post(url__regex=r".*").respond(200, json={})
            with pytest.raises(LenzAuthError):
                unauth_client.cancel(TASK)
        assert route.call_count == 0

    def test_an_answer_in_another_version_is_refused(self, client: Lenz) -> None:
        with respx.mock(base_url=BASE) as r:
            r.post(f"/verify/{TASK}/cancel").respond(
                200,
                json={"task_id": TASK, "cancelled": True, "status": "failed"},
                headers={"X-Lenz-API-Version": "2026-05-13"},
            )
            with pytest.raises(LenzApiVersionError):
                client.cancel(TASK)


# ── cancel_review(review_id) ──


class TestCancelReview:
    def test_a_review_it_stopped_is_the_full_view(self, client: Lenz) -> None:
        body = _load("cancel_review_cancelled.json")
        with respx.mock(base_url=BASE) as r:
            route = r.post(f"/reviews/{REVIEW}/cancel").respond(200, json=body)
            review = client.cancel_review(REVIEW)
        assert isinstance(review, ReviewFull)
        assert (review.review_id, review.status, review.outcome) == (REVIEW, "cancelled", "incomplete")
        assert review.credits.charged == 2
        assert len(review.claims) == 2
        assert route.call_count == 1

    def test_it_is_the_model_get_review_returns(self, client: Lenz) -> None:
        body = _load("cancel_review_cancelled.json")
        with respx.mock(base_url=BASE) as r:
            r.post(f"/reviews/{REVIEW}/cancel").respond(200, json=body)
            r.get(f"/reviews/{REVIEW}").respond(200, json=body)
            cancelled = client.cancel_review(REVIEW)
            read = client.get_review(REVIEW)
        assert type(cancelled) is type(read)
        assert cancelled.model_dump() == read.model_dump()

    @pytest.mark.parametrize(
        ("name", "status"),
        [
            ("cancel_review_completed.json", "completed"),
            ("cancel_review_already_cancelled.json", "cancelled"),
        ],
    )
    def test_a_review_that_ended_is_returned_as_it_stands(self, client: Lenz, name: str, status: str) -> None:
        with respx.mock(base_url=BASE) as r:
            r.post(f"/reviews/{REVIEW}/cancel").respond(200, json=_load(name))
            assert client.cancel_review(REVIEW).status == status

    def test_the_request_is_a_bare_post_with_no_key(self, client: Lenz) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.post(f"/reviews/{REVIEW}/cancel").respond(200, json=_load("cancel_review_cancelled.json"))
            client.cancel_review(REVIEW)
        request = route.calls.last.request
        assert request.method == "POST"
        assert request.content == b""
        assert "Idempotency-Key" not in request.headers
        assert request.url.params.get("view") is None

    @pytest.mark.parametrize("review_id", ["", None])
    def test_an_empty_id_sends_nothing(self, client: Lenz, review_id: Any) -> None:
        with respx.mock(base_url=BASE, assert_all_called=False) as r:
            route = r.post(url__regex=r".*").respond(200, json={})
            with pytest.raises(ValueError, match="cancel_review"):
                client.cancel_review(review_id)
        assert route.call_count == 0

    def test_an_unknown_or_other_accounts_review_is_a_not_found(self, client: Lenz, slept: list[float]) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.post(f"/reviews/{REVIEW}/cancel").respond(404, json=_load("error_cancel_review_404.json"))
            with pytest.raises(LenzNotFoundError) as ei:
                client.cancel_review(REVIEW)
        assert (ei.value.status_code, ei.value.code) == (404, "not_found")
        assert ei.value.body["code"] == "not_found"
        assert route.call_count == 1 and slept == []

    def test_a_server_error_is_retried(self, client: Lenz, slept: list[float]) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.post(f"/reviews/{REVIEW}/cancel")
            route.side_effect = [
                httpx.Response(503, json={"detail": "down"}),
                httpx.Response(200, json=_load("cancel_review_cancelled.json")),
            ]
            assert client.cancel_review(REVIEW).status == "cancelled"
        assert route.call_count == 2

    def test_an_answer_in_another_version_is_refused(self, client: Lenz) -> None:
        with respx.mock(base_url=BASE) as r:
            r.post(f"/reviews/{REVIEW}/cancel").respond(
                200, json=_load("cancel_review_cancelled.json"), headers={"X-Lenz-API-Version": "2026-05-13"}
            )
            with pytest.raises(LenzApiVersionError):
                client.cancel_review(REVIEW)

    def test_it_needs_a_key(self, unauth_client: Lenz) -> None:
        with pytest.raises(LenzAuthError):
            unauth_client.cancel_review(REVIEW)


# ── cancel_citecheck(citecheck_id) ──


class TestCancelCitecheck:
    def test_a_check_it_stopped_is_the_citecheck_model(self, client: Lenz) -> None:
        body = _load("cancel_citecheck_cancelled.json")
        with respx.mock(base_url=BASE) as r:
            route = r.post(f"/citechecks/{CHECK}/cancel").respond(200, json=body)
            check = client.cancel_citecheck(CHECK)
        assert isinstance(check, Citecheck)
        assert (check.citecheck_id, check.status, check.outcome) == (CHECK, "cancelled", "incomplete")
        assert check.credits.charged == 0
        assert route.call_count == 1

    def test_it_is_the_model_get_citecheck_returns(self, client: Lenz) -> None:
        body = _load("cancel_citecheck_cancelled.json")
        with respx.mock(base_url=BASE) as r:
            r.post(f"/citechecks/{CHECK}/cancel").respond(200, json=body)
            r.get(f"/citechecks/{CHECK}").respond(200, json=body)
            cancelled = client.cancel_citecheck(CHECK)
            read = client.get_citecheck(CHECK)
        assert type(cancelled) is type(read)
        assert cancelled.model_dump() == read.model_dump()

    @pytest.mark.parametrize(
        ("name", "status"),
        [
            ("cancel_citecheck_completed.json", "completed"),
            ("cancel_citecheck_already_cancelled.json", "cancelled"),
        ],
    )
    def test_a_check_that_ended_is_returned_as_it_stands(self, client: Lenz, name: str, status: str) -> None:
        with respx.mock(base_url=BASE) as r:
            r.post(f"/citechecks/{CHECK}/cancel").respond(200, json=_load(name))
            assert client.cancel_citecheck(CHECK).status == status

    def test_the_request_is_a_bare_post_with_no_key(self, client: Lenz) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.post(f"/citechecks/{CHECK}/cancel").respond(200, json=_load("cancel_citecheck_cancelled.json"))
            client.cancel_citecheck(CHECK)
        request = route.calls.last.request
        assert request.method == "POST"
        assert request.content == b""
        assert "Idempotency-Key" not in request.headers

    @pytest.mark.parametrize("citecheck_id", ["", None])
    def test_an_empty_id_sends_nothing(self, client: Lenz, citecheck_id: Any) -> None:
        with respx.mock(base_url=BASE, assert_all_called=False) as r:
            route = r.post(url__regex=r".*").respond(200, json={})
            with pytest.raises(ValueError, match="cancel_citecheck"):
                client.cancel_citecheck(citecheck_id)
        assert route.call_count == 0

    @pytest.mark.parametrize("name", ["error_cancel_citecheck_404.json"])
    def test_an_unknown_check_or_a_review_id_is_a_not_found(self, client: Lenz, slept: list[float], name: str) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.post(f"/citechecks/{CHECK}/cancel").respond(404, json=_load(name))
            with pytest.raises(LenzNotFoundError) as ei:
                client.cancel_citecheck(CHECK)
        assert (ei.value.status_code, ei.value.code) == (404, "not_found")
        assert ei.value.body["code"] == "not_found"
        assert route.call_count == 1 and slept == []

    def test_a_server_error_is_retried(self, client: Lenz, slept: list[float]) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.post(f"/citechecks/{CHECK}/cancel")
            route.side_effect = [
                httpx.Response(500, json={"detail": "oops"}),
                httpx.Response(200, json=_load("cancel_citecheck_cancelled.json")),
            ]
            assert client.cancel_citecheck(CHECK).status == "cancelled"
        assert route.call_count == 2

    def test_an_answer_in_another_version_is_refused(self, client: Lenz) -> None:
        with respx.mock(base_url=BASE) as r:
            r.post(f"/citechecks/{CHECK}/cancel").respond(
                200, json=_load("cancel_citecheck_cancelled.json"), headers={"X-Lenz-API-Version": "2026-05-13"}
            )
            with pytest.raises(LenzApiVersionError):
                client.cancel_citecheck(CHECK)

    def test_it_needs_a_key(self, unauth_client: Lenz) -> None:
        with pytest.raises(LenzAuthError):
            unauth_client.cancel_citecheck(CHECK)


def test_the_cancel_result_is_lax_like_the_other_models() -> None:
    result = CancelResult.model_validate({})
    assert (result.task_id, result.cancelled, result.status) == ("", False, "")


@pytest.mark.parametrize("path", ["/verify/t1/cancel", "/reviews/r1/cancel", "/citechecks/c1/cancel"])
@pytest.mark.parametrize(
    ("status", "code"), [(401, "not_authenticated"), (404, "not_found"), (422, "validation_error")]
)
def test_a_cancel_error_keeps_the_servers_code(path: str, status: int, code: str) -> None:
    from lenz_io.errors import map_response_to_error

    err = map_response_to_error(status, json.dumps({"detail": "x", "code": code}).encode(), {}, endpoint=("POST", path))
    assert err.code == code


def test_another_endpoints_404_keeps_the_empty_code_2x_had() -> None:
    from lenz_io.errors import map_response_to_error

    err = map_response_to_error(
        404, json.dumps({"detail": "x", "code": "not_found"}).encode(), {}, endpoint=("GET", "/verify/status/t1")
    )
    assert err.code == ""


# ── an answer that is not the cancel's ──


@pytest.mark.parametrize(
    "body",
    [
        {},
        [],
        "ok",
        {"task_id": TASK},
        {"cancelled": True, "status": "cancelled"},
        {"task_id": "", "cancelled": True, "status": "cancelled"},
        {"task_id": "someone-else", "cancelled": True, "status": "cancelled"},
        {"task_id": TASK, "cancelled": "yes", "status": "cancelled"},
        {"task_id": TASK, "cancelled": True},
    ],
)
def test_a_cancel_answer_that_is_not_a_cancel_result_is_an_error(client: Lenz, body: Any) -> None:
    from lenz_io import LenzAPIError

    with respx.mock(base_url=BASE) as r:
        r.post(f"/verify/{TASK}/cancel").respond(200, json=body)
        with pytest.raises(LenzAPIError) as ei:
            client.cancel(TASK)
    assert "unexpected" in ei.value.message.lower()


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"review_id": REVIEW},
        {"status": "cancelled"},
        [],
        {"review_id": "other", "status": "cancelled", "issues": [], "failures": [], "claims": []},
    ],
)
def test_a_review_cancel_answer_that_is_not_the_review_is_an_error(client: Lenz, body: Any) -> None:
    from lenz_io import LenzAPIError

    with respx.mock(base_url=BASE) as r:
        r.post(f"/reviews/{REVIEW}/cancel").respond(200, json=body)
        with pytest.raises(LenzAPIError):
            client.cancel_review(REVIEW)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"citecheck_id": CHECK},
        {"status": "cancelled"},
        [],
        {
            "citecheck_id": "other",
            "status": "cancelled",
            "citations": [],
            "citation_issues": [],
            "citation_failures": [],
        },
    ],
)
def test_a_citecheck_cancel_answer_that_is_not_the_check_is_an_error(client: Lenz, body: Any) -> None:
    from lenz_io import LenzAPIError

    with respx.mock(base_url=BASE) as r:
        r.post(f"/citechecks/{CHECK}/cancel").respond(200, json=body)
        with pytest.raises(LenzAPIError):
            client.cancel_citecheck(CHECK)
