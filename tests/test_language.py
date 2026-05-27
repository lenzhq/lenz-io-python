"""Multi-language SDK behavior — request-body wire format, response
parsing, error surfacing, and the byte-identical English regression.

Six endpoint methods gain an optional ``language`` kwarg:
``verify``, ``verify_and_wait``, ``verify_batch``, ``assess``, ``extract``,
``ask.send``. The convention (see ``lenz_io.client`` module docstring):

* ``language=''`` (default) MUST omit the field from the request body.
  This is the CRITICAL regression invariant: every existing English
  caller's wire format must stay byte-identical after this change.
* ``language='es' | 'de' | …`` (any of 12 supported codes) MUST send
  ``"language": "<code>"`` in the JSON body.
* Response models populate ``.language`` from the server's echoed
  field, defaulting to ``'en'`` for legacy payloads that omit it.

Server validation lives in the main repo; the SDK just round-trips
the field. We mock 422s to confirm typed errors surface cleanly.
"""

from __future__ import annotations

import json

import pytest
import respx

DEFAULT_BASE = "https://lenz.io/api/v1"


# ─────────────────────────────────────────────────── REGRESSION ──
# IRON RULE per the SDK plan: omit-language MUST produce a request
# body with NO ``language`` key. Without this guarantee, every
# existing English customer starts sending ``"language": ""`` (or
# something else) on the wire — silent breaking change.


