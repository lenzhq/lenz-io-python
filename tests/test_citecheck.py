"""``POST /citecheck`` and ``GET /citechecks/{citecheck_id}``: the submit call
(text or pairs), the typed body, the wait helper, the webhook event and the
errors.

The completed bodies are recorded server responses (``tests/fixtures/
contract/citecheck_*.json``, shared with the Node SDK): one check of a draft's
first four citations, and one of two statement-source pairs.
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
    Citecheck,
    CitecheckEvent,
    CitecheckFailed,
    CitecheckStarted,
    CitecheckTimeout,
    LenzGoneError,
    LenzPipelineError,
    LenzTimeoutError,
    LenzWebhooks,
    parse_webhook,
)

BASE = "https://lenz.io/api/v1"
FIXTURES = Path(__file__).parent / "fixtures" / "contract"
DRAFT = "Water boils at 100 degrees Celsius, according to [the entry](https://en.wikipedia.org/wiki/Boiling_point)."


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


COMPLETED = _load("citecheck_completed.json")
CHECK_ID = COMPLETED["citecheck_id"]


@pytest.fixture()
def no_sleep(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr("lenz_io.client.time.sleep", lambda s: slept.append(s))
    return slept


def _running() -> dict:
    """The completed body with its rows set back to waiting."""
    body = json.loads(json.dumps(COMPLETED))
    body.update(status="checking", outcome=None, completed_at=None, poll_after_seconds=5)
    for row in body["citations"]:
        row["result"] = None
        row["check"]["status"] = "running"
    body["citation_issues"] = []
    body["summary"]["citation_checks"] = {"checked": 0, "unchecked": 0, "failed": 0}
    return body


# ── submit ──────────────────────────────────────────────────────────────────


class TestSubmit:
    def _body(self, client, *args, **kw) -> dict:
        with respx.mock(base_url=BASE) as r:
            route = r.post("/citecheck").respond(202, json=_load("citecheck_accepted.json"))
            started = client.citecheck(*args, idempotency_key="k", **kw)
        assert isinstance(started, CitecheckStarted)
        assert (started.citecheck_id, started.status) == (CHECK_ID, "queued")
        return json.loads(route.calls.last.request.content)

    def test_text_is_sent_as_it_is(self, client):
        assert self._body(client, DRAFT) == {"text": DRAFT}

    def test_text_with_the_options(self, client):
        body = self._body(client, DRAFT, max_citations=4, language="de", webhook_url="")
        assert body == {"text": DRAFT, "max_citations": 4, "language": "de", "webhook_url": ""}

    def test_pairs_are_sent_as_they_are(self, client):
        pairs = [
            {"statement": "Water boils at 100 degrees Celsius.", "url": "https://example.org/boiling"},
            {"statement": "Diamond sensors measure heat in cells.", "doi": "10.1038/nature12373", "cited_year": "2015"},
        ]
        assert self._body(client, pairs=pairs) == {"pairs": pairs}

    def test_a_pair_has_no_language_of_its_own(self):
        from lenz_io import CitationPair

        # `language` is request-level only: the output language of the reasoning.
        assert "language" not in CitationPair.__annotations__

    def test_sends_an_idempotency_key(self, client):
        with respx.mock(base_url=BASE) as r:
            route = r.post("/citecheck").respond(202, json=_load("citecheck_accepted.json"))
            client.citecheck(DRAFT)
            client.citecheck(DRAFT, idempotency_key="mine")
        first, second = (c.request.headers["Idempotency-Key"] for c in route.calls)
        assert len(first) == 32 and second == "mine"

    @pytest.mark.parametrize(
        ("args", "kw", "match"),
        [
            ((), {}, "exactly one of text and pairs"),
            (("   ",), {}, "exactly one of text and pairs"),
            ((DRAFT,), {"pairs": [{"statement": "s", "url": "https://x.org"}]}, "exactly one of text and pairs"),
            ((), {"pairs": [{"statement": "s", "url": "https://x.org"}], "max_citations": 2}, "goes with text"),
        ],
    )
    def test_bad_combinations_are_refused_before_any_request(self, client, args, kw, match):
        with respx.mock(base_url=BASE, assert_all_called=False) as r:
            route = r.post("/citecheck").respond(202, json=_load("citecheck_accepted.json"))
            with pytest.raises(ValueError, match=match):
                client.citecheck(*args, **kw)
        assert not route.called

    def test_a_conflict_naming_the_check_is_the_receipt(self, client):
        with respx.mock(base_url=BASE) as r:
            r.post("/citecheck").respond(
                409, json={"detail": "still being created", "code": "idempotency_conflict", "citecheck_id": CHECK_ID}
            )
            started = client.citecheck(DRAFT, idempotency_key="k")
        assert started.citecheck_id == CHECK_ID

    def test_a_conflict_naming_no_check_raises(self, client):
        from lenz_io import LenzError

        with respx.mock(base_url=BASE) as r:
            r.post("/citecheck").respond(
                409, json={"detail": "still being created", "code": "idempotency_conflict", "citecheck_id": None}
            )
            with pytest.raises(LenzError):
                client.citecheck(DRAFT, idempotency_key="k")


# ── read ────────────────────────────────────────────────────────────────────


class TestRead:
    def test_a_recorded_check_of_a_draft(self, client):
        with respx.mock(base_url=BASE) as r:
            r.get(f"/citechecks/{CHECK_ID}").respond(200, json=COMPLETED)
            check = client.get_citecheck(CHECK_ID)
        assert isinstance(check, Citecheck)
        assert (check.status, check.outcome, check.policy.max_citations) == ("completed", "issues_found", 4)
        s = check.summary
        assert (s.citations_found, s.citations_selected, s.citation_limit_reached) == (10, 4, True)
        assert s.citation_checks is not None and s.citation_checks.checked == 3
        assert [i.finding for i in check.citation_issues] == ["contradicted", "contradicted"]
        assert check.credits.charged == 3
        assert check.more_citations is not None and len(check.more_citations) == 6
        more = check.more_citations[0]
        assert more.cited_url and more.sentence and more.position is not None

    def test_a_recorded_check_of_pairs(self):
        check = Citecheck.model_validate(_load("citecheck_pairs_completed.json"))
        assert check.more_citations == []
        findings = [c.result.finding for c in check.citations if c.result]
        assert findings == ["supported", "metadata_mismatch"]
        doi = check.citations[1]
        assert doi.doi == "10.1038/nature12373" and doi.check.metadata == "mismatch"

    def test_a_missing_id_is_refused(self, client):
        with pytest.raises(ValueError):
            client.get_citecheck("")

    def test_a_purged_check_is_gone(self, client):
        with respx.mock(base_url=BASE) as r:
            r.get("/citechecks/deadbeef").respond(
                410, json={"detail": "Check removed.", "code": "purged", "purged_at": "2026-10-25T00:00:00Z"}
            )
            with pytest.raises(LenzGoneError):
                client.get_citecheck("deadbeef")


# ── wait ────────────────────────────────────────────────────────────────────


class TestWait:
    def test_polls_until_completed(self, client, no_sleep):
        seen: list[str] = []
        with respx.mock(base_url=BASE) as r:
            r.post("/citecheck").respond(202, json=_load("citecheck_accepted.json"))
            r.get(f"/citechecks/{CHECK_ID}").mock(
                side_effect=[httpx.Response(200, json=_running()), httpx.Response(200, json=COMPLETED)]
            )
            check = client.citecheck_and_wait(DRAFT, on_update=lambda c: seen.append(c.status))
        assert check.outcome == "issues_found"
        assert seen == ["checking", "completed"]
        assert no_sleep == [5.0]

    def test_a_body_that_is_not_this_check_is_a_failed_poll(self, client, no_sleep):
        with respx.mock(base_url=BASE) as r:
            r.post("/citecheck").respond(202, json=_load("citecheck_accepted.json"))
            r.get(f"/citechecks/{CHECK_ID}").mock(
                side_effect=[
                    httpx.Response(200, json={"status": "completed"}),
                    httpx.Response(200, json=dict(COMPLETED, citecheck_id="other")),
                    httpx.Response(200, json=COMPLETED),
                ]
            )
            check = client.citecheck_and_wait(DRAFT)
        assert check.citecheck_id == CHECK_ID

    def test_a_failed_check_raises(self, client, no_sleep):
        failed = dict(
            _running(),
            status="failed",
            outcome="unchecked",
            failure={
                "failure_reason": "upstream_unavailable",
                "failure_class": "upstream_unavailable",
                "retryable": True,
                "hint": "Retry in a few minutes.",
                "docs_url": "https://lenz.io/docs/errors",
            },
        )
        with respx.mock(base_url=BASE) as r:
            r.post("/citecheck").respond(202, json=_load("citecheck_accepted.json"))
            r.get(f"/citechecks/{CHECK_ID}").respond(200, json=failed)
            with pytest.raises(CitecheckFailed) as exc:
                client.citecheck_and_wait(DRAFT)
        err = exc.value
        assert isinstance(err, LenzPipelineError)
        assert (err.citecheck_id, err.error_code, err.retryable) == (CHECK_ID, "upstream_unavailable", True)
        assert err.citecheck is not None and err.citecheck.status == "failed"

    def test_timeout_raises_with_the_last_body(self, client, monkeypatch):
        clock = [0.0]
        monkeypatch.setattr("lenz_io.client.time.monotonic", lambda: clock[0])
        monkeypatch.setattr("lenz_io.client.time.sleep", lambda s: clock.__setitem__(0, clock[0] + s))
        with respx.mock(base_url=BASE) as r:
            r.post("/citecheck").respond(202, json=_load("citecheck_accepted.json"))
            r.get(f"/citechecks/{CHECK_ID}").respond(200, json=_running())
            with pytest.raises(CitecheckTimeout) as exc:
                client.citecheck_and_wait(DRAFT, timeout=12)
        err = exc.value
        assert isinstance(err, LenzTimeoutError) and err.citecheck_id == CHECK_ID
        assert err.partial is not None and err.partial.status == "checking"
        assert clock[0] == 12


# ── webhooks ────────────────────────────────────────────────────────────────

SECRET = "whsec_test"


def _payload() -> dict:
    return {
        "event": "citecheck.completed",
        "event_id": "evt_0123456789abcdef01234567",
        "citecheck_id": CHECK_ID,
        "task_id": "3af4392a7d6747289b88c11778d32d08",
        "status": "completed",
        "citecheck": COMPLETED,
        "attempt": 1,
        "delivered_at": datetime.now(timezone.utc).isoformat(),
    }


def test_the_webhook_parses_into_a_citecheck_event():
    payload = _payload()
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    for event in (LenzWebhooks(secret=SECRET).parse(raw, {"X-Lenz-Signature": sig}), parse_webhook(payload)):
        assert isinstance(event, CitecheckEvent)
        assert (event.event, event.citecheck_id, event.event_id) == (
            "citecheck.completed",
            CHECK_ID,
            "evt_0123456789abcdef01234567",
        )
        assert isinstance(event.citecheck, Citecheck) and len(event.citecheck.citation_issues) == 2


def test_a_webhook_whose_body_does_not_parse_keeps_the_raw_payload():
    event = parse_webhook(dict(_payload(), event="citecheck.failed", citecheck="not a body"))
    assert isinstance(event, CitecheckEvent) and event.citecheck is None
    assert event.raw["citecheck"] == "not a body"
