"""A task cancelled elsewhere (the website's Stop button, another process) is
a terminal answer.

In API version 2026-10-11 ``cancelled`` is a status of its own. In 2026-05-13
the same thing read ``failed`` with ``failure_class: "cancelled"``, and 2.x
raised the failed error for it. A 3.x wait must end the same way, at once,
instead of polling to its timeout. The recorded bodies are the API's own
(``tests/fixtures/parity/``)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from parity_observe import load

from lenz_io import (
    CitecheckEvent,
    CitecheckFailed,
    Lenz,
    LenzPipelineError,
    LenzTimeoutError,
    ReviewEvent,
    ReviewFailed,
    TaskStatus,
    VerificationCancelled,
    WebhookEvent,
    parse_webhook,
)
from lenz_io.models import Citecheck, ReviewFull

BASE = "https://lenz.io/api/v1"
_RUNNING = {"status": "processing", "task_id": "t", "progress": {"step": "research"}}
_DONE = {"status": "completed", "task_id": "u", "result": {"verification_id": "v1", "claim": "A."}}


def _body(name: str, shape: str = "canonical") -> dict[str, Any]:
    return load(shape, name)["body"]


@pytest.fixture()
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    calls: list[float] = []
    monkeypatch.setattr("lenz_io.client.time.sleep", lambda s: calls.append(s))
    return calls


# ── /verify/status ──


@pytest.mark.parametrize("name", ["verify__status_cancelled_live.json", "verify__status_cancelled_durable.json"])
class TestAVerificationCancelledElsewhere:
    def test_get_status_returns_it_and_does_not_raise(self, client: Lenz, name: str) -> None:
        body = _body(name)
        with respx.mock(base_url=BASE) as r:
            r.get(f"/verify/status/{body['task_id']}").respond(200, json=body)
            status = client.get_status(body["task_id"])
        assert isinstance(status, TaskStatus)
        assert status.status == "cancelled"
        assert status.task_id == body["task_id"]
        assert status.result is None and status.failure is None

    def test_wait_raises_the_failed_error_at_once(self, client: Lenz, slept: list[float], name: str) -> None:
        body = _body(name)
        with respx.mock(base_url=BASE) as r:
            poll = r.get(f"/verify/status/{body['task_id']}")
            poll.side_effect = [
                httpx.Response(200, json=_RUNNING | {"task_id": body["task_id"]}),
                httpx.Response(200, json=body),
            ]
            with pytest.raises(LenzPipelineError) as ei:
                client.wait(body["task_id"], timeout=300)
        err = ei.value
        assert not isinstance(err, LenzTimeoutError)
        assert poll.call_count == 2
        assert (err.failure_class, err.retryable, err.failure_reason) == ("cancelled", False, "cancelled")
        assert err.task_id == body["task_id"]
        assert "cancelled" in err.message.lower()

    def test_verify_and_wait_raises_it(self, client: Lenz, slept: list[float], name: str) -> None:
        body = _body(name)
        with respx.mock(base_url=BASE) as r:
            r.post("/verify").respond(200, json={"task_id": body["task_id"]})
            poll = r.get(f"/verify/status/{body['task_id']}").respond(200, json=body)
            with pytest.raises(LenzPipelineError) as ei:
                client.verify_and_wait("A.", timeout=300)
        assert poll.call_count == 1
        assert ei.value.failure_class == "cancelled" and ei.value.retryable is False

    def test_it_is_the_error_2x_raised_for_the_original_shape(
        self, client: Lenz, slept: list[float], name: str
    ) -> None:
        new, old = _body(name), _body(name, "legacy")
        raised = []
        for body in (new, old):
            with respx.mock(base_url=BASE) as r:
                r.get(f"/verify/status/{body['task_id']}").respond(200, json=body)
                with pytest.raises(LenzPipelineError) as ei:
                    client.wait(body["task_id"], timeout=300)
            raised.append(ei.value)
        a, b = raised
        for attr in ("failure_class", "retryable", "failure_reason", "task_id", "doc_url", "hint"):
            assert getattr(a, attr) == getattr(b, attr), attr


def test_a_batch_item_cancelled_elsewhere_is_a_failed_item(client: Lenz, slept: list[float]) -> None:
    gone = _body("verify__status_cancelled_live.json")
    with respx.mock(base_url=BASE) as r:
        r.post("/verify/batch").respond(
            200,
            json={
                "batch_id": "b",
                "items": [{"task_id": gone["task_id"], "claim": "A."}, {"task_id": "u", "claim": "B."}],
            },
        )
        cancelled = r.get(f"/verify/status/{gone['task_id']}").respond(200, json=gone)
        done = r.get("/verify/status/u")
        done.side_effect = [httpx.Response(200, json=_RUNNING | {"task_id": "u"}), httpx.Response(200, json=_DONE)]
        results = client.verify_batch_and_wait(claims=[{"claim": "A."}, {"claim": "B."}], timeout=300)
    assert [x.status for x in results] == ["failed", "completed"]
    assert results[0].verification is None
    assert results[0].status_detail is not None and results[0].status_detail.status == "cancelled"
    assert cancelled.call_count == 1, "a cancelled item is not polled again"
    assert done.call_count == 2


def test_the_batch_waits_for_nothing_but_the_running_ones(client: Lenz, slept: list[float]) -> None:
    """A batch whose only item is cancelled returns without sleeping to a timeout."""
    gone = _body("verify__status_cancelled_durable.json")
    with respx.mock(base_url=BASE) as r:
        r.post("/verify/batch").respond(
            200, json={"batch_id": "b", "items": [{"task_id": gone["task_id"], "claim": "A."}]}
        )
        r.get(f"/verify/status/{gone['task_id']}").respond(200, json=gone)
        results = client.verify_batch_and_wait(claims=[{"claim": "A."}], timeout=300)
    assert [x.status for x in results] == ["failed"]
    assert slept == []


# ── /review ──


class TestAReviewCancelledElsewhere:
    def test_get_review_returns_it_and_does_not_raise(self, client: Lenz) -> None:
        body = _body("review__get_cancelled.json")
        with respx.mock(base_url=BASE) as r:
            r.get(f"/reviews/{body['review_id']}").respond(200, json=body)
            review = client.get_review(body["review_id"], view="full")
        assert isinstance(review, ReviewFull)
        assert review.status == "cancelled" and review.outcome == "incomplete"
        assert review.failure is None

    def test_review_and_wait_raises_the_failed_error_at_once(self, client: Lenz, slept: list[float]) -> None:
        body = _body("review__get_cancelled.json")
        rid = body["review_id"]
        running = {**body, "status": "verifying", "outcome": None, "completed_at": None, "poll_after_seconds": 5}
        with respx.mock(base_url=BASE) as r:
            r.post("/review").respond(202, json={"review_id": rid, "status": "queued"})
            poll = r.get(f"/reviews/{rid}")
            poll.side_effect = [httpx.Response(200, json=running), httpx.Response(200, json=body)]
            with pytest.raises(ReviewFailed) as ei:
                client.review_and_wait("draft", timeout=300)
        err = ei.value
        assert poll.call_count == 2
        assert (err.failure_class, err.retryable, err.failure_reason, err.error_code) == (
            "cancelled",
            False,
            "cancelled",
            "cancelled",
        )
        assert err.review_id == rid and err.review is not None and err.review.status == "cancelled"
        assert "cancelled" in err.message.lower()

    def test_it_is_the_error_2x_raised_for_the_original_shape(self, client: Lenz, slept: list[float]) -> None:
        raised = []
        for shape in ("canonical", "legacy"):
            body = _body("review__get_cancelled.json", shape)
            with respx.mock(base_url=BASE) as r:
                r.get(f"/reviews/{body['review_id']}").respond(200, json=body)
                with pytest.raises(ReviewFailed) as ei:
                    client._wait_review(body["review_id"], timeout=300)
            raised.append(ei.value)
        a, b = raised
        for attr in (
            "failure_class",
            "retryable",
            "failure_reason",
            "error_code",
            "doc_url",
            "hint",
            "message",
            "cause",
            "fix",
        ):
            assert getattr(a, attr) == getattr(b, attr), attr


# ── /citecheck ──


class TestACitecheckCancelledElsewhere:
    def test_get_citecheck_returns_it_and_does_not_raise(self, client: Lenz) -> None:
        body = _body("citecheck__get_cancelled.json")
        with respx.mock(base_url=BASE) as r:
            r.get(f"/citechecks/{body['citecheck_id']}").respond(200, json=body)
            check = client.get_citecheck(body["citecheck_id"])
        assert isinstance(check, Citecheck)
        assert check.status == "cancelled" and check.failure is None

    def test_citecheck_and_wait_raises_the_failed_error_at_once(self, client: Lenz, slept: list[float]) -> None:
        body = _body("citecheck__get_cancelled.json")
        cid = body["citecheck_id"]
        running = {**body, "status": "checking", "outcome": None, "completed_at": None, "poll_after_seconds": 5}
        with respx.mock(base_url=BASE) as r:
            r.post("/citecheck").respond(202, json={"citecheck_id": cid, "status": "queued"})
            poll = r.get(f"/citechecks/{cid}")
            poll.side_effect = [httpx.Response(200, json=running), httpx.Response(200, json=body)]
            with pytest.raises(CitecheckFailed) as ei:
                client.citecheck_and_wait(text="See https://example.gov/report-2024.", timeout=300)
        err = ei.value
        assert poll.call_count == 2
        assert (err.failure_class, err.retryable, err.failure_reason, err.error_code) == (
            "cancelled",
            False,
            "cancelled",
            "cancelled",
        )
        assert err.citecheck_id == cid and err.citecheck is not None and err.citecheck.status == "cancelled"
        assert "cancelled" in err.message.lower()

    def test_it_is_the_error_2x_raised_for_the_original_shape(self, client: Lenz, slept: list[float]) -> None:
        raised = []
        for shape in ("canonical", "legacy"):
            body = _body("citecheck__get_cancelled.json", shape)
            with respx.mock(base_url=BASE) as r:
                r.get(f"/citechecks/{body['citecheck_id']}").respond(200, json=body)
                with pytest.raises(CitecheckFailed) as ei:
                    client._wait_citecheck(body["citecheck_id"], timeout=300)
            raised.append(ei.value)
        a, b = raised
        for attr in (
            "failure_class",
            "retryable",
            "failure_reason",
            "error_code",
            "doc_url",
            "hint",
            "message",
            "cause",
            "fix",
        ):
            assert getattr(a, attr) == getattr(b, attr), attr


# ── webhooks ──


class TestCancelledWebhooks:
    def test_verification_cancelled(self) -> None:
        raw = _body("webhook__verification_cancelled.json")
        event = parse_webhook(json.dumps(raw))
        assert isinstance(event, VerificationCancelled)
        assert (event.event, event.status, event.task_id) == ("verification.cancelled", "cancelled", raw["task_id"])
        assert event.event_id == raw["event_id"] and event.attempt == 1
        assert event.delivered_at == raw["delivered_at"]
        assert isinstance(event.verification, TaskStatus)
        assert event.verification.status == "cancelled" and event.verification.task_id == raw["task_id"]

    def test_verification_cancelled_is_not_a_failed_event(self) -> None:
        from lenz_io import VerificationFailed

        assert not issubclass(VerificationCancelled, VerificationFailed)
        assert issubclass(VerificationCancelled, WebhookEvent)

    def test_a_cancelled_envelope_on_another_event_is_not_read_as_that_kind(self) -> None:
        raw = _body("webhook__verification_cancelled.json") | {"event": "verification.failed"}
        event = parse_webhook(raw)
        assert event.verification is None  # a failed event carrying a cancelled run

    def test_review_cancelled(self) -> None:
        raw = _body("webhook__review_cancelled.json")
        event = parse_webhook(raw)
        assert isinstance(event, ReviewEvent)
        assert (event.event, event.status, event.review_id, event.event_id) == (
            "review.cancelled",
            "cancelled",
            raw["review_id"],
            raw["event_id"],
        )
        assert event.task_id == raw["review_id"]
        assert event.review is not None and event.review.status == "cancelled"

    def test_citecheck_cancelled(self) -> None:
        raw = _body("webhook__citecheck_cancelled.json")
        event = parse_webhook(raw)
        assert isinstance(event, CitecheckEvent)
        assert (event.event, event.status, event.citecheck_id, event.event_id) == (
            "citecheck.cancelled",
            "cancelled",
            raw["citecheck_id"],
            raw["event_id"],
        )
        assert event.task_id == raw["citecheck_id"]
        assert event.citecheck is not None and event.citecheck.status == "cancelled"

    @pytest.mark.parametrize(
        ("name", "cls"),
        [
            ("webhook__verification_cancelled.json", "VerificationFailed"),
            ("webhook__review_cancelled.json", "ReviewEvent"),
            ("webhook__citecheck_cancelled.json", "CitecheckEvent"),
        ],
    )
    def test_a_cancellation_of_an_original_shape_submission_keeps_arriving_as_failed(self, name: str, cls: str) -> None:
        raw = _body(name, "legacy")
        event = parse_webhook(raw)
        assert type(event).__name__ == cls
        assert event.event.endswith(".failed") and event.status == "failed"
        if cls == "VerificationFailed":
            assert (event.error, event.failure_class, event.retryable) == ("cancelled", "cancelled", False)

    def test_an_unknown_event_still_parses_generically(self) -> None:
        event = parse_webhook({"event": "verification.paused", "task_id": "t", "status": "paused"})
        assert type(event) is WebhookEvent
        assert event.event == "verification.paused"


# ── the command line ──


class TestTheCli:
    def _run(self, monkeypatch: pytest.MonkeyPatch, args: list[str], routes: dict[str, dict[str, Any]]):
        from typer.testing import CliRunner

        from lenz_io.cli.app import app

        monkeypatch.setenv("LENZ_API_KEY", "lenz_" + "0" * 32)
        with respx.mock(base_url=BASE) as r:
            for path, body in routes.items():
                r.get(path).respond(200, json=body)
            return CliRunner().invoke(app, args)

    def test_verify_resume_ends_like_a_failed_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        body = _body("verify__status_cancelled_durable.json")
        result = self._run(
            monkeypatch, ["--json", "verify", "--resume", body["task_id"]], {f"/verify/status/{body['task_id']}": body}
        )
        assert result.exit_code == 1
        assert json.loads(result.stdout)["error"]["code"] == "pipeline_failed"
        assert "Unexpected status" not in result.output

    def test_status_says_cancelled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        body = _body("verify__status_cancelled_durable.json")
        result = self._run(monkeypatch, ["status", body["task_id"]], {f"/verify/status/{body['task_id']}": body})
        assert result.exit_code == 0 and "cancelled" in result.output

    def test_review_resume_says_cancelled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        body = _body("review__get_cancelled.json")
        result = self._run(
            monkeypatch, ["review", "--resume", body["review_id"]], {f"/reviews/{body['review_id']}": body}
        )
        # off a terminal the CLI prints JSON, which carries the status
        assert json.loads(result.stdout)["status"] == "cancelled"
        assert result.exit_code == 2  # incomplete: the same exit as any review that did not finish

    def test_citecheck_resume_says_cancelled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        body = _body("citecheck__get_cancelled.json")
        result = self._run(
            monkeypatch, ["citecheck", "--resume", body["citecheck_id"]], {f"/citechecks/{body['citecheck_id']}": body}
        )
        assert json.loads(result.stdout)["status"] == "cancelled"
        assert result.exit_code == 2


def test_the_original_shape_still_reads_as_a_failure(client: Lenz) -> None:
    """A 2.x-shaped answer is not this release's concern, but the status it
    carries stays what the server said."""
    body = _body("verify__status_cancelled_durable.json", "legacy")
    status = TaskStatus.model_validate(body)
    assert status.status == "failed" and status.failure_class == "cancelled"