class TestOmitLanguageWireFormatRegression:
    """CRITICAL — every existing English caller's request body must
    stay byte-identical to before the ``language`` parameter existed.
    """

    def _body(self, route) -> dict:
        return json.loads(route.calls.last.request.content)

    def test_verify_no_language_key_when_omitted(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.post("/verify").respond(200, json={"task_id": "t", "claim_text": "x"})
            client.verify(claim="The earth is flat")
        assert "language" not in self._body(route)

    def test_verify_and_wait_no_language_key_when_omitted(self, client):
        # verify_and_wait wraps _verify_submit; same wire invariant.
        with respx.mock(base_url=DEFAULT_BASE) as r:
            submit = r.post("/verify").respond(200, json={"task_id": "t1", "claim_text": "x"})
            r.get("/verify/status/t1").respond(
                200,
                json={
                    "status": "completed",
                    "result": {
                        "verification_id": "v1",
                        "claim": "x",
                        "verdict": "True",
                        "confidence": "high",
                    },
                },
            )
            client.verify_and_wait(claim="x", timeout=5.0)
        assert "language" not in self._body(submit)

    def test_verify_batch_no_language_key_when_omitted(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.post("/verify/batch").respond(200, json={"batch_id": "b", "items": []})
            client.verify_batch(claims=[{"text": "a"}, {"text": "b"}])
        assert "language" not in self._body(route)

    def test_assess_no_language_key_when_omitted(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.post("/assess").respond(200, json={"claims": [], "error": None})
            client.assess(text="x")
        assert "language" not in self._body(route)

    def test_extract_no_language_key_when_omitted(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.post("/extract").respond(
                200,
                json={"status": "ready", "claim": "x", "identified_claims": ["x"], "domain": "Science"},
            )
            client.extract(text="The earth is flat")
        assert "language" not in self._body(route)

    def test_ask_send_no_language_key_when_omitted(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.post("/ask/v1").respond(200, json={"reply": "ok"})
            client.ask.send("v1", message="why?")
        assert "language" not in self._body(route)


# ─────────────────────────────────────────────────── HAPPY PATH ──
# Explicit ``language='es'`` MUST send ``"language": "es"`` on the wire.


class TestExplicitLanguageWireFormat:
    def _body(self, route) -> dict:
        return json.loads(route.calls.last.request.content)

    def test_verify_sends_language(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.post("/verify").respond(200, json={"task_id": "t", "claim_text": "x"})
            client.verify(claim="x", language="es")
        assert self._body(route)["language"] == "es"

    def test_verify_and_wait_sends_language(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            submit = r.post("/verify").respond(200, json={"task_id": "t1", "claim_text": "x"})
            r.get("/verify/status/t1").respond(
                200,
                json={
                    "status": "completed",
                    "result": {
                        "verification_id": "v1",
                        "claim": "La Tierra es plana",
                        "verdict": "False",
                        "confidence": "high",
                        "language": "es",
                    },
                },
            )
            v = client.verify_and_wait(claim="x", language="es", timeout=5.0)
        assert self._body(submit)["language"] == "es"
        assert v.language == "es"

    def test_verify_batch_batchwide_language(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.post("/verify/batch").respond(200, json={"batch_id": "b", "items": []})
            client.verify_batch(claims=[{"text": "a"}, {"text": "b"}], language="de")
        assert self._body(route)["language"] == "de"

    def test_verify_batch_per_item_language(self, client):
        # Plain dict per-item — TypedDict is type-only.
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.post("/verify/batch").respond(200, json={"batch_id": "b", "items": []})
            client.verify_batch(
                claims=[{"text": "a", "language": "es"}, {"text": "b", "language": "de"}],
            )
        body = self._body(route)
        # Batch-wide language not set; per-item carries the override.
        assert "language" not in body
        assert body["claims"] == [
            {"text": "a", "language": "es"},
            {"text": "b", "language": "de"},
        ]

    def test_verify_batch_mixed_default_and_per_item(self, client):
        # Batch default 'es', per-item override 'de' — both reach the server.
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.post("/verify/batch").respond(200, json={"batch_id": "b", "items": []})
            client.verify_batch(
                claims=[{"text": "a"}, {"text": "b", "language": "de"}],
                language="es",
            )
        body = self._body(route)
        assert body["language"] == "es"
        assert body["claims"][1]["language"] == "de"

    def test_assess_sends_language(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.post("/assess").respond(200, json={"claims": [], "error": None})
            client.assess(text="x", language="es")
        assert self._body(route)["language"] == "es"

    def test_extract_sends_language(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.post("/extract").respond(
                200,
                json={"status": "ready", "claim": "x", "identified_claims": ["x"], "domain": "Science"},
            )
            client.extract(text="x", language="es")
        assert self._body(route)["language"] == "es"

    def test_ask_send_sends_language(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.post("/ask/v1").respond(200, json={"reply": "ok"})
            client.ask.send("v1", message="why?", language="en")
        assert self._body(route)["language"] == "en"


# ─────────────────────────────────────────────────── ERROR PATH ──
# Server returns 422 on invalid codes; SDK must raise a typed
# LenzAPIError without losing the original status / message.


class TestInvalidLanguageCode:
    def test_assess_422_raises_typed_error(self, client):
        from lenz_io import LenzError

        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.post("/assess").respond(
                422,
                json={"detail": "Unsupported language code 'xx'. Supported: en, es, de, fr, …"},
            )
            with pytest.raises(LenzError) as ei:
                client.assess(text="x", language="xx")
        assert ei.value.status_code == 422
        assert "xx" in str(ei.value)


# ─────────────────────────────────────────────────── RESPONSE PARSING ──
# Server-side schemas declare `language` as REQUIRED, but the SDK
# tolerates legacy / mocked payloads that omit it by defaulting
# to 'en'. Both shapes must round-trip cleanly.


class TestResponseModelLanguageParsing:
    def test_verification_parses_explicit_language(self, client):
        # Round-trip via the verifications.get path so we exercise the
        # full Verification deserialization.
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.get("/verifications/v1").respond(
                200,
                json={
                    "verification_id": "v1",
                    "claim": "La Tierra es plana",
                    "verdict": "False",
                    "confidence": "high",
                    "language": "es",
                },
            )
            v = client.verifications.get("v1")
        assert v.language == "es"
        assert v.verdict == "False"  # enum stays English

    def test_verification_falls_back_to_en_when_field_missing(self, client):
        # Legacy / mocked payload — server may omit language; SDK
        # defaults to 'en' rather than blowing up.
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.get("/verifications/v1").respond(
                200,
                json={"verification_id": "v1", "claim": "x", "verdict": "True", "confidence": "high"},
            )
            v = client.verifications.get("v1")
        assert v.language == "en"

    def test_verification_list_item_parses_language(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.get("/verifications").respond(
                200,
                json={
                    "items": [
                        {"verification_id": "v1", "claim": "a", "verdict": "True", "language": "fr"},
                    ],
                    "total": 1,
                    "page": 1,
                    "page_size": 20,
                },
            )
            page = client.verifications.list()
        assert page.items[0].language == "fr"

    def test_assess_claim_parses_language(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.post("/assess").respond(
                200,
                json={
                    "claims": [
                        {"claim": "x", "verdict": "False", "confidence": "high", "language": "de"},
                    ],
                    "error": None,
                },
            )
            out = client.assess(text="x", language="de")
        assert out.claims[0].language == "de"
        assert out.claims[0].verdict == "False"

    def test_assess_claim_defaults_when_language_missing(self, client):
        # Legacy payload — server omits the field; SDK defaults to 'en'.
        with respx.mock(base_url=DEFAULT_BASE) as r:
            r.post("/assess").respond(
                200,
                json={
                    "claims": [{"claim": "x", "verdict": "True", "confidence": "high"}],
                    "error": None,
                },
            )
            out = client.assess(text="x")
        assert out.claims[0].language == "en"


# ─────────────────────────────────────────────────── TYPE-LEVEL ──
# The TypedDict for batch items is purely type-hint sugar; at runtime
# we accept plain dicts. Nothing to assert at runtime beyond "the
# import succeeds and the type is a TypedDict".


class TestVerifyBatchItemTypedDict:
    def test_typed_dict_import_and_total_false(self):
        from lenz_io.client import VerifyBatchItem

        # TypedDicts expose __total__ + __optional_keys__ (Python 3.11+).
        # total=False means every key is optional — callers may pass any
        # subset.
        assert VerifyBatchItem.__total__ is False

    def test_top_level_export(self):
        # REGRESSION: 1.0.0 published with VerifyBatchItem reachable only
        # via ``from lenz_io.client import VerifyBatchItem`` — the README
        # and example files used the top-level path, so customers
        # following the docs hit ImportError. 1.0.1 added it to
        # ``lenz_io.__all__``; this test pins it so a future re-export
        # rename doesn't silently regress.
        import lenz_io

        assert hasattr(lenz_io, "VerifyBatchItem"), (
            "lenz_io.VerifyBatchItem must be importable at the top level; see lenz_io/__init__.py"
        )
        assert "VerifyBatchItem" in lenz_io.__all__, (
            "VerifyBatchItem must be listed in lenz_io.__all__ so `from lenz_io import *` brings it in."
        )
        # The top-level symbol must be the same TypedDict object the
        # submodule exports — no shadowing.
        from lenz_io.client import VerifyBatchItem as ClientVbi

        assert lenz_io.VerifyBatchItem is ClientVbi
