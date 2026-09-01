"""`text` is a document, `claim` is a claim — and nothing on the wire changes.

`assess`, `verify`, batch items and `select` take `claim` / `claims` from
2.10.0; the older spellings stay as aliases. Every test here pins one of two
things: the new name reaches the same wire key the server has always
accepted, or an existing call's request body is byte-identical to 2.9.x.
"""

from __future__ import annotations

import json

import pytest
import respx

DEFAULT_BASE = "https://lenz.io/api/v1"
CLAIM = "The Danube flows through more countries than any other river."


def _body(route):
    return json.loads(route.calls.last.request.content)


class TestAssess:
    def test_claim_is_positional_and_sends_text(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.post("/assess").respond(200, json={"claims": []})
            client.assess(CLAIM)
        assert _body(route) == {"text": CLAIM}

    def test_text_kwarg_still_works_byte_identically(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.post("/assess").respond(200, json={"claims": []})
            client.assess(text=CLAIM, language="es")
        assert _body(route) == {"text": CLAIM, "language": "es"}

    def test_claim_wins_when_both_are_given(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.post("/assess").respond(200, json={"claims": []})
            client.assess(claim=CLAIM, text="ignored")
        assert _body(route) == {"text": CLAIM}


class TestVerify:
    def test_text_kwarg_is_an_alias(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.post("/verify").respond(202, json={"task_id": "tsk_1", "status": "queued"})
            client.verify(text=CLAIM)
        assert _body(route)["text"] == CLAIM

    def test_positional_claim_is_unchanged(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.post("/verify").respond(202, json={"task_id": "tsk_1", "status": "queued"})
            client.verify(CLAIM)
        assert _body(route) == {"text": CLAIM, "source_url": "", "webhook_url": ""}


class TestVerifyBatch:
    def test_item_claim_is_sent_as_text(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.post("/verify/batch").respond(
                202, json={"batch_id": "bat_1", "items": [{"task_id": "tsk_1", "claim_text": CLAIM}]}
            )
            client.verify_batch(claims=[{"claim": CLAIM, "language": "es"}])
        assert _body(route)["claims"] == [{"language": "es", "text": CLAIM}]

    def test_legacy_items_are_forwarded_verbatim(self, client):
        item = {"text": CLAIM, "source_url": "https://example.com", "depth": "low"}
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.post("/verify/batch").respond(
                202, json={"batch_id": "bat_1", "items": [{"task_id": "tsk_1", "claim_text": CLAIM}]}
            )
            client.verify_batch(claims=[item])
        assert _body(route)["claims"] == [item]


class TestSelect:
    def test_claims_kwarg_sends_texts(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.post("/verify/tsk_1/select").respond(
                200, json={"batch_id": "bat_1", "items": [{"task_id": "tsk_2", "claim_text": CLAIM}]}
            )
            client.select("tsk_1", claims=[CLAIM])
        assert _body(route) == {"texts": [CLAIM]}

    def test_texts_kwarg_still_works(self, client):
        with respx.mock(base_url=DEFAULT_BASE) as r:
            route = r.post("/verify/tsk_1/select").respond(
                200, json={"batch_id": "bat_1", "items": [{"task_id": "tsk_2", "claim_text": CLAIM}]}
            )
            client.select("tsk_1", texts=[CLAIM])
        assert _body(route) == {"texts": [CLAIM]}

    def test_neither_raises(self, client):
        with pytest.raises(ValueError, match="claims"):
            client.select("tsk_1")
