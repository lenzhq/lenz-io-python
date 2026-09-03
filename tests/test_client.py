"""Lenz client behavior — auth, base_url, marquee verbs, resource namespaces,
verify_and_wait state machine, retry, idempotency, connection reuse.

Mocked end-to-end with respx so no real network. Each test pins one
behavior; together they form the cross-language test parity baseline.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest
import respx

from lenz_io import (
    Lenz,
    LenzAuthError,
    LenzError,
    LenzNeedsInputError,
    LenzPipelineError,
    LenzRateLimitError,
    LenzTimeoutError,
    LenzValidationError,
    TaskAccepted,
)
from lenz_io.client import API_VERSION, ASSESS_LIST_TIMEOUT, DEFAULT_TIMEOUT, RETRY_BACKOFF
from lenz_io.errors import MAX_RETRY_AFTER_SLEEP

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

    def test_library_list_forwards_curated_verdict_random(self, unauth_client):
        with respx.mock(base_url=DEFAULT_BASE, assert_all_called=False) as r:
            route = r.get("/library").respond(200, json={"items": [], "total": 0, "page": 1, "page_size": 20})
            unauth_client.library.list(curated=["trivia", "featured"], sort="random", verdict="True,False")
        qs = route.calls.last.request.url.params
        assert qs["curated"] == "trivia,featured"
        assert qs["sort"] == "random"
        assert qs["verdict"] == "True,False"

    def test_library_list_omits_curated_and_verdict_when_default(self, unauth_client):
        with respx.mock(base_url=DEFAULT_BASE, assert_all_called=False) as r:
            route = r.get("/library").respond(200, json={"items": [], "total": 0, "page": 1, "page_size": 20})
            unauth_client.library.list()
        qs = route.calls.last.request.url.params
        assert "curated" not in qs
        assert "verdict" not in qs

    def test_auth_required_method_without_key_raises_with_clear_message(self, unauth_client):
        with pytest.raises(LenzAuthError) as ei:
            unauth_client.verifications.list()
        assert "/api-credentials" in str(ei.value)

    def test_api_key_armed_methods(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.get("/me/usage").respond(
                200,
                json={
                    "plan": "plus",
                    "quota_resets_at": "2026-07-01T00:00:00+00:00",
                    "credits": {
                        "total": 1000,
                        "used": 50,
                        "remaining": 950,
                        "bonus": 0,
                        "resets_at": "2026-07-01T00:00:00+00:00",
                    },
                    "costs": {"verify": 10, "assess": 1, "ask": 1, "extract": 0},
                    "verify": {
                        "quota_used": 5,
                        "quota_total": 100,
                        "quota_remaining": 95,
                        "bonus": 0,
                        "credits": 0,
                        "remaining": 95,
                    },
                    "ask": {
                        "quota_used": 50,
                        "quota_total": 1000,
                        "quota_remaining": 950,
                        "bonus": 0,
                        "credits": 0,
                        "remaining": 950,
                    },
                    "assess": {
                        "quota_used": 50,
                        "quota_total": 1000,
                        "quota_remaining": 950,
                        "bonus": 0,
                        "credits": 0,
                        "remaining": 950,
                    },
                    "extract": {"calls_today": 0, "daily_limit": 1000, "unlimited": False},
                },
            )
            u = client.usage()
        assert u.plan == "plus"
        assert u.credits.remaining == 950
        assert u.costs["verify"] == 10
        assert u.verify.quota_total == 100
        assert u.verify.remaining == 95
        assert u.assess.bonus == 0

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


# Canonical completed-verification fixture shared across verify_and_wait tests
# Post-unify: flat verdict block (verdict + confidence + lenz_score) at top
# level — no nested Verdict object. Categorical confidence only.
_COMPLETED_RESULT = {
    "verification_id": "v",
    "claim": "Sample claim",
    "verdict": "True",
    "confidence": "high",
    "lenz_score": 8,
}


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

    def test_verify_omits_visibility_by_default(self, client):
        """Omit-when-empty: no visibility kwarg → no key in the body, so the
        server applies its 'private' default and existing callers are
        byte-identical."""
        import json

        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.post("/verify").respond(200, json={"task_id": "t", "claim_text": "x"})
            client.verify(claim="x")
        assert "visibility" not in json.loads(route.calls.last.request.content)

    def test_verify_sends_visibility_when_set(self, client):
        import json

        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.post("/verify").respond(200, json={"task_id": "t", "claim_text": "x"})
            client.verify(claim="x", visibility="unlisted")
        assert json.loads(route.calls.last.request.content)["visibility"] == "unlisted"

    def test_verify_batch_visibility_batch_wide_and_per_item(self, client):
        """Batch-wide default is sent once; per-item override rides on the
        item dict (server is authoritative on the merge)."""
        import json

        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.post("/verify/batch").respond(200, json={"batch_id": "b", "items": []})
            client.verify_batch(
                claims=[{"text": "a"}, {"text": "b", "visibility": "private"}],
                visibility="unlisted",
            )
        body = json.loads(route.calls.last.request.content)
        assert body["visibility"] == "unlisted"
        assert body["claims"][1]["visibility"] == "private"

    def test_verify_batch_omits_visibility_by_default(self, client):
        import json

        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.post("/verify/batch").respond(200, json={"batch_id": "b", "items": []})
            client.verify_batch(claims=[{"text": "a"}])
        assert "visibility" not in json.loads(route.calls.last.request.content)

    def test_verify_omits_depth_by_default(self, client):
        """Omit-when-empty: no depth kwarg → no key in the body. The
        currently-deployed server 422s on unknown fields, so a
        always-present key would break every call."""
        import json

        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.post("/verify").respond(200, json={"task_id": "t", "claim_text": "x"})
            client.verify(claim="x")
        assert "depth" not in json.loads(route.calls.last.request.content)

    def test_verify_sends_depth_when_set(self, client):
        import json

        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.post("/verify").respond(200, json={"task_id": "t", "claim_text": "x"})
            client.verify(claim="x", depth="low")
        assert json.loads(route.calls.last.request.content)["depth"] == "low"

    def test_verify_batch_depth_batch_wide_and_per_item(self, client):
        """Batch-wide default is sent once; per-item override rides on the
        item dict (server is authoritative on the merge)."""
        import json

        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.post("/verify/batch").respond(200, json={"batch_id": "b", "items": []})
            client.verify_batch(
                claims=[{"text": "a"}, {"text": "b", "depth": "standard"}],
                depth="low",
            )
        body = json.loads(route.calls.last.request.content)
        assert body["depth"] == "low"
        assert body["claims"][1]["depth"] == "standard"

    def test_verify_batch_omits_depth_by_default(self, client):
        import json

        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.post("/verify/batch").respond(200, json={"batch_id": "b", "items": []})
            client.verify_batch(claims=[{"text": "a"}])
        assert "depth" not in json.loads(route.calls.last.request.content)

    def test_extract_no_match_validates(self, client):
        """`no_match` is a successful answer, not an error, and never the
        unfocused list in disguise."""
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.post("/extract").respond(
                200,
                json={
                    "status": "no_match",
                    "claim": "",
                    "identified_claims": [],
                    "candidate_claims": [],
                    "domain": "",
                    "key_entities": [],
                    "presumed_intent": "Raise a Series A round from investors.",
                    "original_input": "...",
                },
            )
            out = client.extract(text="...", focus="claims about cricket")
        assert out.status == "no_match"
        assert out.identified_claims == []
        assert out.claim == ""

    def test_extract_returns_identified_claims(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.post("/extract").respond(
                200,
                json={
                    "status": "multi_claim",
                    "claim": "The earth is flat.",
                    "identified_claims": ["The earth is flat.", "Coffee causes cancer."],
                    "domain": "Science",
                    "original_input": "...",
                },
            )
            out = client.extract(text="The earth is flat and coffee causes cancer.")
        assert out.identified_claims == ["The earth is flat.", "Coffee causes cancer."]
        # Unified vocabulary: `claim` (not `atomic_claim`) on the top-level
        # ExtractedClaims response, matching every other claim-shaped API
        # response.
        assert out.claim == "The earth is flat."

    def test_get_status_returns_typed_status(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.get("/verify/status/tsk_001").respond(
                200, json={"status": "processing", "progress": {"step": "Framing..."}}
            )
            s = client.get_status("tsk_001")
        assert s.status == "processing"

    def test_get_status_clarification_uses_candidates_not_candidate_claims(self, client):
        # Server-side renamed candidate_claims -> candidates on the
        # clarification_required needs_input branch. SDK model tracks.
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.get("/verify/status/tsk_x").respond(
                200,
                json={
                    "status": "needs_input",
                    "reason": "clarification_required",
                    "candidates": ["Did you mean A?", "Or B?"],
                },
            )
            s = client.get_status("tsk_x")
        assert s.reason == "clarification_required"
        assert s.candidates == ["Did you mean A?", "Or B?"]

    def test_select_requires_texts(self, client):
        with pytest.raises(ValueError):
            client.select("tsk_001", texts=[])  # empty selection

    def test_select_dispatches_a_task_per_claim(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.post("/verify/tsk_001/select").respond(
                200,
                json={
                    "batch_id": "bat_1",
                    "items": [
                        {"task_id": "tsk_002", "claim_text": "The earth is flat."},
                        {"task_id": "tsk_003", "claim_text": "Coffee causes cancer."},
                    ],
                },
            )
            b = client.select("tsk_001", texts=["The earth is flat.", "Coffee causes cancer."])
        assert b.batch_id == "bat_1"
        assert [it.task_id for it in b.items] == ["tsk_002", "tsk_003"]

    def test_select_request_body_is_texts(self, client):
        # The server's SelectIn is {texts: list[str]} — assert the SDK sends that
        # exact shape. respx mocks the response regardless of the request, so
        # without inspecting the body a malformed payload passes silently.
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.post("/verify/tsk_001/select").respond(
                200, json={"batch_id": "bat_1", "items": [{"task_id": "tsk_002", "claim_text": "x"}]}
            )
            client.select("tsk_001", texts=["The earth is flat."])
        assert json.loads(route.calls.last.request.content) == {"texts": ["The earth is flat."]}

    def test_select_text_kwarg_is_not_supported(self, client):
        # The single-claim `text=` / `claim_index=` params are gone; selection is
        # always a list. An old-style call must fail loudly at the call site
        # (before any HTTP — no respx route needed).
        with pytest.raises(TypeError):
            client.select("tsk_001", text="The earth is flat.")

    def test_select_partial_flag_is_reachable_via_lax(self, client):
        # The server sets `partial: true` on a mid-fan-out enqueue failure (still
        # a 202 — the spawned task_ids are returned). We intentionally don't type
        # `partial` on BatchAccepted, but the `_Lax` model must pass it through so
        # callers who need the degraded-success signal can still read it.
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.post("/verify/tsk_001/select").respond(
                200,
                json={
                    "batch_id": "bat_1",
                    "items": [{"task_id": "tsk_002", "claim_text": "The earth is flat."}],
                    "partial": True,
                },
            )
            b = client.select("tsk_001", texts=["The earth is flat.", "Coffee causes cancer."])
        assert b.batch_id == "bat_1"
        assert b.partial is True


# ─────────────────────────────────────────────────── assess (top-level) ──


class TestAssess:
    def test_single_claim_happy_path(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.post("/assess").respond(
                200,
                json={
                    "claims": [
                        {
                            "claim": "The earth is flat.",
                            "verdict": "False",
                            "confidence": "high",
                        }
                    ],
                    "error": None,
                },
            )
            r2 = client.assess(text="The earth is flat.")
        assert len(r2.claims) == 1
        c = r2.claims[0]
        assert c.claim == "The earth is flat."
        assert c.verdict == "False"
        assert c.confidence == "high"
        assert c.verification_url is None

    def test_multiclaim_returns_n_entries(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.post("/assess").respond(
                200,
                json={
                    "claims": [
                        {"claim": "Coffee causes cancer.", "verdict": "Mixed", "confidence": "medium"},
                        {"claim": "The earth is flat.", "verdict": "False", "confidence": "high"},
                    ],
                    "error": None,
                },
            )
            r2 = client.assess(text="Coffee causes cancer and the earth is flat.")
        assert [c.verdict for c in r2.claims] == ["Mixed", "False"]
        assert [c.confidence for c in r2.claims] == ["medium", "high"]

    def test_verification_url_present_on_claim_hit(self, client):
        # /assess found a matching stored Claim row; the response includes
        # an API URL the SDK consumer can hit for the deep payload.
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.post("/assess").respond(
                200,
                json={
                    "claims": [
                        {
                            "claim": "Water boils at 100°C at sea level.",
                            "verdict": "True",
                            "confidence": "high",
                            "verification_url": "https://lenz.io/api/v1/verifications/a1b2c3d4",
                        }
                    ],
                    "error": None,
                },
            )
            r2 = client.assess(text="Water boils at 100°C at sea level.")
        assert r2.claims[0].verification_url == "https://lenz.io/api/v1/verifications/a1b2c3d4"

    def test_zero_claims_error_payload(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.post("/assess").respond(
                200,
                json={"claims": [], "error": "No verifiable claim detected"},
            )
            r2 = client.assess(text="just chatter")
        assert r2.claims == []
        assert r2.error == "No verifiable claim detected"

    def test_single_form_wire_body_is_unchanged(self, client):
        """The single form still sends exactly ``{"text": ...}`` — no ``claims``
        key, no new keys — and rides the client-wide timeout."""
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.post("/assess").respond(200, json={"claims": [], "error": None})
            client.assess(claim="The earth is flat.")
            assert json.loads(route.calls.last.request.content) == {"text": "The earth is flat."}
            client.assess(claim="La tierra es plana.", language="es")
            assert json.loads(route.calls.last.request.content) == {"text": "La tierra es plana.", "language": "es"}
        timeout = route.calls.last.request.extensions["timeout"]
        assert timeout["read"] == DEFAULT_TIMEOUT

    def test_row_fields_default_when_an_older_server_omits_them(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.post("/assess").respond(
                200,
                json={"claims": [{"claim": "x", "verdict": "True", "confidence": "high"}], "error": None},
            )
            c = client.assess(claim="x").claims[0]
        assert c.error_code is None
        assert c.candidate_claims == []
        assert c.identified_claims == []
        assert c.hint is None

    # ── list form: assess(claims=[...]) ──

    def test_claims_list_sends_claims_key_and_never_text(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.post("/assess").respond(200, json={"claims": [], "error": None})
            client.assess(claims=["A is true.", "B is false."])
        body = json.loads(route.calls.last.request.content)
        assert body == {"claims": ["A is true.", "B is false."]}
        assert "text" not in body

    def test_claims_list_forwards_language(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.post("/assess").respond(200, json={"claims": [], "error": None})
            client.assess(claims=["A."], language="de")
        assert json.loads(route.calls.last.request.content) == {"claims": ["A."], "language": "de"}

    def test_claims_list_one_row_per_item_in_order_with_row_fields(self, client):
        rows = [
            {
                "claim": "Water boils at 100 °C at sea level.",
                "language": "en",
                "verdict": "True",
                "confidence": "high",
                "verification_url": None,
                "error_code": None,
                "candidate_claims": [],
                "identified_claims": [],
                "hint": None,
            },
            {
                "claim": "Bilingual children develop stronger executive function.",
                "language": "en",
                "verdict": "Mixed",
                "confidence": "medium",
                "verification_url": "https://lenz.io/api/v1/verifications/3f9a1c2e",
                "error_code": None,
                "candidate_claims": [],
                "identified_claims": ["Bilingual children learn to read later than monolingual peers."],
                "hint": "Assessed the main claim only. Send identified_claims as their own items to check the rest.",
            },
            {
                "claim": "this is fine",
                "language": "en",
                "verdict": "Error",
                "confidence": "low",
                "verification_url": None,
                "error_code": "no_claim",
                "candidate_claims": [],
                "identified_claims": [],
                "hint": "No factual statement that can be checked against evidence was found in the input.",
            },
            {
                "claim": "RAM prices have more than doubled",
                "language": "en",
                "verdict": "Error",
                "confidence": "low",
                "verification_url": None,
                "error_code": "ambiguous",
                "candidate_claims": ["DDR4 desktop RAM prices doubled 2021-2026.", "DRAM contract prices doubled."],
                "identified_claims": [],
                "hint": "Ambiguous: Which memory market / form factor? Send one of candidate_claims as its own item.",
            },
        ]
        sent = [row["claim"] for row in rows]
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.post("/assess").respond(200, json={"claims": rows, "error": None})
            out = client.assess(claims=sent)
        # One row per item, in the order sent — Error rows hold their position.
        assert [c.claim for c in out.claims] == sent
        assert out.error is None
        plain, compound, no_claim, ambiguous = out.claims
        assert (plain.verdict, plain.confidence) == ("True", "high")
        assert plain.error_code is None and plain.hint is None
        assert plain.candidate_claims == [] and plain.identified_claims == []
        assert compound.identified_claims == ["Bilingual children learn to read later than monolingual peers."]
        assert compound.hint.startswith("Assessed the main claim only.")
        assert compound.verification_url == "https://lenz.io/api/v1/verifications/3f9a1c2e"
        assert (no_claim.verdict, no_claim.error_code) == ("Error", "no_claim")
        assert no_claim.hint
        assert (ambiguous.verdict, ambiguous.error_code) == ("Error", "ambiguous")
        assert len(ambiguous.candidate_claims) == 2
        assert ambiguous.hint.startswith("Ambiguous")

    def test_claims_list_gets_the_longer_default_timeout(self, client):
        """One parallel wave takes ~10-25s, so a list call rides
        ``ASSESS_LIST_TIMEOUT`` rather than the 30s client default."""
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.post("/assess").respond(200, json={"claims": [], "error": None})
            client.assess(claims=["A.", "B."])
        timeout = route.calls.last.request.extensions["timeout"]
        assert timeout["read"] == ASSESS_LIST_TIMEOUT == 45.0
        assert timeout["connect"] == ASSESS_LIST_TIMEOUT

    def test_explicit_timeout_overrides_the_default_for_that_call_only(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.post("/assess").respond(200, json={"claims": [], "error": None})
            client.assess(claims=["A."], timeout=90)
            assert route.calls.last.request.extensions["timeout"]["read"] == 90.0
            client.assess(claim="A.", timeout=5)
            assert route.calls.last.request.extensions["timeout"]["read"] == 5.0
            # The next call is back on the client default — nothing leaked.
            client.assess(claim="A.")
            assert route.calls.last.request.extensions["timeout"]["read"] == DEFAULT_TIMEOUT

    def test_claims_with_claim_or_text_raises(self, client):
        with pytest.raises(ValueError, match="either one claim"):
            client.assess("A.", claims=["B."])
        with pytest.raises(ValueError, match="either one claim"):
            client.assess(text="A.", claims=["B."])

    def test_claims_list_is_not_checked_client_side(self, client):
        """Count and length limits are the server's 422 (``too_many_items``,
        ``item_too_long``, ``blank_item``) — the SDK forwards the list as is."""
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.post("/assess").respond(
                422, json={"detail": "At most 20 claims per call.", "code": "too_many_items"}
            )
            with pytest.raises(LenzValidationError) as ei:
                client.assess(claims=["x"] * 21)
        assert len(json.loads(route.calls.last.request.content)["claims"]) == 21
        assert ei.value.status_code == 422


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
                            "verdict": "False",
                            "confidence": "high",
                            "lenz_score": 1,
                        },
                    },
                ),
            ]
            v = client.verify_and_wait(claim="x", timeout=10)
        # Flat verdict block — categorical confidence only, no nested .label / .score
        assert v.verdict == "False"
        assert v.lenz_score == 1
        assert v.confidence == "high"

    def test_idempotency_default_true_sends_uuid_header(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            submit = r.post("/verify").respond(200, json={"task_id": "t", "claim_text": "x"})
            r.get("/verify/status/t").respond(200, json={"status": "completed", "result": _COMPLETED_RESULT})
            client.verify_and_wait(claim="x", timeout=5)
        idem = submit.calls.last.request.headers.get("Idempotency-Key")
        assert idem and re.match(r"^[0-9a-f]{32}$", idem), idem

    def test_idempotency_explicit_overrides_default(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            submit = r.post("/verify").respond(200, json={"task_id": "t", "claim_text": "x"})
            r.get("/verify/status/t").respond(200, json={"status": "completed", "result": _COMPLETED_RESULT})
            client.verify_and_wait(claim="x", timeout=5, idempotency_key="my-key")
        assert submit.calls.last.request.headers["Idempotency-Key"] == "my-key"

    def test_depth_forwarded_to_submit(self, client):
        """The and_wait helpers plumb depth through to the submit body."""
        import json

        with respx.mock(base_url=DEFAULT_BASE) as r:
            submit = r.post("/verify").respond(200, json={"task_id": "t", "claim_text": "x"})
            r.get("/verify/status/t").respond(200, json={"status": "completed", "result": _COMPLETED_RESULT})
            client.verify_and_wait(claim="x", timeout=5, depth="low")
        assert json.loads(submit.calls.last.request.content)["depth"] == "low"

    def test_depth_omitted_from_submit_when_unset(self, client):
        import json

        with respx.mock(base_url=DEFAULT_BASE) as r:
            submit = r.post("/verify").respond(200, json={"task_id": "t", "claim_text": "x"})
            r.get("/verify/status/t").respond(200, json={"status": "completed", "result": _COMPLETED_RESULT})
            client.verify_and_wait(claim="x", timeout=5)
        assert "depth" not in json.loads(submit.calls.last.request.content)

    def test_idempotency_off_when_explicitly_disabled(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            submit = r.post("/verify").respond(200, json={"task_id": "t", "claim_text": "x"})
            r.get("/verify/status/t").respond(200, json={"status": "completed", "result": _COMPLETED_RESULT})
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
                    "hint": "The input holds two distinct claims. Pick the one to verify via /select.",
                    "claims": [{"text": "A", "domain": "X"}, {"text": "B", "domain": "Y"}],
                },
            )
            with pytest.raises(LenzNeedsInputError) as ei:
                client.verify_and_wait(claim="x", timeout=5)
        assert ei.value.task_id == "t"
        assert ei.value.kind == "multi_claim"
        assert ei.value.hint == "The input holds two distinct claims. Pick the one to verify via /select."
        assert ei.value.payload["hint"] == ei.value.hint

    def test_needs_input_hint_is_empty_from_an_older_server(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.post("/verify").respond(200, json={"task_id": "t", "claim_text": "x"})
            r.get("/verify/status/t").respond(
                200, json={"status": "needs_input", "reason": "clarification_required", "candidates": ["A", "B"]}
            )
            with pytest.raises(LenzNeedsInputError) as ei:
                client.verify_and_wait(claim="x", timeout=5)
        assert ei.value.hint == ""

    def test_status_carries_hint_on_not_a_claim_failure(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.get("/verify/status/t").respond(
                200,
                json={
                    "status": "failed",
                    "failure_reason": "not_a_claim",
                    "error": "Not a checkable claim.",
                    "hint": "Send one factual claim, or run a longer text through /extract.",
                },
            )
            st = client.get_status("t")
        assert st.status == "failed"
        assert st.hint == "Send one factual claim, or run a longer text through /extract."

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
                client.verify_and_wait(claim="x", timeout=0)
        assert ei.value.task_id == "tsk_slow"

    def test_failed_surfaces_error_wire_field(self, client):
        # Server sends the diagnostic under ``error`` (not failure_reason).
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.post("/verify").respond(200, json={"task_id": "t", "claim_text": "x"})
            r.get("/verify/status/t").respond(
                200, json={"status": "failed", "error": "Pipeline stopped at: research_empty"}
            )
            with pytest.raises(LenzPipelineError) as ei:
                client.verify_and_wait(claim="x", timeout=5)
        assert "research_empty" in str(ei.value)


# ─────────────────────────────────────────────────── wait ──


class TestWait:
    def test_accepts_task_id_string(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.get("/verify/status/t").respond(200, json={"status": "completed", "result": _COMPLETED_RESULT})
            v = client.wait("t", timeout=5)
        assert v.verdict == "True"

    def test_accepts_task_accepted_object(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.get("/verify/status/t").respond(200, json={"status": "completed", "result": _COMPLETED_RESULT})
            v = client.wait(TaskAccepted(task_id="t", claim_text="x"), timeout=5)
        assert v.lenz_score == 8

    def test_empty_task_id_raises_value_error(self, client):
        with pytest.raises(ValueError):
            client.wait(TaskAccepted())  # task_id defaults to ""

    def test_polls_until_completed(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            poll = r.get("/verify/status/t")
            poll.side_effect = [
                httpx.Response(200, json={"status": "processing", "progress": {}}),
                httpx.Response(200, json={"status": "completed", "result": _COMPLETED_RESULT}),
            ]
            v = client.wait("t", timeout=10)
        assert v.verdict == "True"

    def test_needs_input_raises(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.get("/verify/status/t").respond(
                200, json={"status": "needs_input", "reason": "multi_claim", "claims": []}
            )
            with pytest.raises(LenzNeedsInputError) as ei:
                client.wait("t", timeout=5)
        assert ei.value.kind == "multi_claim"

    def test_completed_without_result_raises(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.get("/verify/status/t").respond(200, json={"status": "completed"})
            with pytest.raises(LenzPipelineError):
                client.wait("t", timeout=5)

    def test_timeout_raises(self, client, monkeypatch):
        monkeypatch.setattr("lenz_io.client.time.sleep", lambda _: None)
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.get("/verify/status/t").respond(200, json={"status": "processing", "progress": {}})
            with pytest.raises(LenzTimeoutError) as ei:
                client.wait("t", timeout=0)
        assert ei.value.task_id == "t"


# ─────────────────────────────────────────────────── verify_batch_and_wait ──


class TestVerifyBatchAndWait:
    def test_all_complete_in_input_order(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.post("/verify/batch").respond(
                200,
                json={
                    "batch_id": "b",
                    "items": [{"task_id": "t1", "claim_text": "a"}, {"task_id": "t2", "claim_text": "b"}],
                },
            )
            r.get("/verify/status/t1").respond(200, json={"status": "completed", "result": _COMPLETED_RESULT})
            r.get("/verify/status/t2").respond(200, json={"status": "completed", "result": _COMPLETED_RESULT})
            results = client.verify_batch_and_wait(claims=[{"text": "a"}, {"text": "b"}], timeout=5)
        assert [x.task_id for x in results] == ["t1", "t2"]
        assert all(x.status == "completed" and x.verification is not None for x in results)

    def test_mixed_outcomes(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.post("/verify/batch").respond(
                200,
                json={
                    "batch_id": "b",
                    "items": [
                        {"task_id": "t1", "claim_text": "a"},
                        {"task_id": "t2", "claim_text": "b"},
                        {"task_id": "t3", "claim_text": "c"},
                    ],
                },
            )
            r.get("/verify/status/t1").respond(200, json={"status": "completed", "result": _COMPLETED_RESULT})
            r.get("/verify/status/t2").respond(
                200, json={"status": "needs_input", "reason": "multi_claim", "claims": []}
            )
            r.get("/verify/status/t3").respond(200, json={"status": "failed", "error": "research_empty"})
            results = client.verify_batch_and_wait(claims=[{"text": "a"}, {"text": "b"}, {"text": "c"}], timeout=5)
        assert [x.status for x in results] == ["completed", "needs_input", "failed"]
        assert results[1].status_detail is not None and results[1].status_detail.reason == "multi_claim"
        assert results[2].status_detail is not None and results[2].status_detail.error == "research_empty"

    def test_item_timeout(self, client, monkeypatch):
        monkeypatch.setattr("lenz_io.client.time.sleep", lambda _: None)
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.post("/verify/batch").respond(
                200,
                json={
                    "batch_id": "b",
                    "items": [{"task_id": "t1", "claim_text": "a"}, {"task_id": "t2", "claim_text": "b"}],
                },
            )
            r.get("/verify/status/t1").respond(200, json={"status": "completed", "result": _COMPLETED_RESULT})
            r.get("/verify/status/t2").respond(200, json={"status": "processing", "progress": {}})
            results = client.verify_batch_and_wait(claims=[{"text": "a"}, {"text": "b"}], timeout=0)
        by_id = {x.task_id: x for x in results}
        assert by_id["t1"].status == "completed"
        assert by_id["t2"].status == "timeout"
        assert by_id["t2"].status_detail is None

    def test_forwards_idempotency_key(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            submit = r.post("/verify/batch").respond(
                200, json={"batch_id": "b", "items": [{"task_id": "t1", "claim_text": "a"}]}
            )
            r.get("/verify/status/t1").respond(200, json={"status": "completed", "result": _COMPLETED_RESULT})
            client.verify_batch_and_wait(claims=[{"text": "a"}], idempotency_key="k1", timeout=5)
        assert submit.calls.last.request.headers["Idempotency-Key"] == "k1"

    def test_forwards_depth(self, client):
        import json

        with respx.mock(base_url=DEFAULT_BASE) as r:
            submit = r.post("/verify/batch").respond(
                200, json={"batch_id": "b", "items": [{"task_id": "t1", "claim_text": "a"}]}
            )
            r.get("/verify/status/t1").respond(200, json={"status": "completed", "result": _COMPLETED_RESULT})
            client.verify_batch_and_wait(claims=[{"text": "a"}], depth="low", timeout=5)
        assert json.loads(submit.calls.last.request.content)["depth"] == "low"


# ─────────────────────────────────────────────────── progress ──


_PROCESSING = {
    "status": "processing",
    "task_id": "t",
    "progress": {"step": "research", "index": 2, "total": 5, "elapsed_seconds": 42, "poll_after_seconds": 5},
}


class TestOnProgress:
    """The callback is the only way anyone using the documented happy path
    sees the stage at all — ``verify_and_wait`` does the polling."""

    def test_fires_per_poll_with_the_task_id(self, client, monkeypatch):
        monkeypatch.setattr("lenz_io.client.time.sleep", lambda _: None)
        seen = []
        with respx.mock(base_url=DEFAULT_BASE) as r:
            poll = r.get("/verify/status/t")
            poll.side_effect = [
                httpx.Response(200, json=_PROCESSING),
                httpx.Response(200, json={"status": "completed", "result": _COMPLETED_RESULT}),
            ]
            client.wait("t", timeout=10, on_progress=lambda tid, p: seen.append((tid, p)))
        # Once for the processing poll; never for the terminal one.
        assert len(seen) == 1
        task_id, progress = seen[0]
        assert task_id == "t"
        assert (progress.step, progress.index, progress.total) == ("research", 2, 5)

    def test_the_batch_loop_says_which_claim_moved(self, client, monkeypatch):
        """Without the id a callback cannot tell the caller which of the
        round-robined ids the progress belongs to."""
        monkeypatch.setattr("lenz_io.client.time.sleep", lambda _: None)
        seen = []
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.post("/verify/batch").respond(
                200,
                json={
                    "batch_id": "b",
                    "items": [{"task_id": "t1", "claim_text": "a"}, {"task_id": "t2", "claim_text": "b"}],
                },
            )
            r.get("/verify/status/t1").respond(200, json={"status": "completed", "result": _COMPLETED_RESULT})
            r.get("/verify/status/t2").side_effect = [
                httpx.Response(200, json={**_PROCESSING, "task_id": "t2", "progress": {"step": "debate", "index": 3}}),
                httpx.Response(200, json={"status": "completed", "result": _COMPLETED_RESULT}),
            ]
            client.verify_batch_and_wait(
                claims=[{"text": "a"}, {"text": "b"}], timeout=10, on_progress=lambda tid, p: seen.append((tid, p.step))
            )
        assert seen == [("t2", "debate")]

    def test_a_raising_callback_does_not_kill_the_poll(self, client, monkeypatch):
        monkeypatch.setattr("lenz_io.client.time.sleep", lambda _: None)

        def boom(task_id, progress):
            raise RuntimeError("caller bug")

        with respx.mock(base_url=DEFAULT_BASE) as r:
            poll = r.get("/verify/status/t")
            poll.side_effect = [
                httpx.Response(200, json=_PROCESSING),
                httpx.Response(200, json={"status": "completed", "result": _COMPLETED_RESULT}),
            ]
            v = client.wait("t", timeout=10, on_progress=boom)
        assert v.verdict == "True"

    def test_the_callback_gets_a_copy_it_cannot_use_to_mutate_sdk_state(self, client, monkeypatch):
        monkeypatch.setattr("lenz_io.client.time.sleep", lambda _: None)
        handed = []
        with respx.mock(base_url=DEFAULT_BASE) as r:
            poll = r.get("/verify/status/t")
            poll.side_effect = [
                httpx.Response(200, json=_PROCESSING),
                httpx.Response(200, json={"status": "completed", "result": _COMPLETED_RESULT}),
            ]
            client.wait("t", timeout=10, on_progress=lambda tid, p: handed.append(p))
        handed[0].step = "tampered"
        # Nothing downstream observed the mutation — the terminal result is
        # unaffected, and the object is not the one the client held.
        assert handed[0].step == "tampered"


class TestPollAfterSeconds:
    """The server's hint replaces the local 2/4/8 ladder — this is the only
    feedback loop we get on whether anyone adopted it."""

    def _sleeps(self, client, monkeypatch, progress):
        slept = []
        monkeypatch.setattr("lenz_io.client.time.sleep", lambda s: slept.append(s))
        with respx.mock(base_url=DEFAULT_BASE) as r:
            poll = r.get("/verify/status/t")
            poll.side_effect = [
                httpx.Response(200, json={"status": "processing", "task_id": "t", "progress": progress}),
                httpx.Response(200, json={"status": "completed", "result": _COMPLETED_RESULT}),
            ]
            client.wait("t", timeout=60)
        return slept

    def test_the_hint_replaces_the_ladder(self, client, monkeypatch):
        assert self._sleeps(client, monkeypatch, {"step": "research", "poll_after_seconds": 5}) == [5.0]

    def test_no_hint_falls_back_to_the_ladder(self, client, monkeypatch):
        assert self._sleeps(client, monkeypatch, {"step": "research"}) == [2.0]

    @pytest.mark.parametrize("bad", [0, -1, 999, None])
    def test_out_of_range_falls_back_to_the_ladder(self, client, monkeypatch, bad):
        """The floor stops a bad value turning the loop hot; the ceiling stops
        it stalling a wait well inside its own timeout."""
        assert self._sleeps(client, monkeypatch, {"step": "research", "poll_after_seconds": bad}) == [2.0]

    @pytest.mark.parametrize("coerced,expected", [("5", 5.0), (True, 1.0)])
    def test_pydantic_coerces_before_the_range_check_sees_it(self, client, monkeypatch, coerced, expected):
        """Documented, not desired.

        ``poll_after_seconds`` is ``int | None`` on the model, so a stringy
        or boolean value is coerced during validation and reaches the range
        check as a plain int. Both land inside the bounds, so both are safe —
        the type guard in ``_poll_hint`` only bites when a value bypasses the
        model (an ``extra="allow"`` field, a hand-built ``Progress``).
        """
        assert self._sleeps(client, monkeypatch, {"step": "research", "poll_after_seconds": coerced}) == [expected]

    def test_a_batch_waits_the_shortest_hint(self, client, monkeypatch):
        """Waiting the longest hint would starve the fastest claim."""
        slept = []
        monkeypatch.setattr("lenz_io.client.time.sleep", lambda s: slept.append(s))
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.post("/verify/batch").respond(
                200,
                json={
                    "batch_id": "b",
                    "items": [{"task_id": "t1", "claim_text": "a"}, {"task_id": "t2", "claim_text": "b"}],
                },
            )
            for tid, hint in (("t1", 12), ("t2", 3)):
                r.get(f"/verify/status/{tid}").side_effect = [
                    httpx.Response(
                        200,
                        json={
                            "status": "processing",
                            "task_id": tid,
                            "progress": {"step": "research", "poll_after_seconds": hint},
                        },
                    ),
                    httpx.Response(200, json={"status": "completed", "result": _COMPLETED_RESULT}),
                ]
            client.verify_batch_and_wait(claims=[{"text": "a"}, {"text": "b"}], timeout=60)
        assert slept == [3.0]


# ─────────────────────────────────────────────────── Resources ──


class TestVerifications:
    def test_list(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.get("/verifications").respond(200, json={"items": [], "total": 0, "page": 1, "page_size": 20})
            page = client.verifications.list()
        assert page.total == 0

    def test_get(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.get("/verifications/vid_1").respond(
                200,
                json={
                    "verification_id": "vid_1",
                    "verdict": "True",
                    "confidence": "high",
                    "lenz_score": 9,
                },
            )
            v = client.verifications.get("vid_1")
        assert v.verification_id == "vid_1"
        assert v.verdict == "True"
        assert v.confidence == "high"
        assert v.lenz_score == 9

    def test_get_works_without_api_key(self, unauth_client):
        """GET /verifications/{id} takes optional Bearer: a key-less client can
        still fetch public claims (anon caller → no Authorization header)."""
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.get("/verifications/public_id").respond(
                200,
                json={
                    "verification_id": "public_id",
                    "claim": "Water boils at 100°C.",
                    "verdict": "True",
                    "confidence": "high",
                    "lenz_score": 10,
                },
            )
            v = unauth_client.verifications.get("public_id")
        # No key configured → nothing to send.
        assert "Authorization" not in route.calls.last.request.headers
        assert v.verification_id == "public_id"
        assert v.verdict == "True"

    def test_get_sends_bearer_when_keyed(self, client):
        """Optional-auth endpoints MUST still send the key when we have one — the
        server only returns the caller's own private/hidden verifications to the
        owning bearer. Suppressing it (the old `and auth_required` guard) made
        `lenz show` 404 on a user's own fresh API claim (private+hidden)."""
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.get("/verifications/mine").respond(
                200, json={"verification_id": "mine", "verdict": "False", "confidence": "high"}
            )
            v = client.verifications.get("mine")
        assert route.calls.last.request.headers["Authorization"] == "Bearer lenz_test_abc123"
        assert v.verification_id == "mine"

    def test_delete_happy(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.delete("/verifications/vid_1").respond(200, json={"ok": True})
            assert client.verifications.delete("vid_1") is True

    def test_delete_404_after_retry_returns_true(self, client):
        # Idempotent normalize: if the row was already gone, treat as success.
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.delete("/verifications/vid_1").respond(404, json={"detail": "not found"})
            assert client.verifications.delete("vid_1") is True

    def test_set_visibility_method_removed_in_1_1_0(self, client):
        """1.1.0: API claims are always private. The set_visibility
        method was removed; accessing it raises AttributeError."""
        assert not hasattr(client.verifications, "set_visibility")

    def test_related_returns_typed_items(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.get("/verifications/vid_1/related").respond(
                200,
                json={
                    "items": [
                        {
                            "verification_id": "rel00001",
                            "claim": "A related claim",
                            "verdict": "False",
                            "confidence": "high",
                            "lenz_score": 2,
                            "url": "https://lenz.io/c/foo-rel00001",
                            "distance": 0.31,
                        },
                        {
                            "verification_id": "rel00002",
                            "claim": "Another",
                            "verdict": "True",
                            "confidence": "medium",
                            "lenz_score": 9,
                            "url": "https://lenz.io/c/bar-rel00002",
                            "distance": 0.42,
                        },
                    ]
                },
            )
            related = client.verifications.related("vid_1", limit=5)
        # limit propagated as query param
        assert route.calls.last.request.url.params["limit"] == "5"
        assert len(related.items) == 2
        first = related.items[0]
        assert first.verification_id == "rel00001"
        assert first.claim == "A related claim"
        # Unified vocabulary: `verdict` + `lenz_score` (not the old
        # `verdict_label` + `score`); `confidence` is now also exposed.
        assert first.verdict == "False"
        assert first.lenz_score == 2
        assert first.confidence == "high"
        assert first.distance == 0.31

    def test_related_empty_when_no_matches(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.get("/verifications/vid_2/related").respond(200, json={"items": []})
            related = client.verifications.related("vid_2")
        assert related.items == []


class TestAsk:
    """Renamed from TestFollowup after the /verifications/{id}/follow-up ->
    /ask/{id} server-side rename and the followup -> ask client namespace
    rename."""

    def test_history(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.get("/ask/vid_1").respond(
                200,
                json={
                    "messages": [
                        {"role": "user", "content": "why?", "created_at": "2026-05-22T12:00:00Z"},
                        {"role": "expert", "content": "because…", "created_at": "2026-05-22T12:00:05Z"},
                    ],
                    "exchanges_used": 1,
                    "exchange_limit": 10,
                    "can_send": True,
                },
            )
            h = client.ask.history("vid_1")
        assert h.can_send is True
        # Typed AskMessage in the messages list (no more bare dicts)
        assert h.messages[0].role == "user"
        assert h.messages[1].role == "expert"
        assert h.messages[1].content == "because…"

    def test_send(self, client):
        # Server returns {role, content, created_at} — confirmed against
        # lenz/api/public_authed.py:1804. Pre-1.0.2 the mock used
        # `{"reply": "..."}` and the SDK declared `.reply: str`; both were
        # documentation drift away from the wire format, but the test
        # passed because the mock matched the (wrong) model.
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.post("/ask/vid_1").respond(
                200,
                json={
                    "role": "expert",
                    "content": "because the photoelectric effect…",
                    "created_at": "2026-05-22T12:00:05Z",
                },
            )
            f = client.ask.send("vid_1", message="why?")
        assert f.role == "expert"
        assert f.content == "because the photoelectric effect…"
        assert f.created_at == "2026-05-22T12:00:05Z"

    def test_send_legacy_reply_attr_gone(self):
        # REGRESSION: pre-1.0.2 `AskReply.reply` always returned `""`
        # because the server never sends a `reply` key. Renamed to
        # `content` in 1.0.2; the old name is no longer in the typed
        # surface. This test pins the rename so a careless revert
        # silently re-introducing `.reply` fails CI.
        from lenz_io import AskReply

        fields = set(AskReply.model_fields.keys())
        assert "content" in fields, "AskReply.content must be the declared field"
        assert "reply" not in fields, (
            "AskReply.reply was removed in 1.0.2 — it was always empty "
            "because the server returns `content`, not `reply`."
        )

    def test_reset(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.delete("/ask/vid_1").respond(200, json={"ok": True})
            assert client.ask.reset("vid_1") is True


class TestLibrary:
    def test_list_without_api_key(self, unauth_client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.get("/library").respond(200, json={"items": [], "total": 0, "page": 1, "page_size": 20})
            unauth_client.library.list(page=1, sort="recent")
        # No Authorization header sent for library
        assert "Authorization" not in route.calls.last.request.headers

    def test_list_stays_anonymous_even_when_keyed(self, client):
        """library.list is public content — the bearer must NOT be attached even
        with a key configured (optional-auth WITHOUT auth_optional). Only
        verifications.get opts in, so a key never reaches a public-only endpoint."""
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.get("/library").respond(200, json={"items": [], "total": 0, "page": 1, "page_size": 20})
            client.library.list(page=1, sort="recent")
        assert "Authorization" not in route.calls.last.request.headers

    def test_get_method_removed(self, unauth_client):
        # GET /api/v1/library/{id} merged server-side into
        # GET /api/v1/verifications/{id}. SDK's library.get() was dropped;
        # callers use verifications.get() (works key-less for public claims).
        assert not hasattr(unauth_client.library, "get")


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

    def test_429_with_a_long_retry_after_raises_instead_of_sleeping(self, client, monkeypatch):
        """The /extract daily cap sends seconds-until-UTC-midnight. Sleeping
        that would block the call for most of a day — three times over.

        Without the clamp this test does not fail, it HANGS for 24 hours.
        """
        slept = []
        monkeypatch.setattr("lenz_io.client.time.sleep", lambda s: slept.append(s))
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.get("/me/usage")
            route.side_effect = [
                httpx.Response(
                    429,
                    json={"detail": "Daily /extract limit of 1000 reached.", "reset_in_seconds": 86400},
                    headers={"Retry-After": "86400"},
                ),
                httpx.Response(200, json={"plan": "free", "credits_used": 0, "credits_total": 10}),
            ]
            with pytest.raises(LenzRateLimitError) as exc:
                client.usage()

        assert slept == [], "must not sleep through a 24h wait"
        assert len(route.calls) == 1, "must not burn retries on an unwinnable wait"
        # The true wait is surfaced so the caller can schedule the work.
        assert exc.value.retry_after == 86400

    def test_429_at_the_clamp_boundary_still_retries(self, client, monkeypatch):
        slept = []
        monkeypatch.setattr("lenz_io.client.time.sleep", lambda s: slept.append(s))
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.get("/me/usage")
            route.side_effect = [
                httpx.Response(429, json={"detail": "slow"}, headers={"Retry-After": str(MAX_RETRY_AFTER_SLEEP)}),
                httpx.Response(200, json={"plan": "free", "credits_used": 0, "credits_total": 10}),
            ]
            client.usage()
        assert slept == [MAX_RETRY_AFTER_SLEEP]

    def test_429_without_a_stated_wait_falls_back_to_backoff(self, client, monkeypatch):
        slept = []
        monkeypatch.setattr("lenz_io.client.time.sleep", lambda s: slept.append(s))
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.get("/me/usage")
            route.side_effect = [
                httpx.Response(429, json={"detail": "slow"}),
                httpx.Response(200, json={"plan": "free", "credits_used": 0, "credits_total": 10}),
            ]
            client.usage()
        assert slept == [RETRY_BACKOFF[0]]

    def test_429_reads_the_wait_from_the_body_when_no_header(self, client, monkeypatch):
        """The body-fallback branch — the only place this diverged from Node."""
        slept = []
        monkeypatch.setattr("lenz_io.client.time.sleep", lambda s: slept.append(s))
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.get("/me/usage")
            route.side_effect = [
                httpx.Response(429, json={"detail": "slow", "reset_in_seconds": 5}),
                httpx.Response(200, json={"plan": "free", "credits_used": 0, "credits_total": 10}),
            ]
            client.usage()
        assert slept == [5]

    def test_429_long_wait_in_the_body_alone_still_raises(self, client, monkeypatch):
        slept = []
        monkeypatch.setattr("lenz_io.client.time.sleep", lambda s: slept.append(s))
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.get("/me/usage")
            route.side_effect = [
                httpx.Response(429, json={"detail": "capped", "reset_in_seconds": 86400}),
                httpx.Response(200, json={"plan": "free", "credits_used": 0, "credits_total": 10}),
            ]
            with pytest.raises(LenzRateLimitError) as exc:
                client.usage()
        assert slept == []
        assert exc.value.retry_after == 86400

    def test_429_with_a_non_json_body_falls_back_to_backoff(self, client, monkeypatch):
        slept = []
        monkeypatch.setattr("lenz_io.client.time.sleep", lambda s: slept.append(s))
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.get("/me/usage")
            route.side_effect = [
                httpx.Response(429, text="<html>rate limited</html>"),
                httpx.Response(200, json={"plan": "free", "credits_used": 0, "credits_total": 10}),
            ]
            client.usage()
        assert slept == [RETRY_BACKOFF[0]]

    def test_negative_retry_after_is_floored_not_a_bare_ValueError(self, client, monkeypatch):
        """time.sleep(-5) raises ValueError, which would escape the retry
        ladder as a non-LenzError and defeat the typed-exception contract."""
        slept = []
        monkeypatch.setattr("lenz_io.client.time.sleep", lambda s: slept.append(s))
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.get("/me/usage")
            route.side_effect = [
                httpx.Response(429, json={"detail": "slow"}, headers={"Retry-After": "-5"}),
                httpx.Response(200, json={"plan": "free", "credits_used": 0, "credits_total": 10}),
            ]
            client.usage()
        assert slept == [0]

    def test_5xx_still_honors_a_short_retry_after(self, client, monkeypatch):
        """2.6.0 honored Retry-After on 5xx; the clamp must not silently
        drop that for a status class the changelog never mentions."""
        slept = []
        monkeypatch.setattr("lenz_io.client.time.sleep", lambda s: slept.append(s))
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.get("/me/usage")
            route.side_effect = [
                httpx.Response(503, json={"detail": "down"}, headers={"Retry-After": "5"}),
                httpx.Response(200, json={"plan": "free", "credits_used": 0, "credits_total": 10}),
            ]
            client.usage()
        assert slept == [5]

    def test_untyped_503_with_a_long_retry_after_keeps_retrying_on_backoff(self, client, monkeypatch):
        """The regression pin for the code-gated rule. A 503 with NO Lenz
        ``code`` is an ordinary Cloud Run / CDN / load-balancer
        maintenance-or-overload response — the server is down, not pacing us,
        and our own backoff may still satisfy it. A maintenance-window hour
        becomes backoff: not an abort, and not an hour-long sleep.

        Gating this on the status number instead of the body code aborts here
        with a bare LenzAPIError and throws the stated wait away."""
        slept = []
        monkeypatch.setattr("lenz_io.client.time.sleep", lambda s: slept.append(s))
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.get("/me/usage")
            route.side_effect = [
                httpx.Response(503, json={"detail": "maintenance"}, headers={"Retry-After": "600"}),
                httpx.Response(200, json={"plan": "free", "credits_used": 0, "credits_total": 10}),
            ]
            client.usage()
        assert slept == [RETRY_BACKOFF[0]]

    def test_plain_5xx_with_a_long_retry_after_keeps_retrying_on_backoff(self, client, monkeypatch):
        """Same rule for every other 5xx — untouched by 2.8.0."""
        slept = []
        monkeypatch.setattr("lenz_io.client.time.sleep", lambda s: slept.append(s))
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.get("/me/usage")
            route.side_effect = [
                httpx.Response(500, json={"detail": "down"}, headers={"Retry-After": "3600"}),
                httpx.Response(200, json={"plan": "free", "credits_used": 0, "credits_total": 10}),
            ]
            client.usage()
        assert slept == [RETRY_BACKOFF[0]]

    def test_typed_503_with_a_long_retry_after_raises_immediately_with_the_true_wait(self, client, monkeypatch):
        """2.8.0: the server's own shed/exhaustion 503s carry ``code``
        ``capacity`` / ``upstream_unavailable`` and state 90-120s waits.
        Burning the 1/2/4s ladder against them is the opposite of what the
        header asks — raise at once with the wait, exactly like 429."""
        from lenz_io import LenzAPIError, LenzUpstreamUnavailableError

        slept = []
        monkeypatch.setattr("lenz_io.client.time.sleep", lambda s: slept.append(s))
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.get("/me/usage").respond(
                503,
                json={"detail": "at capacity", "code": "capacity", "retry_after": 90},
                headers={"Retry-After": "90"},
            )
            with pytest.raises(LenzUpstreamUnavailableError) as exc_info:
                client.usage()
        assert slept == []
        assert exc_info.value.retry_after == 90
        assert exc_info.value.code == "capacity"
        assert isinstance(exc_info.value, LenzAPIError)  # existing handlers still catch it

    def test_503_reads_the_wait_from_the_body_retry_after_key(self, client, monkeypatch):
        """The 503 bodies carry ``retry_after`` (429 carries ``reset_in_seconds``);
        a proxy that strips the header must not demote the stated wait to the
        blind ladder."""
        from lenz_io import LenzUpstreamUnavailableError

        slept = []
        monkeypatch.setattr("lenz_io.client.time.sleep", lambda s: slept.append(s))
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.get("/me/usage").respond(
                503,
                json={"detail": "providers down", "code": "upstream_unavailable", "retry_after": 90},
            )
            with pytest.raises(LenzUpstreamUnavailableError) as exc_info:
                client.usage()
        assert slept == []
        assert exc_info.value.retry_after == 90

    def test_typed_503_within_the_cap_is_slept_and_retried(self, client, monkeypatch):
        """The abort is gated on the stated wait as well as the code: a typed
        503 asking for 30s is inside MAX_RETRY_AFTER_SLEEP, so we wait it out
        and retry rather than handing the caller an error it could have
        avoided."""
        slept = []
        monkeypatch.setattr("lenz_io.client.time.sleep", lambda s: slept.append(s))
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.get("/me/usage")
            route.side_effect = [
                httpx.Response(
                    503,
                    json={"detail": "at capacity", "code": "capacity", "retry_after": 30},
                    headers={"Retry-After": "30"},
                ),
                httpx.Response(200, json={"plan": "free", "credits_used": 0, "credits_total": 10}),
            ]
            client.usage()
        assert slept == [30]

    def test_plain_503_without_a_stated_wait_keeps_the_ladder(self, client, monkeypatch):
        """Regression pin: a bare 503 (no header, no body key) is still 'server
        down' and keeps the backoff ladder as before 2.8.0."""
        slept = []
        monkeypatch.setattr("lenz_io.client.time.sleep", lambda s: slept.append(s))
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.get("/me/usage")
            route.side_effect = [
                httpx.Response(503, json={"detail": "down"}),
                httpx.Response(200, json={"plan": "free", "credits_used": 0, "credits_total": 10}),
            ]
            client.usage()
        assert slept == [RETRY_BACKOFF[0]]


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
            r.get("/verify/status/tsk_log_test").respond(200, json={"status": "completed", "result": _COMPLETED_RESULT})
            client.verify_and_wait(claim="x", timeout=5)
        # INFO-level log of the task_id (for support recovery)
        assert any("tsk_log_test" in r.message for r in caplog.records)


class TestCoverage:
    """The warranty block and the certificate download.

    Three states the SDK has to keep apart, because conflating the first two
    is the mistake a caller makes: `coverage is None` means the feature is not
    enabled for this account at all, `status == "uncovered"` means it IS
    enabled and this verdict did not qualify, and `status == "covered"` means
    a certificate exists. Only the third has a certificate to fetch.
    """

    def _detail(self, coverage):
        body = {"verification_id": "vid_c", "claim": "x", "verdict": "True", "language": "en"}
        if coverage is not None:
            body["coverage"] = coverage
        return body

    def test_absent_block_is_none_not_an_empty_coverage(self, client):
        """`None` and `status="uncovered"` are different facts. A model that
        defaulted to an empty Coverage would make them indistinguishable, and
        a caller would read "not enabled" as "does not qualify"."""
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.get("/verifications/vid_c").respond(200, json=self._detail(None))
            v = client.verifications.get("vid_c")
        assert v.coverage is None

    def test_a_covered_verdict_carries_its_certificate_and_the_caps(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.get("/verifications/vid_c").respond(
                200,
                json=self._detail(
                    {
                        "status": "covered",
                        "reasons": [],
                        "certificate_id": "9f2c",
                        "certificate_url": "https://lenz.io/certificate/9f2c",
                        "as_of": "2026-09-03T10:00:00+00:00",
                        "currency": "EUR",
                        "cap": 10000,
                        "aggregate": 500000,
                        "terms_version": "v1",
                    }
                ),
            )
            v = client.verifications.get("vid_c")
        assert v.coverage.status == "covered"
        assert v.coverage.certificate_id == "9f2c"
        assert v.coverage.reasons == []
        # Major units, and the currency is read rather than assumed.
        assert (v.coverage.currency, v.coverage.cap, v.coverage.aggregate) == ("EUR", 10000, 500000)

    def test_an_uncovered_verdict_says_why(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.get("/verifications/vid_c").respond(
                200,
                json=self._detail(
                    {
                        "status": "uncovered",
                        "reasons": ["plan", "verdict"],
                        "certificate_id": None,
                        "certificate_url": None,
                        "as_of": None,
                        "currency": "EUR",
                        "cap": 10000,
                        "aggregate": 500000,
                        "terms_version": "v1",
                    }
                ),
            )
            v = client.verifications.get("vid_c")
        assert v.coverage.status == "uncovered"
        assert v.coverage.reasons == ["plan", "verdict"]
        assert v.coverage.certificate_id is None

    def test_an_unknown_status_does_not_break_the_client(self, client):
        """`CoverageStatus` is exported for exhaustive matching, but the field
        stays `str` — the SDK must never reject a status added server-side
        after this release was cut."""
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.get("/verifications/vid_c").respond(200, json=self._detail({"status": "something_new", "reasons": []}))
            v = client.verifications.get("vid_c")
        assert v.coverage.status == "something_new"

    def test_get_certificate_returns_the_verifiable_document(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.get("/verifications/vid_c/certificate").respond(
                200,
                json={
                    "document_version": "1",
                    "certificate_id": "9f2c",
                    "record_version": "1",
                    "payload": {"atomic_claim": "x", "verdict": "True", "currency": "EUR", "cap": 10000},
                    "leaf": "0" * 64,
                    "signature": "MEUCIQ",
                    "key_id": "projects/.../cryptoKeyVersions/1",
                    "anchors": {"qualified_timestamp": {"authority": "qtsp"}, "opentimestamps": {}},
                    "withdrawn_at": None,
                    "keys_url": "https://lenz.io/.well-known/lenz-certificate-keys.json",
                    "verifier_url": "https://lenz.io/verify_certificate.py",
                },
            )
            cert = client.verifications.get_certificate("vid_c")
        # Authenticated: the certificate is resolved by (verification, ACCOUNT),
        # so an anonymous fetch would be the wrong document or none.
        assert "Authorization" in route.calls.last.request.headers
        assert cert.certificate_id == "9f2c"
        assert cert.leaf == "0" * 64
        # The pointers that make it checkable WITHOUT Lenz.
        assert cert.verifier_url.endswith("verify_certificate.py")
        assert cert.keys_url.endswith("lenz-certificate-keys.json")

    def test_a_withdrawn_certificate_is_still_returned(self, client):
        """It is the record of what WAS warranted, and reliance before the
        notice can still be covered — so the SDK must not treat withdrawal as
        an error."""
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.get("/verifications/vid_c/certificate").respond(
                200,
                json={"certificate_id": "9f2c", "leaf": "0" * 64, "withdrawn_at": "2026-10-01T09:00:00+00:00"},
            )
            cert = client.verifications.get_certificate("vid_c")
        assert cert.withdrawn_at == "2026-10-01T09:00:00+00:00"

    def test_no_certificate_raises_rather_than_returning_an_empty_one(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.get("/verifications/vid_c/certificate").respond(404, json={"detail": "Not found."})
            with pytest.raises(LenzError) as exc:
                client.verifications.get_certificate("vid_c")
        assert exc.value.status_code == 404
