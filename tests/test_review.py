"""``POST /review`` and ``GET /reviews/{review_id}``: the submit call, the two
typed views, the wait helper, the webhook event and the error codes.

Every body here is a recorded server response (``tests/fixtures/contract/
review_*.json``, shared with the Node SDK), so the models are exercised
against what the API actually sends in each state, not against a hand-made
stand-in.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx

from lenz_io import (
    Lenz,
    LenzError,
    LenzGoneError,
    LenzPipelineError,
    LenzRateLimitError,
    LenzTimeoutError,
    LenzUpstreamUnavailableError,
    LenzValidationError,
    LenzWebhooks,
    ReviewEvent,
    ReviewFailed,
    ReviewFull,
    ReviewIssues,
    ReviewStarted,
    ReviewTimeout,
    VerificationCompleted,
    parse_webhook,
)

BASE = "https://lenz.io/api/v1"
FIXTURES = Path(__file__).parent / "fixtures" / "contract"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture()
def no_sleep(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr("lenz_io.client.time.sleep", lambda s: slept.append(s))
    return slept


# ── submit ──────────────────────────────────────────────────────────────────


class TestSubmit:
    def test_returns_review_started_and_posts_the_text(self, client):
        with respx.mock(base_url=BASE) as r:
            route = r.post("/review").respond(202, json=_load("review_accepted.json"))
            started = client.review("The EU AI Act entered into force on 1 August 2024.")
        assert isinstance(started, ReviewStarted)
        assert started.review_id == "442b6aa9"
        assert started.status == "queued"
        body = json.loads(route.calls.last.request.content)
        assert body == {"text": "The EU AI Act entered into force on 1 August 2024.", "visibility": "private"}

    def test_flat_policy_kwargs_become_the_wire_escalate_object(self, client):
        with respx.mock(base_url=BASE) as r:
            route = r.post("/review").respond(202, json=_load("review_accepted.json"))
            client.review(
                "draft",
                verdicts=["False"],
                confidence=["low", "medium"],
                max_assessments=5,
                max_verifications=0,
                depth="low",
            )
        body = json.loads(route.calls.last.request.content)
        assert body["escalate"] == {
            "verdicts": ["False"],
            "confidence": ["low", "medium"],
            "max_assessments": 5,
            "max_verifications": 0,
            "depth": "low",
        }
        # Nothing of the policy leaks to the top level, where the server
        # rejects an unknown field with a 422 rather than ignoring it.
        assert "max_verifications" not in body

    def test_empty_selector_lists_are_sent_not_dropped(self, client):
        # `[]` means "no label rule", a legal policy; dropping it would bring
        # back the server default and deep-check rows the caller excluded.
        with respx.mock(base_url=BASE) as r:
            route = r.post("/review").respond(202, json=_load("review_accepted.json"))
            client.review("draft", verdicts=[], confidence=[])
        assert json.loads(route.calls.last.request.content)["escalate"] == {"verdicts": [], "confidence": []}

    @pytest.mark.parametrize(
        ("webhook_url", "expected"),
        [(None, "absent"), ("", ""), ("https://example.com/hook", "https://example.com/hook")],
    )
    def test_webhook_url_has_three_states(self, client, webhook_url, expected):
        with respx.mock(base_url=BASE) as r:
            route = r.post("/review").respond(202, json=_load("review_accepted.json"))
            client.review("draft", webhook_url=webhook_url)
        body = json.loads(route.calls.last.request.content)
        if expected == "absent":
            assert "webhook_url" not in body
        else:
            assert body["webhook_url"] == expected

    def test_language_and_visibility(self, client):
        with respx.mock(base_url=BASE) as r:
            route = r.post("/review").respond(202, json=_load("review_accepted.json"))
            client.review("draft", language="de", visibility="unlisted")
        body = json.loads(route.calls.last.request.content)
        assert body["language"] == "de"
        assert body["visibility"] == "unlisted"

    def test_sends_an_idempotency_key_so_a_retry_cannot_start_a_second_review(self, client):
        with respx.mock(base_url=BASE) as r:
            route = r.post("/review").respond(202, json=_load("review_accepted.json"))
            client.review("draft")
            client.review("draft", idempotency_key="my-key")
        first, second = route.calls[0].request, route.calls[1].request
        assert len(first.headers["Idempotency-Key"]) == 32
        assert second.headers["Idempotency-Key"] == "my-key"

    def test_retried_submit_reuses_its_key(self, client, no_sleep):
        with respx.mock(base_url=BASE) as r:
            route = r.post("/review").mock(
                side_effect=[httpx.Response(502), httpx.Response(202, json=_load("review_accepted.json"))]
            )
            client.review("draft")
        keys = {c.request.headers["Idempotency-Key"] for c in route.calls}
        assert len(route.calls) == 2 and len(keys) == 1

    def test_selectors_accept_any_iterable_but_not_a_bare_string(self, client):
        with respx.mock(base_url=BASE) as r:
            route = r.post("/review").respond(202, json=_load("review_accepted.json"))
            client.review("draft", verdicts=("False",), confidence={"low"})
        assert json.loads(route.calls.last.request.content)["escalate"] == {
            "verdicts": ["False"],
            "confidence": ["low"],
        }
        with pytest.raises(ValueError):
            client.review("draft", verdicts="False")  # type: ignore[arg-type]

    def test_empty_visibility_is_omitted(self, client):
        with respx.mock(base_url=BASE) as r:
            route = r.post("/review").respond(202, json=_load("review_accepted.json"))
            client.review("draft", visibility="")
        assert "visibility" not in json.loads(route.calls.last.request.content)

    def test_a_conflict_naming_the_review_returns_it(self, client, no_sleep):
        # The first attempt's socket dropped after the server took it; the
        # retry, under the same key, finds that review still being created.
        with respx.mock(base_url=BASE) as r:
            route = r.post("/review").mock(
                side_effect=[
                    httpx.ReadError("dropped"),
                    httpx.Response(
                        409,
                        json={"detail": "still being created", "code": "idempotency_conflict", "review_id": "442b6aa9"},
                    ),
                ]
            )
            started = client.review("draft")
        assert started.review_id == "442b6aa9"
        assert len({c.request.headers["Idempotency-Key"] for c in route.calls}) == 1

    def test_a_conflict_without_a_review_id_raises(self, client):
        with respx.mock(base_url=BASE) as r:
            r.post("/review").respond(
                409, json={"detail": "still being created", "code": "idempotency_conflict", "review_id": None}
            )
            with pytest.raises(LenzError) as exc:
                client.review("draft")
        assert exc.value.status_code == 409

    def test_empty_text_is_refused_before_the_network(self, client):
        with pytest.raises(ValueError):
            client.review("   ")


# ── errors ──────────────────────────────────────────────────────────────────


class TestErrors:
    def test_review_in_flight_raises_at_once_with_retry_after(self, client, no_sleep):
        # A 60 s wait is within the ladder's sleep cap, but a review runs for
        # minutes: sleeping it three times over inside a submit is a hidden
        # three-minute block that most likely ends in the same 429.
        with respx.mock(base_url=BASE) as r:
            route = r.post("/review").respond(
                429, json=_load("error_review_in_flight_429.json"), headers={"Retry-After": "60"}
            )
            with pytest.raises(LenzRateLimitError) as exc:
                client.review("draft")
        assert route.call_count == 1
        assert no_sleep == []
        assert exc.value.code == "review_in_flight"
        assert exc.value.retry_after == 60

    def test_review_in_flight_retry_after_from_the_body(self, client, no_sleep):
        with respx.mock(base_url=BASE) as r:
            r.post("/review").respond(429, json=_load("error_review_in_flight_429.json"))
            with pytest.raises(LenzRateLimitError) as exc:
                client.review("draft")
        assert exc.value.retry_after == 60

    @pytest.mark.parametrize("code", ["invalid_verdict_label", "invalid_confidence_band", "webhook_secret_missing"])
    def test_validation_codes_surface(self, client, code):
        body = _load("error_review_invalid_verdict_label_422.json")
        body["code"] = code
        with respx.mock(base_url=BASE) as r:
            r.post("/review").respond(422, json=body)
            with pytest.raises(LenzValidationError) as exc:
                client.review("draft")
        assert exc.value.code == code
        assert exc.value.errors[0]["loc"][-1] == "verdicts"

    @pytest.mark.parametrize("code", ["capacity", "upstream_unavailable"])
    def test_503_codes_raise_upstream_unavailable(self, client, code, no_sleep):
        with respx.mock(base_url=BASE) as r:
            r.post("/review").respond(503, json={"detail": "busy", "code": code, "retry_after": 90})
            with pytest.raises(LenzUpstreamUnavailableError) as exc:
                client.review("draft")
        assert exc.value.retry_after == 90

    def test_a_body_only_503_wait_in_retry_after_seconds_is_honoured(self, client, no_sleep):
        with respx.mock(base_url=BASE) as r:
            route = r.post("/review").respond(
                503, json={"detail": "busy", "code": "capacity", "retry_after_seconds": 120}
            )
            with pytest.raises(LenzUpstreamUnavailableError) as exc:
                client.review("draft")
        # 120 s is past the ladder's sleep cap: raised at once, carrying the wait
        assert route.call_count == 1
        assert exc.value.retry_after == 120

    def test_extract_daily_limit_on_a_url_review(self, client, no_sleep):
        with respx.mock(base_url=BASE) as r:
            r.post("/review").respond(
                429,
                json={
                    "detail": "Daily /extract limit reached.",
                    "code": "extract_daily_limit",
                    "reset_in_seconds": 7200,
                },
            )
            with pytest.raises(LenzRateLimitError) as exc:
                client.review("https://example.com/post")
        assert exc.value.code == "extract_daily_limit"
        assert exc.value.retry_after == 7200

    def test_purged_review_is_gone(self, client):
        with respx.mock(base_url=BASE) as r:
            r.get("/reviews/442b6aa9").respond(
                410, json={"detail": "Review removed.", "code": "purged", "purged_at": "2026-10-25T00:00:00Z"}
            )
            with pytest.raises(LenzGoneError) as exc:
                client.get_review("442b6aa9")
        assert exc.value.purged_at == "2026-10-25T00:00:00Z"


# ── read ────────────────────────────────────────────────────────────────────


class TestGetReview:
    def test_full_view_is_the_default(self, client):
        with respx.mock(base_url=BASE) as r:
            route = r.get("/reviews/442b6aa9").respond(200, json=_load("review_completed.json"))
            review = client.get_review("442b6aa9")
        assert "view" not in route.calls.last.request.url.params
        assert isinstance(review, ReviewFull)
        assert review.view == "full"
        assert review.outcome == "issues_found"
        assert review.credits.charged == 14
        assert [c.index for c in review.claims] == [0, 1, 2, 3]

    def test_issues_view(self, client):
        with respx.mock(base_url=BASE) as r:
            route = r.get("/reviews/442b6aa9").respond(200, json=_load("review_completed_issues.json"))
            review = client.get_review("442b6aa9", view="issues")
        assert route.calls.last.request.url.params["view"] == "issues"
        assert isinstance(review, ReviewIssues)
        assert not hasattr(review, "claims")
        assert review.issues[0].verdict == "False"

    def test_unknown_view_is_refused_locally(self, client):
        with pytest.raises(ValueError):
            client.get_review("442b6aa9", view="bogus")  # type: ignore[call-overload]


class TestModels:
    def test_completed_issue_fields(self):
        review = ReviewFull.model_validate(_load("review_completed.json"))
        # ordered by final verdict, then confidence band
        issue, other = review.issues
        assert issue.claim_index == 0
        assert issue.claim == "The EU AI Act entered into force in March 2024."
        assert issue.verified_claim is None
        assert (issue.verdict, issue.confidence, issue.source) == ("False", "high", "verification")
        assert issue.verification_id == "c9b769e1"
        assert issue.verification_status == "completed"
        assert issue.url.startswith("https://lenz.io/c/")
        assert issue.escalation.matched_rules == ["verdict", "confidence"]
        assert issue.escalation.disposition == "planned"
        assert issue.key_finding and issue.rationale
        assert issue.suggested_rewrite == "The EU AI Act entered into force on 1 August 2024."
        assert issue.failure is None
        # a deep check that found the claim false but established no correction
        assert (other.claim_index, other.confidence, other.suggested_rewrite) == (3, "medium", None)
        assert other.claim.startswith("About 40% of European companies")

    def test_claim_rows_carry_result_assessment_and_deep_check(self):
        review = ReviewFull.model_validate(_load("review_completed.json"))
        quick, deep = review.claims[1], review.claims[3]
        assert quick.result.source == "assessment" and quick.result.is_issue is False
        assert quick.escalation.disposition == "not_selected"
        assert quick.verification is None
        # the deep check overrode the quick verdict
        assert deep.assessment.verdict == "Mostly False"
        assert deep.result.verdict == "False" and deep.result.source == "verification"
        v = deep.verification
        assert (v.status, v.content_status, v.depth, v.visibility) == ("completed", "available", "low", "private")
        assert v.lenz_score == 2 and isinstance(v.lenz_score, int)
        assert v.entities[0].name == "EU AI Act"
        assert v.warnings

    def test_every_in_flight_state_parses(self):
        queued = ReviewFull.model_validate(_load("review_queued.json"))
        assert queued.status == "queued" and queued.summary.claims_selected is None
        assert queued.summary.assessments is None and queued.claims == [] and queued.issues == []
        assert queued.outcome is None and queued.poll_after_seconds == 10

        assessing = ReviewFull.model_validate(_load("review_assessing.json"))
        assert [c.assessment.status for c in assessing.claims] == ["completed", "completed", "running", "running"]
        assert assessing.claims[3].result is None and assessing.claims[3].escalation is None

        verifying = ReviewFull.model_validate(_load("review_verifying.json"))
        assert verifying.claims[3].verification.status == "processing"
        assert verifying.claims[3].verification.verdict is None
        assert verifying.summary.verifications.planned == 1

    def test_failed_states(self):
        no_claim = ReviewFull.model_validate(_load("review_failed_no_claim.json"))
        assert no_claim.outcome == "unchecked"
        assert no_claim.failure.failure_reason == "no_claim"
        assert no_claim.failure.retryable is False
        assert no_claim.failure.hint

        credits = ReviewFull.model_validate(_load("review_failed_insufficient_credits.json"))
        assert credits.failure.failure_reason == "insufficient_credits"
        assert credits.summary.claims_selected == 4

    def test_incomplete_review_with_a_capped_quick_issue_and_a_failed_row(self):
        review = ReviewFull.model_validate(_load("review_incomplete.json"))
        assert review.outcome == "incomplete"
        assert [i.claim_index for i in review.issues] == [0, 3, 2]
        quick = review.issues[2]
        assert quick.source == "assessment"
        assert quick.verification_id is None and quick.suggested_rewrite is None
        assert quick.escalation.disposition == "cap"
        (failed,) = review.failures
        assert failed.stage == "assessment"
        assert failed.failure.failure_class == "upstream_unavailable" and failed.failure.retryable is True
        assert review.claims[1].assessment.error_code == "timeout"

    def test_the_quickstart_recipe_reads(self):
        # The quickstart's Python tab, verbatim apart from the client call.
        review = ReviewFull.model_validate(_load("review_incomplete.json"))
        lines = []
        for i in review.issues:
            lines.append((i.verdict, i.confidence, i.claim))
            if i.suggested_rewrite:
                lines.append(("  Suggested rewrite:", i.suggested_rewrite))
        capped = [{"claim": c.claim} for c in review.claims if c.escalation and c.escalation.disposition == "cap"]
        deep = next((i for i in review.issues if i.verification_id), None)
        assert len(lines) == 4
        assert capped == [{"claim": review.claims[2].claim}]
        assert deep is not None and deep.verification_id == "c9b769e1"

    def test_an_entity_with_no_name_parses(self):
        body = _load("review_completed.json")
        body["claims"][3]["verification"]["entities"].append({"name": None, "qid": None})
        review = ReviewFull.model_validate(body)
        assert review.claims[3].verification.entities[-1].name is None

    def test_unknown_values_pass_through(self):
        # Every enum-shaped field is a plain string: a disposition, status or
        # error code the API adds later must not break a released SDK.
        body = _load("review_completed.json")
        body["status"] = "archived"
        body["claims"][0]["escalation"]["disposition"] = "account_cap_v2"
        body["claims"][0]["assessment"]["error_code"] = "new_code"
        body["new_top_level_field"] = 1
        review = ReviewFull.model_validate(body)
        assert review.status == "archived"
        assert review.claims[0].escalation.disposition == "account_cap_v2"


# ── wait ────────────────────────────────────────────────────────────────────


def _sequence_raw(*bodies: dict) -> list[httpx.Response]:
    return [httpx.Response(200, json=b) for b in bodies]


def _sequence(*names: str) -> list[httpx.Response]:
    return [httpx.Response(200, json=_load(n)) for n in names]


class TestReviewAndWait:
    def test_polls_to_completed_and_fires_on_update_only_on_change(self, client, no_sleep):
        updates: list[str] = []
        with respx.mock(base_url=BASE) as r:
            r.post("/review").respond(202, json=_load("review_accepted.json"))
            r.get("/reviews/442b6aa9").mock(
                side_effect=_sequence(
                    "review_queued.json",
                    "review_queued.json",
                    "review_assessing.json",
                    "review_verifying.json",
                    "review_verifying.json",
                    "review_completed.json",
                )
            )
            review = client.review_and_wait("draft", on_update=lambda rv: updates.append(rv.status))
        assert isinstance(review, ReviewFull)
        assert review.outcome == "issues_found"
        assert updates == ["queued", "assessing", "verifying", "completed"]
        # poll_after_seconds: 10 while queued/assessing, 15 while verifying
        assert no_sleep == [10, 10, 10, 15, 15]

    def test_forwards_submit_kwargs(self, client, no_sleep):
        with respx.mock(base_url=BASE) as r:
            post = r.post("/review").respond(202, json=_load("review_accepted.json"))
            r.get("/reviews/442b6aa9").respond(200, json=_load("review_completed.json"))
            client.review_and_wait(text="draft", max_verifications=2, depth="low")
        assert json.loads(post.calls.last.request.content)["escalate"] == {"max_verifications": 2, "depth": "low"}

    def test_poll_interval_has_a_five_second_floor(self, client, no_sleep):
        fast = _load("review_assessing.json")
        fast["poll_after_seconds"] = 0
        with respx.mock(base_url=BASE) as r:
            r.post("/review").respond(202, json=_load("review_accepted.json"))
            r.get("/reviews/442b6aa9").mock(
                side_effect=[httpx.Response(200, json=fast), httpx.Response(200, json=_load("review_completed.json"))]
            )
            client.review_and_wait("draft")
        assert no_sleep == [5]

    def test_a_missing_poll_hint_uses_a_default(self, client, no_sleep):
        body = _load("review_assessing.json")
        body["poll_after_seconds"] = None
        with respx.mock(base_url=BASE) as r:
            r.post("/review").respond(202, json=_load("review_accepted.json"))
            r.get("/reviews/442b6aa9").mock(
                side_effect=[httpx.Response(200, json=body), httpx.Response(200, json=_load("review_completed.json"))]
            )
            client.review_and_wait("draft")
        assert no_sleep == [10]

    @pytest.mark.parametrize(
        ("fixture", "code", "retryable"),
        [
            ("review_failed_no_claim.json", "no_claim", False),
            ("review_failed_insufficient_credits.json", "insufficient_credits", False),
        ],
    )
    def test_failed_terminal_raises_review_failed(self, client, no_sleep, fixture, code, retryable):
        with respx.mock(base_url=BASE) as r:
            r.post("/review").respond(202, json=_load("review_accepted.json"))
            r.get("/reviews/442b6aa9").mock(side_effect=_sequence("review_queued.json", fixture))
            with pytest.raises(ReviewFailed) as exc:
                client.review_and_wait("draft")
        err = exc.value
        assert isinstance(err, LenzPipelineError)
        assert err.review_id == _load(fixture)["review_id"]
        assert err.error_code == code
        assert err.failure_reason == code
        assert err.retryable is retryable
        assert err.hint and err.hint == err.review.failure.hint
        assert isinstance(err.review, ReviewFull) and err.review.outcome == "unchecked"

    def test_timeout_raises_with_the_last_body(self, client, monkeypatch):
        clock = [0.0]
        monkeypatch.setattr("lenz_io.client.time.monotonic", lambda: clock[0])
        monkeypatch.setattr("lenz_io.client.time.sleep", lambda s: clock.__setitem__(0, clock[0] + s))
        with respx.mock(base_url=BASE) as r:
            r.post("/review").respond(202, json=_load("review_accepted.json"))
            get = r.get("/reviews/442b6aa9").respond(200, json=_load("review_verifying.json"))
            with pytest.raises(ReviewTimeout) as exc:
                client.review_and_wait("draft", timeout=40)
        err = exc.value
        assert isinstance(err, LenzTimeoutError)
        assert err.review_id == "442b6aa9"
        assert isinstance(err.partial, ReviewFull) and err.partial.status == "verifying"
        # never sleeps past the deadline, and polls once more at it
        assert clock[0] == 40
        assert get.call_count == 4

    def test_timeout_before_any_body_has_no_partial(self, client, monkeypatch):
        clock = [0.0]
        monkeypatch.setattr("lenz_io.client.time.monotonic", lambda: clock[0])
        monkeypatch.setattr("lenz_io.client.time.sleep", lambda s: clock.__setitem__(0, clock[0] + s))
        monkeypatch.setattr("lenz_io.client._retry_sleep", lambda attempt: 0)
        with respx.mock(base_url=BASE) as r:
            r.post("/review").respond(202, json=_load("review_accepted.json"))
            r.get("/reviews/442b6aa9").mock(side_effect=httpx.ConnectError("down"))
            with pytest.raises(ReviewTimeout) as exc:
                client.review_and_wait("draft", timeout=12)
        assert exc.value.partial is None

    def test_transient_poll_errors_are_retried(self, client, no_sleep):
        with respx.mock(base_url=BASE) as r:
            r.post("/review").respond(202, json=_load("review_accepted.json"))
            r.get("/reviews/442b6aa9").mock(
                side_effect=[httpx.Response(500)] * 4 + [httpx.Response(200, json=_load("review_completed.json"))]
            )
            review = client.review_and_wait("draft")
        assert review.status == "completed"

    def test_one_request_per_poll_bounded_by_the_deadline(self, client, no_sleep):
        with respx.mock(base_url=BASE) as r:
            r.post("/review").respond(202, json=_load("review_accepted.json"))
            get = r.get("/reviews/442b6aa9").mock(
                side_effect=[httpx.Response(502), httpx.Response(200, json=_load("review_completed.json"))]
            )
            client.review_and_wait("draft", timeout=20)
        # the 502 was not retried inside the poll: the next poll read it
        assert get.call_count == 2
        assert no_sleep == [10]
        read_timeout = get.calls[0].request.extensions["timeout"]["read"]
        assert read_timeout <= 20

    @pytest.mark.parametrize(("stated", "slept"), [("45", 45), ("3600", 60)])
    def test_a_proxy_503_while_polling_waits_what_it_states_capped(self, client, no_sleep, stated, slept):
        with respx.mock(base_url=BASE) as r:
            r.post("/review").respond(202, json=_load("review_accepted.json"))
            r.get("/reviews/442b6aa9").mock(
                side_effect=[
                    httpx.Response(503, text="Service Unavailable", headers={"Retry-After": stated}),
                    httpx.Response(200, json=_load("review_completed.json")),
                ]
            )
            client.review_and_wait("draft")
        assert no_sleep == [slept]

    def test_a_429_while_polling_waits_what_it_states(self, client, no_sleep):
        with respx.mock(base_url=BASE) as r:
            r.post("/review").respond(202, json=_load("review_accepted.json"))
            r.get("/reviews/442b6aa9").mock(
                side_effect=[
                    httpx.Response(429, json={"detail": "slow down"}, headers={"Retry-After": "40"}),
                    httpx.Response(200, json=_load("review_completed.json")),
                ]
            )
            assert client.review_and_wait("draft").status == "completed"
        assert no_sleep == [40]

    def test_an_unreadable_body_is_read_again(self, client, no_sleep):
        bad = _load("review_verifying.json")
        bad["credits"] = None
        with respx.mock(base_url=BASE) as r:
            r.post("/review").respond(202, json=_load("review_accepted.json"))
            r.get("/reviews/442b6aa9").mock(side_effect=_sequence_raw(bad, _load("review_completed.json")))
            assert client.review_and_wait("draft").status == "completed"

    def test_failed_review_without_a_failure_block(self, client, no_sleep):
        body = _load("review_failed_no_claim.json")
        body["failure"] = None
        with respx.mock(base_url=BASE) as r:
            r.post("/review").respond(202, json=_load("review_accepted.json"))
            r.get("/reviews/442b6aa9").respond(200, json=body)
            with pytest.raises(ReviewFailed) as exc:
                client.review_and_wait("draft")
        assert exc.value.error_code == ""
        assert exc.value.retryable is None

    def test_a_partial_failure_block_parses(self):
        body = _load("review_failed_no_claim.json")
        body["failure"] = {"failure_reason": None, "failure_class": None, "retryable": None, "docs_url": None}
        assert ReviewFull.model_validate(body).failure.failure_reason is None

    def test_non_transient_poll_errors_raise(self, client, no_sleep):
        with respx.mock(base_url=BASE) as r:
            r.post("/review").respond(202, json=_load("review_accepted.json"))
            r.get("/reviews/442b6aa9").respond(404, json={"detail": "Review not found.", "code": "not_found"})
            with pytest.raises(Exception) as exc:
                client.review_and_wait("draft")
        assert not isinstance(exc.value, ReviewTimeout)
        assert exc.value.status_code == 404

    def test_on_update_errors_do_not_stop_the_wait(self, client, no_sleep):
        def boom(_review):
            raise RuntimeError("caller bug")

        with respx.mock(base_url=BASE) as r:
            r.post("/review").respond(202, json=_load("review_accepted.json"))
            r.get("/reviews/442b6aa9").mock(side_effect=_sequence("review_queued.json", "review_completed.json"))
            assert client.review_and_wait("draft", on_update=boom).status == "completed"

    def test_on_update_gets_a_copy(self, client, no_sleep):
        seen: list[ReviewFull] = []

        def mutate(review):
            review.issues.clear()
            seen.append(review)

        with respx.mock(base_url=BASE) as r:
            r.post("/review").respond(202, json=_load("review_accepted.json"))
            r.get("/reviews/442b6aa9").respond(200, json=_load("review_completed.json"))
            review = client.review_and_wait("draft", on_update=mutate)
        assert len(review.issues) == 2 and seen[0].issues == []

    def test_silent_without_on_update(self, client, no_sleep, capsys):
        with respx.mock(base_url=BASE) as r:
            r.post("/review").respond(202, json=_load("review_accepted.json"))
            r.get("/reviews/442b6aa9").respond(200, json=_load("review_completed.json"))
            client.review_and_wait("draft")
        assert capsys.readouterr() == ("", "")


# ── webhooks ────────────────────────────────────────────────────────────────

SECRET = "whsec_test"


def _signed(payload: dict) -> tuple[bytes, dict[str, str]]:
    payload = dict(payload, delivered_at=datetime.now(timezone.utc).isoformat())
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {"X-Lenz-Signature": sig}


class TestWebhooks:
    def test_review_completed(self):
        raw, headers = _signed(_load("review_webhook_completed.json"))
        event = LenzWebhooks(secret=SECRET).parse(raw, headers)
        assert isinstance(event, ReviewEvent)
        assert event.event == "review.completed"
        assert event.event_id == "evt_a43e6ae7646f275d92f15223"
        assert event.review_id == "442b6aa9"
        assert event.status == "completed"
        assert isinstance(event.review, ReviewFull)
        assert event.review.issues[0].suggested_rewrite

    def test_review_failed(self):
        raw, headers = _signed(_load("review_webhook_failed.json"))
        event = LenzWebhooks(secret=SECRET).parse(raw, headers)
        assert isinstance(event, ReviewEvent)
        assert event.event == "review.failed"
        assert event.review.failure.failure_reason == "no_claim"

    def test_event_id_is_stable_across_attempts(self):
        first = parse_webhook(_load("review_webhook_completed.json"))
        retry = parse_webhook(dict(_load("review_webhook_completed.json"), attempt=3))
        assert first.event_id == retry.event_id
        assert (first.attempt, retry.attempt) == (1, 3)

    def test_parse_webhook_accepts_bytes_str_and_dict(self):
        payload = _load("review_webhook_completed.json")
        for body in (json.dumps(payload).encode(), json.dumps(payload), payload):
            assert isinstance(parse_webhook(body), ReviewEvent)

    def test_verification_events_are_unchanged(self):
        event = parse_webhook(
            {"event": "verification.completed", "task_id": "t", "verification_id": "abcd1234", "result": {}}
        )
        assert isinstance(event, VerificationCompleted)

    def test_unknown_events_parse_as_the_base_event(self):
        event = parse_webhook({"event": "review.archived", "task_id": "t"})
        assert type(event).__name__ == "WebhookEvent"
        assert event.event == "review.archived"

    def test_a_malformed_review_body_still_yields_the_event(self):
        payload = _load("review_webhook_completed.json")
        payload["review"] = "not an object"
        event = parse_webhook(payload)
        assert isinstance(event, ReviewEvent)
        assert event.review is None
        assert event.event_id == "evt_a43e6ae7646f275d92f15223"

    def test_a_malformed_attempt_does_not_break_parsing(self):
        event = parse_webhook(dict(_load("review_webhook_completed.json"), attempt="x"))
        assert event.attempt == 1

    def test_parse_webhook_rejects_non_objects(self):
        with pytest.raises(ValueError):
            parse_webhook(b"[1, 2]")


def test_review_names_are_public():
    import lenz_io
    import lenz_io.models

    exported = (
        "ReviewStarted",
        "ReviewFull",
        "ReviewIssues",
        "ReviewIssue",
        "ReviewClaim",
        "ReviewFailure",
        "Escalation",
        "EscalationPolicy",
        "FailureBlock",
        "ReviewEvent",
        "ReviewFailed",
        "ReviewTimeout",
        "parse_webhook",
    )
    for name in exported:
        assert name in lenz_io.__all__, name
    # Shapes a caller receives but never constructs or annotates: importable
    # from lenz_io.models, and kept out of the top-level semver promise.
    nested = (
        "ReviewEnvelope",
        "ReviewResult",
        "ReviewAssessment",
        "ReviewVerification",
        "ReviewEntity",
        "ReviewSummary",
        "ReviewCredits",
        "ReviewAssessmentCounts",
        "ReviewVerificationCounts",
    )
    for name in nested:
        assert name not in lenz_io.__all__, name
        assert name in lenz_io.models.__all__, name
    assert Lenz.review.__doc__ and Lenz.get_review.__doc__ and Lenz.review_and_wait.__doc__
