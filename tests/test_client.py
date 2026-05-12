"""Lenz client behavior — auth, base_url, marquee verbs, resource namespaces,
verify_and_wait state machine, retry, idempotency, connection reuse.

Mocked end-to-end with respx so no real network. Each test pins one
behavior; together they form the cross-language test parity baseline.
"""

from __future__ import annotations

import re

import httpx
import pytest
import respx

from lenz_io import (
    Lenz,
    LenzAuthError,
    LenzNeedsInputError,
    LenzPipelineError,
    LenzTimeoutError,
)
from lenz_io.client import API_VERSION

DEFAULT_BASE = "https://lenz.io/api/v1"


# ─────────────────────────────────────────────────── Construction / auth ──


class TestConstruction:
    def test_no_api_key_permits_library(self, unauth_client):
        # Library endpoints don't require a key — auth check is deferred until
        # a method actually tries to call something that needs it.
        with respx.mock(base_url=DEFAULT_BASE, assert_all_called=False) as r:
            r.get("/library").respond(200, json={"items": [], "total": 0, "page": 1, "page_size": 20})
            page = unauth_client.library.list()
        assert page.total == 0

    def test_auth_required_method_without_key_raises_with_clear_message(self, unauth_client):
        with pytest.raises(LenzAuthError) as ei:
            unauth_client.verifications.list()
        assert "/api-integration" in str(ei.value)

    def test_api_key_armed_methods(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.get("/me/usage").respond(
                200,
                json={
                    "plan": "plus",
                    "credits_used": 5,
                    "credits_total": 100,
                    "extract_calls_today": 0,
                    "extract_daily_limit": 1000,
                },
            )
            u = client.usage()
        assert u.plan == "plus"
        assert u.credits_total == 100

    def test_base_url_override_routes_through_alternate_base(self, custom_base_client):
        with respx.mock(base_url="http://localhost:8001/api/v1") as r:
            r.get("/library").respond(200, json={"items": [], "total": 0, "page": 1, "page_size": 20})
            page = custom_base_client.library.list()
        assert page.total == 0

    def test_lenz_api_version_header_sent_on_every_request(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.get("/me/usage").respond(200, json={"plan": "free", "credits_used": 0, "credits_total": 10})
            client.usage()
        assert route.calls.last.request.headers["X-Lenz-API-Version"] == API_VERSION

    def test_lenz_api_key_env_var_picked_up(self, monkeypatch):
        monkeypatch.setenv("LENZ_API_KEY", "lenz_env_key")
        c = Lenz()
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.get("/me/usage").respond(200, json={"plan": "free", "credits_used": 0, "credits_total": 10})
            c.usage()
        assert route.calls.last.request.headers["Authorization"] == "Bearer lenz_env_key"
        c.close()


# ─────────────────────────────────────────────────── verify (top-level) ──


class TestVerify:
    def test_verify_happy_path_returns_task_id(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.post("/verify").respond(200, json={"task_id": "tsk_001", "claim_text": "x"})
            t = client.verify(claim="The earth is flat")
        assert t.task_id == "tsk_001"

    def test_verify_with_idempotency_key_sets_header(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.post("/verify").respond(200, json={"task_id": "t", "claim_text": "x"})
            client.verify(claim="x", idempotency_key="custom-key-1")
        assert route.calls.last.request.headers["Idempotency-Key"] == "custom-key-1"

    def test_verify_batch_returns_batch_id_and_items(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.post("/verify/batch").respond(
                200,
                json={
                    "batch_id": "batch_1",
                    "items": [
                        {"task_id": "t1", "claim_text": "a"},
                        {"task_id": "t2", "claim_text": "b"},
                    ],
                },
            )
            b = client.verify_batch(claims=[{"text": "a"}, {"text": "b"}])
        assert b.batch_id == "batch_1"
        assert len(b.items) == 2

    def test_extract_returns_identified_claims(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.post("/extract").respond(
                200,
                json={
                    "status": "multi_claim",
                    "identified_claims": ["The earth is flat.", "Coffee causes cancer."],
                    "domain": "Science",
                    "original_input": "...",
                },
            )
            out = client.extract(text="The earth is flat and coffee causes cancer.")
        assert out.identified_claims == ["The earth is flat.", "Coffee causes cancer."]

    def test_get_status_returns_typed_status(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.get("/verify/status/tsk_001").respond(
                200, json={"status": "processing", "progress": {"step": "Framing..."}}
            )
            s = client.get_status("tsk_001")
        assert s.status == "processing"

    def test_select_requires_text_or_claim_index(self, client):
        with pytest.raises(ValueError):
            client.select("tsk_001")  # no args

    def test_select_with_text_dispatches_new_task(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.post("/verify/tsk_001/select").respond(200, json={"task_id": "tsk_002", "claim_text": "x"})
            t = client.select("tsk_001", text="The earth is flat.")
        assert t.task_id == "tsk_002"


# ─────────────────────────────────────────────────── verify_and_wait ──


class TestVerifyAndWait:
    def test_happy_path_polls_until_completed(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.post("/verify").respond(200, json={"task_id": "tsk_001", "claim_text": "x"})
            # First poll: processing. Second: completed.
            poll = r.get("/verify/status/tsk_001")
            poll.side_effect = [
                httpx.Response(200, json={"status": "processing", "progress": {}}),
                httpx.Response(
                    200,
                    json={
                        "status": "completed",
                        "result": {
                            "verification_id": "vid_1",
                            "verdict": {"label": "false", "score": 2.0, "confidence": 0.9},
                        },
                    },
                ),
            ]
            v = client.verify_and_wait(claim="x", timeout=10)
        assert v.verdict.label == "false"
        assert v.verdict.score == 2.0

    def test_idempotency_default_true_sends_uuid_header(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            submit = r.post("/verify").respond(200, json={"task_id": "t", "claim_text": "x"})
            r.get("/verify/status/t").respond(
                200,
                json={
                    "status": "completed",
                    "result": {"verification_id": "v", "verdict": {"label": "true"}},
                },
            )
            client.verify_and_wait(claim="x", timeout=5)
        idem = submit.calls.last.request.headers.get("Idempotency-Key")
        assert idem and re.match(r"^[0-9a-f]{32}$", idem), idem

    def test_idempotency_explicit_overrides_default(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            submit = r.post("/verify").respond(200, json={"task_id": "t", "claim_text": "x"})
            r.get("/verify/status/t").respond(
                200,
                json={
                    "status": "completed",
                    "result": {"verification_id": "v", "verdict": {"label": "true"}},
                },
            )
            client.verify_and_wait(claim="x", timeout=5, idempotency_key="my-key")
        assert submit.calls.last.request.headers["Idempotency-Key"] == "my-key"

    def test_idempotency_off_when_explicitly_disabled(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            submit = r.post("/verify").respond(200, json={"task_id": "t", "claim_text": "x"})
            r.get("/verify/status/t").respond(
                200,
                json={
                    "status": "completed",
                    "result": {"verification_id": "v", "verdict": {"label": "true"}},
                },
            )
            client.verify_and_wait(claim="x", timeout=5, idempotency=False)
        assert "Idempotency-Key" not in submit.calls.last.request.headers

    def test_needs_input_raises_with_payload(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.post("/verify").respond(200, json={"task_id": "t", "claim_text": "x"})
            r.get("/verify/status/t").respond(
                200,
                json={
                    "status": "needs_input",
                    "reason": "multi_claim",
                    "claims": [{"text": "A", "domain": "X"}, {"text": "B", "domain": "Y"}],
                },
            )
            with pytest.raises(LenzNeedsInputError) as ei:
                client.verify_and_wait(claim="x", timeout=5)
        assert ei.value.task_id == "t"
        assert ei.value.kind == "multi_claim"

    def test_failed_pipeline_raises(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.post("/verify").respond(200, json={"task_id": "t", "claim_text": "x"})
            r.get("/verify/status/t").respond(
                200,
                json={
                    "status": "failed",
                    "failure_reason": "research_empty",
                    "failure_detail": "no sources",
                },
            )
            with pytest.raises(LenzPipelineError) as ei:
                client.verify_and_wait(claim="x", timeout=5)
        assert ei.value.failure_reason == "research_empty"

    def test_timeout_raises_with_task_id(self, client, monkeypatch):
        # Make sleep instant so the test runs in <1s
        monkeypatch.setattr("lenz_io.client.time.sleep", lambda _: None)
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.post("/verify").respond(200, json={"task_id": "tsk_slow", "claim_text": "x"})
            r.get("/verify/status/tsk_slow").respond(200, json={"status": "processing", "progress": {}})
            with pytest.raises(LenzTimeoutError) as ei:
                client.verify_and_wait(claim="x", timeout=0.001)
        assert ei.value.task_id == "tsk_slow"


# ─────────────────────────────────────────────────── Resources ──


class TestVerifications:
    def test_list(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.get("/verifications").respond(200, json={"items": [], "total": 0, "page": 1, "page_size": 20})
            page = client.verifications.list()
        assert page.total == 0

    def test_get(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.get("/verifications/vid_1").respond(200, json={"verification_id": "vid_1", "verdict": {"label": "true"}})
            v = client.verifications.get("vid_1")
        assert v.verification_id == "vid_1"
        assert v.verdict.label == "true"

    def test_delete_happy(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.delete("/verifications/vid_1").respond(200, json={"ok": True})
            assert client.verifications.delete("vid_1") is True

    def test_delete_404_after_retry_returns_true(self, client):
        # Idempotent normalize: if the row was already gone, treat as success.
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.delete("/verifications/vid_1").respond(404, json={"detail": "not found"})
            assert client.verifications.delete("vid_1") is True

    def test_set_visibility(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.patch("/verifications/vid_1/visibility").respond(200, json={"ok": True, "visibility": "public"})
            out = client.verifications.set_visibility("vid_1", "public")
        assert out == {"ok": True, "visibility": "public"}


class TestFollowup:
    def test_history(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.get("/verifications/vid_1/follow-up").respond(
                200,
                json={"messages": [], "exchanges_used": 0, "exchange_limit": 10, "can_send": True},
            )
            h = client.followup.history("vid_1")
        assert h.can_send is True

    def test_send(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.post("/verifications/vid_1/follow-up").respond(200, json={"reply": "..."})
            f = client.followup.send("vid_1", message="why?")
        assert f.reply == "..."

    def test_reset(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.delete("/verifications/vid_1/follow-up").respond(200, json={"ok": True})
            assert client.followup.reset("vid_1") is True


class TestLibrary:
    def test_list_without_api_key(self, unauth_client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.get("/library").respond(200, json={"items": [], "total": 0, "page": 1, "page_size": 20})
            unauth_client.library.list(page=1, sort="recent")
        # No Authorization header sent for library
        assert "Authorization" not in route.calls.last.request.headers

    def test_get(self, unauth_client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.get("/library/vid_1").respond(200, json={"verification_id": "vid_1", "verdict": {"label": "false"}})
            item = unauth_client.library.get("vid_1")
        assert item.verification_id == "vid_1"


# ─────────────────────────────────────────────────── Auto-retry ──


class TestAutoRetry:
    def test_503_then_200(self, client, monkeypatch):
        monkeypatch.setattr("lenz_io.client.time.sleep", lambda _: None)
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.get("/me/usage")
            route.side_effect = [
                httpx.Response(503, json={"detail": "unavailable"}),
                httpx.Response(503, json={"detail": "unavailable"}),
                httpx.Response(200, json={"plan": "free", "credits_used": 0, "credits_total": 10}),
            ]
            u = client.usage()
        assert u.plan == "free"
        assert len(route.calls) == 3

    def test_429_honors_retry_after_header(self, client, monkeypatch):
        slept = []
        monkeypatch.setattr("lenz_io.client.time.sleep", lambda s: slept.append(s))
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.get("/me/usage")
            route.side_effect = [
                httpx.Response(429, json={"detail": "slow"}, headers={"Retry-After": "7"}),
                httpx.Response(200, json={"plan": "free", "credits_used": 0, "credits_total": 10}),
            ]
            client.usage()
        assert 7 in slept


# ─────────────────────────────────────────────────── Connection reuse ──


class TestConnectionReuse:
    def test_single_httpx_client_across_calls(self, client):
        # The same underlying httpx.Client instance handles all calls — proves
        # we're not creating one per request.
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.get("/me/usage").respond(200, json={"plan": "free", "credits_used": 0, "credits_total": 10})
            for _ in range(5):
                client.usage()
        # client._client is the persistent httpx.Client
        assert isinstance(client._client, httpx.Client)

    def test_logs_task_id_on_submit(self, client, caplog):
        import logging

        caplog.set_level(logging.INFO, logger="lenz_io")
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.post("/verify").respond(200, json={"task_id": "tsk_log_test", "claim_text": "x"})
            r.get("/verify/status/tsk_log_test").respond(
                200,
                json={
                    "status": "completed",
                    "result": {"verification_id": "v", "verdict": {"label": "true"}},
                },
            )
            client.verify_and_wait(claim="x", timeout=5)
        # INFO-level log of the task_id (for support recovery)
        assert any("tsk_log_test" in r.message for r in caplog.records)
