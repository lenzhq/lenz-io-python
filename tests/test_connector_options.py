"""What a multi-user caller (a connector holding one token per user) needs,
on both clients:

* ``with_options(api_key=...)``: a copy with another key on the same pool.
* ``legacy_aliases=False``: results read exactly what the API sent, with no
  2.x field computed by the SDK.
* Lone UTF-16 surrogates in request strings go out as U+FFFD.
* A 2xx whose body is not JSON raises a ``LenzError`` with the real status.
* ``LenzApiVersionError``'s message names the version and a hint.
"""

from __future__ import annotations

import asyncio
import json
import threading
import warnings
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from conftest import AnyClient, make_client

from lenz_io import (
    AsyncLenz,
    Lenz,
    LenzAPIError,
    LenzApiVersionError,
    LenzAuthError,
    LenzError,
    LenzInvalidResponseError,
)

pytestmark = pytest.mark.usefixtures("any_client")

BASE = "https://lenz.io/api/v1"
KEY = "lenz_test_abc123"
_USAGE = {"plan": "free", "credits": {"total": 100, "used": 40, "remaining": 60, "extra": 5}, "costs": {"verify": 10}}


@pytest.fixture()
def client() -> Iterator[Any]:
    with make_client(api_key=KEY) as c:
        yield c


def _auth(route: respx.Route) -> list[str | None]:
    return [c.request.headers.get("Authorization") for c in route.calls]


# ── 1. with_options(api_key=...) ──────────────────────────────────────────


class TestWithOptionsApiKey:
    def test_a_copy_sends_its_own_key(self, client: Any) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.get("/me/usage").respond(200, json=_USAGE)
            client.with_options(api_key="lat_user_token").usage()
            client.usage()
        assert _auth(route) == ["Bearer lat_user_token", f"Bearer {KEY}"]

    def test_an_omitted_key_keeps_the_parents(self, client: Any) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.get("/me/usage").respond(200, json=_USAGE)
            client.with_options(max_retries=0).usage()
            client.with_options(api_key="lat_a").with_options(timeout=5).usage()
        assert _auth(route) == [f"Bearer {KEY}", "Bearer lat_a"]

    @pytest.mark.parametrize("key", ["", "  ", None])
    def test_an_empty_key_is_no_key_and_never_reads_the_environment(
        self, client: Any, monkeypatch: pytest.MonkeyPatch, key: Any
    ) -> None:
        monkeypatch.setenv("LENZ_API_KEY", "lenz_env_key")
        copy = client.with_options(api_key=key)
        with respx.mock(base_url=BASE, assert_all_called=False) as r:
            usage = r.get("/me/usage").respond(200, json=_USAGE)
            library = r.get("/library").respond(200, json={"items": [], "total": 0, "page": 1, "page_size": 20})
            with pytest.raises(LenzAuthError):
                copy.usage()
            copy.library.list()
        assert usage.call_count == 0
        assert "Authorization" not in library.calls.last.request.headers

    def test_a_keyless_parent_never_reads_the_environment_through_a_copy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with make_client(api_key="") as parent:
            monkeypatch.setenv("LENZ_API_KEY", "lenz_env_key")
            with respx.mock(base_url=BASE, assert_all_called=False) as r:
                route = r.get("/me/usage").respond(200, json=_USAGE)
                with pytest.raises(LenzAuthError):
                    parent.with_options(max_retries=0).usage()
            assert route.call_count == 0

    @pytest.mark.parametrize("key", [123, b"lenz_x", ["k"]])
    def test_a_key_that_is_not_a_string_raises(self, client: Any, key: Any) -> None:
        with pytest.raises(ValueError, match="api_key"):
            client.with_options(api_key=key)

    def test_a_copy_never_closes_the_pool(self) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.get("/me/usage").respond(200, json=_USAGE)
            with make_client(api_key=KEY) as parent:
                with parent.with_options(api_key="lat_a"):
                    pass
                parent.usage()
        assert route.call_count == 1

    def test_any_key_string_is_accepted(self, client: Any) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.get("/me/usage").respond(200, json=_USAGE)
            client.with_options(api_key="whatever-123").usage()
        assert _auth(route) == ["Bearer whatever-123"]


@pytest.mark.sync_only
def test_two_copies_on_one_pool_send_their_own_keys_concurrently_sync() -> None:
    barrier = threading.Barrier(2, timeout=5)
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.params["who"], request.headers["Authorization"]))
        barrier.wait()  # both requests are in flight together
        return httpx.Response(200, json={"items": [], "total": 0, "page": 1, "page_size": 20})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http, Lenz(api_key=KEY, http_client=http) as parent:
        a, b = parent.with_options(api_key="lat_alice"), parent.with_options(api_key="lat_bob")

        def call(copy: Lenz, who: str) -> None:
            copy._request("GET", "/verifications", params={"who": who})

        threads = [threading.Thread(target=call, args=(a, "alice")), threading.Thread(target=call, args=(b, "bob"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert sorted(seen) == [("alice", "Bearer lat_alice"), ("bob", "Bearer lat_bob")]


@pytest.mark.sync_only
def test_two_copies_on_one_pool_send_their_own_keys_concurrently_async() -> None:
    seen: list[tuple[str, str]] = []

    async def run() -> None:
        both = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.url.params["who"], request.headers["Authorization"]))
            if len(seen) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 5)  # both requests are in flight together
            return httpx.Response(200, json={"items": [], "total": 0, "page": 1, "page_size": 20})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            async with AsyncLenz(api_key=KEY, http_client=http) as parent:
                a, b = parent.with_options(api_key="lat_alice"), parent.with_options(api_key="lat_bob")
                await asyncio.gather(
                    a._request("GET", "/verifications", params={"who": "alice"}),
                    b._request("GET", "/verifications", params={"who": "bob"}),
                )

    asyncio.run(run())
    assert sorted(seen) == [("alice", "Bearer lat_alice"), ("bob", "Bearer lat_bob")]


# ── 2. legacy_aliases=False ───────────────────────────────────────────────

_FAILED_ROW = {
    "claim": "A.",
    "language": "en",
    "status": "failed",
    "verdict": None,
    "confidence": None,
    "verification_url": None,
    "rationale": None,
    "dissent": None,
    "suggested_rewrite": None,
    "candidate_claims": [],
    "more_claims": [],
    "failure": {"code": "no_checkable_claim", "hint": "Send a statement of fact."},
}
_DONE_ROW = {
    "claim": "B.",
    "language": "en",
    "status": "completed",
    "verdict": "True",
    "confidence": "high",
    "verification_url": None,
    "rationale": "r",
    "dissent": None,
    "suggested_rewrite": None,
    "candidate_claims": [],
    "more_claims": ["C."],
    "failure": None,
}
_ASSESS = {
    "status": "ok",
    "claims": [_FAILED_ROW, _DONE_ROW],
    "more_claims": [],
    "candidate_claims": [],
    "failure": None,
}
_NEEDS_INPUT = {
    "status": "needs_input",
    "task_id": "t1",
    "reason": "multi_claim",
    "hint": "Pick one.",
    "claims": [{"claim": "One.", "domain": "x"}, {"claim": "Two.", "domain": "y"}],
}
_FAILED_STATUS = {
    "status": "failed",
    "task_id": "t1",
    "failure": {
        "code": "no_checkable_claim",
        "detail": "Nothing to check.",
        "failure_class": "invalid_input",
        "retryable": False,
        "hint": "Send a claim.",
        "docs_url": "https://lenz.io/docs/errors#invalid_input",
    },
}
_EXTRACT = {"status": "no_checkable_claim", "claims": [], "candidate_claims": [], "language": "en"}


class TestLegacyAliasesOff:
    def _client(self, **kw: Any) -> Any:
        return make_client(api_key=KEY, legacy_aliases=False, **kw)

    def test_assess_rows_read_as_sent(self) -> None:
        with respx.mock(base_url=BASE) as r, self._client() as c:
            r.post("/assess").respond(200, json=_ASSESS)
            out = c.assess(claims=["A.", "B."])
        failed, done = out.claims
        assert failed.verdict is None and failed.confidence is None
        assert failed.error_code is None and failed.hint is None and failed.identified_claims == []
        assert failed.status == "failed" and failed.failure is not None
        assert failed.failure.code == "no_checkable_claim"
        assert failed.failure.failure_reason == ""  # no 2.x spelling filled in
        assert done.verdict == "True" and done.hint is None and done.identified_claims == []
        assert done.more_claims == ["C."]
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            dumped = out.model_dump(mode="json")
        assert dumped["claims"][0]["verdict"] is None and dumped["claims"][0]["confidence"] is None

    def test_assess_rows_keep_the_aliases_by_default(self, client: Any) -> None:
        with respx.mock(base_url=BASE) as r:
            r.post("/assess").respond(200, json=_ASSESS)
            out = client.assess(claims=["A.", "B."])
        failed, done = out.claims
        assert failed.verdict == "Error" and failed.confidence == "low" and failed.error_code == "no_claim"
        assert failed.failure is not None and failed.failure.failure_reason == "no_claim"
        assert done.identified_claims == ["C."]

    def test_picker_options_carry_claim_without_text(self) -> None:
        with respx.mock(base_url=BASE) as r, self._client() as c:
            r.get("/verify/status/t1").respond(200, json=_NEEDS_INPUT)
            status = c.get_status("t1")
        assert [o.claim for o in status.claims] == ["One.", "Two."]
        assert [o.text for o in status.claims] == ["", ""]

    def test_a_failed_status_fills_no_flat_fields(self) -> None:
        with respx.mock(base_url=BASE) as r, self._client() as c:
            r.get("/verify/status/t1").respond(200, json=_FAILED_STATUS)
            status = c.get_status("t1")
        assert (status.error, status.failure_reason, status.failure_class, status.docs_url, status.hint) == (
            "",
            "",
            "",
            "",
            "",
        )
        assert status.retryable is None
        assert status.failure is not None and status.failure.code == "no_checkable_claim"
        assert status.failure.failure_reason == ""

    def test_usage_blocks_stay_as_sent(self) -> None:
        with respx.mock(base_url=BASE) as r, self._client() as c:
            r.get("/me/usage").respond(200, json=_USAGE)
            u = c.usage()
        assert u.verify is None and u.ask is None and u.assess is None
        assert u.quota_resets_at is None
        assert u.credits.extra == 5
        with pytest.warns(DeprecationWarning):
            assert u.credits.bonus is None

    def test_usage_blocks_are_derived_by_default(self, client: Any) -> None:
        with respx.mock(base_url=BASE) as r:
            r.get("/me/usage").respond(200, json=_USAGE)
            u = client.usage()
        assert u.verify is not None and u.verify.remaining == 6

    def test_extract_status_reads_as_sent(self) -> None:
        with respx.mock(base_url=BASE) as r, self._client() as c:
            r.post("/extract").respond(200, json=_EXTRACT)
            out = c.extract(text="Hello.")
        assert out.status == "no_checkable_claim"
        assert out.claim == "" and out.identified_claims == [] and out.locations is None

    def test_verify_receipt_and_verification(self) -> None:
        verification = {
            "verification_id": "v1",
            "claim": "A.",
            "created_at": "2026-01-01T10:00:00Z",
            "completed_at": "2026-01-02T10:00:00Z",
        }
        with respx.mock(base_url=BASE) as r, self._client() as c:
            r.post("/verify").respond(202, json={"task_id": "t1", "claim": "A."})
            r.get("/verifications/v1").respond(200, json=verification)
            receipt = c.verify("A.")
            v = c.verifications.get("v1")
        assert receipt.claim == "A." and receipt.claim_text == ""
        assert v.completed_at == "2026-01-02T10:00:00Z" and v.modified_at is None

    def test_copies_carry_the_setting(self, client: Any) -> None:
        with respx.mock(base_url=BASE) as r:
            r.post("/assess").respond(200, json=_ASSESS)
            with self._client() as off:
                assert off.with_options(max_retries=0).assess(claims=["A.", "B."]).claims[0].verdict is None
                assert off.with_options(api_key="lat_a").assess(claims=["A.", "B."]).claims[0].verdict is None
            assert client.with_options(api_key="lat_a").assess(claims=["A.", "B."]).claims[0].verdict == "Error"

    @pytest.mark.parametrize("value", [None, 0, "no"])
    def test_a_setting_that_is_not_a_bool_raises(self, value: Any) -> None:
        with pytest.raises(ValueError, match="legacy_aliases"):
            make_client(api_key=KEY, legacy_aliases=value)

    def test_a_copy_cannot_change_it(self, client: Any) -> None:
        with pytest.raises(TypeError, match="legacy_aliases"):
            client.with_options(legacy_aliases=False)


# ── 3. Lone surrogates ────────────────────────────────────────────────────


class TestLoneSurrogates:
    def test_a_lone_surrogate_goes_out_as_the_replacement_character(self, client: Any) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.post("/assess").respond(200, json={"status": "ok", "claims": [], "more_claims": []})
            client.assess("A\ud800B \udfff.", idempotency_key="k")
        body = route.calls.last.request.content
        assert body == '{"text":"A�B �."}'.encode()
        assert b"\xef\xbf\xbd" in body

    def test_a_surrogate_pair_is_one_character(self, client: Any) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.post("/assess").respond(200, json={"status": "ok", "claims": [], "more_claims": []})
            client.assess("x😀y \ud83d")
        assert route.calls.last.request.content == '{"text":"x\U0001f600y �"}'.encode()

    def test_nested_values_keys_and_query_params(self, client: Any) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.post("/assess").respond(200, json={"status": "ok", "claims": [], "more_claims": []})
            lib = r.get("/library").respond(200, json={"items": [], "total": 0, "page": 1, "page_size": 20})
            client.assess(claims=["ok", "bad\udc00"])
            client.library.list(search="q\ud800")
        assert json.loads(route.calls.last.request.content)["claims"] == ["ok", "bad�"]
        assert lib.calls.last.request.url.params["search"] == "q�"

    def test_a_clean_body_is_unchanged(self, client: Any) -> None:
        with respx.mock(base_url=BASE) as r:
            route = r.post("/assess").respond(200, json={"status": "ok", "claims": [], "more_claims": []})
            client.assess("Ünïcödé 😀 claim.")
        assert route.calls.last.request.content == '{"text":"Ünïcödé 😀 claim."}'.encode()


# ── 4. A 2xx that is not JSON ─────────────────────────────────────────────


class TestUnreadableSuccess:
    def test_raises_a_lenz_error_with_the_status(self, client: Any) -> None:
        with respx.mock(base_url=BASE) as r:
            r.get("/me/usage").respond(200, content=b"<html>proxy page</html>", headers={"X-Request-ID": "req-9"})
            with pytest.raises(LenzInvalidResponseError) as ei:
                client.usage()
        exc = ei.value
        assert isinstance(exc, LenzAPIError) and isinstance(exc, LenzError)
        assert isinstance(exc, json.JSONDecodeError)  # what 3.1 raised: still caught
        assert exc.status_code == 200
        assert exc.request_id == "req-9"
        assert exc.body_text == "<html>proxy page</html>"
        assert exc.body is None
        assert "200" in exc.message and "GET /me/usage" in exc.message

    def test_the_body_text_is_truncated(self, client: Any) -> None:
        with respx.mock(base_url=BASE) as r:
            r.get("/me/usage").respond(201, content=b"x" * 5000)
            with pytest.raises(LenzInvalidResponseError) as ei:
                client.usage()
        assert ei.value.status_code == 201
        assert ei.value.body_text == "x" * 1000 + "\u2026"

    def test_bytes_that_are_not_utf8(self, client: Any) -> None:
        with respx.mock(base_url=BASE) as r:
            r.get("/me/usage").respond(200, content=b"\xff\xfe\x00garbage")
            with pytest.raises(LenzInvalidResponseError) as ei:
                client.usage()
        assert ei.value.status_code == 200

    @pytest.mark.parametrize("status", [200, 201, 204, 205])
    def test_an_empty_2xx_raises_whatever_its_status_or_length(self, client: Any, status: int) -> None:
        """Since 3.2: no endpoint answers with an empty body (3.1 read a 204,
        a 205 or ``Content-Length: 0`` as ``{}``)."""
        with respx.mock(base_url=BASE) as r:
            r.get("/me/usage").respond(status, content=b"", headers={"Content-Length": "0"})
            with pytest.raises(LenzInvalidResponseError) as ei:
                client.usage()
            assert ei.value.status_code == status and "empty body" in ei.value.message
            r.get("/me/usage").mock(return_value=httpx.Response(status, stream=httpx.ByteStream(b"")))
            with pytest.raises(LenzInvalidResponseError):
                client.usage()

    @pytest.mark.parametrize("status", [301, 302, 303, 304, 307, 308])
    @pytest.mark.parametrize("content", [b"", b'{"ok": true}', b"<html>moved</html>"])
    def test_a_redirect_raises_whatever_its_body(self, client: Any, status: int, content: bytes) -> None:
        with respx.mock(base_url=BASE) as r:
            r.get("/me/usage").respond(status, content=content, headers={"Location": "https://e.x/"})
            with pytest.raises(LenzInvalidResponseError) as ei:
                client.usage()
        assert ei.value.status_code == status and "redirect" in ei.value.message

    @pytest.mark.parametrize("content", [b"  \n", b""])
    def test_whitespace_or_a_chunked_empty_body_raises(self, client: Any, content: bytes) -> None:
        with respx.mock(base_url=BASE) as r:
            r.get("/me/usage").mock(return_value=httpx.Response(200, stream=httpx.ByteStream(content)))
            with pytest.raises(LenzInvalidResponseError) as ei:
                client.usage()
        assert ei.value.status_code == 200

    def test_the_cut_never_leaves_half_a_character(self, client: Any) -> None:
        with respx.mock(base_url=BASE) as r:
            r.get("/me/usage").respond(200, content=("x" * 999 + "\U0001f600" * 5).encode())
            with pytest.raises(LenzInvalidResponseError) as ei:
                client.usage()
        assert ei.value.body_text == "x" * 999 + "\U0001f600\u2026"
        ei.value.body_text.encode("utf-8")  # no lone surrogate


# ── 5. The version error's message ────────────────────────────────────────


def test_the_version_error_names_the_version_and_a_hint(client: Any) -> None:
    with respx.mock(base_url=BASE) as r:
        r.get("/me/usage").respond(200, json=_USAGE, headers={"X-Lenz-API-Version": "2026-05-13"})
        with pytest.raises(LenzApiVersionError) as ei:
            client.usage()
    assert ei.value.message == "The API answered 2026-05-13; this SDK reads 2026-10-11 only."
    assert ei.value.api_version == "2026-05-13" and ei.value.status_code == 200


def test_the_any_client_fixture_is_live() -> None:
    assert AnyClient.current is not None


class TestLegacyAliasesOffOnAReview:
    def test_get_review_reads_its_rows_as_sent(self) -> None:
        from pathlib import Path

        fixture = Path(__file__).parent / "fixtures/parity/canonical" / "review__get_assessment_rows_full_fields.json"
        recorded = json.loads(fixture.read_text())
        body = recorded["body"]
        rid = body["review_id"]
        with respx.mock(base_url=BASE) as r:
            r.get(f"/reviews/{rid}").respond(200, json=body)
            with make_client(api_key=KEY, legacy_aliases=False) as off:
                review_off = off.get_review(rid)
            with make_client(api_key=KEY) as on:
                review_on = on.get_review(rid)
        rows_off = [c.assessment for c in review_off.claims if c.assessment is not None]
        rows_on = [c.assessment for c in review_on.claims if c.assessment is not None]
        assert any(a.identified_claims for a in rows_on)
        assert all(a.identified_claims == [] and a.error_code is None and a.hint is None for a in rows_off)
        assert [a.more_claims for a in rows_off] == [a.more_claims for a in rows_on]
        assert review_off.summary is not None and review_off.summary.claim_limit_reached is None


_ERROR_ATTRS = ("message", "cause", "fix", "failure_reason", "failure_class", "retryable", "hint", "status_code")


def _error_of(legacy: bool, body: dict[str, Any]) -> LenzError:
    from lenz_io import LenzNeedsInputError, LenzPipelineError

    with respx.mock(base_url=BASE) as r, make_client(api_key=KEY, legacy_aliases=legacy) as c:
        r.get("/verify/status/t1").respond(200, json=body)
        with pytest.raises((LenzPipelineError, LenzNeedsInputError)) as ei:
            c.wait("t1")
    return ei.value


class TestErrorsAreTheSameEitherWay:
    def test_a_failed_wait(self) -> None:
        on, off = _error_of(True, _FAILED_STATUS), _error_of(False, _FAILED_STATUS)
        assert type(on) is type(off)
        assert {a: getattr(off, a, None) for a in _ERROR_ATTRS} == {a: getattr(on, a, None) for a in _ERROR_ATTRS}
        assert off.failure_class == "invalid_input" and off.retryable is False  # type: ignore[attr-defined]

    def test_an_upstream_failure_keeps_its_retry_signal(self) -> None:
        body = {**_FAILED_STATUS, "failure": {**_FAILED_STATUS["failure"], "code": "research_empty"}}
        body["failure"]["failure_class"], body["failure"]["retryable"] = "upstream_unavailable", True
        off = _error_of(False, body)
        assert off.failure_class == "upstream_unavailable" and off.retryable is True  # type: ignore[attr-defined]
        assert off.message == _error_of(True, body).message

    def test_a_cancelled_wait(self) -> None:
        body = {"status": "cancelled", "task_id": "t1"}
        on, off = _error_of(True, body), _error_of(False, body)
        assert {a: getattr(off, a, None) for a in _ERROR_ATTRS} == {a: getattr(on, a, None) for a in _ERROR_ATTRS}

    def test_a_needs_input_pause(self) -> None:
        on, off = _error_of(True, _NEEDS_INPUT), _error_of(False, _NEEDS_INPUT)
        assert off.payload == on.payload  # type: ignore[attr-defined]
        assert off.hint == on.hint == "Pick one."  # type: ignore[attr-defined]


def test_batch_results_keep_how_their_statuses_were_read() -> None:
    with respx.mock(base_url=BASE) as r, make_client(api_key=KEY, legacy_aliases=False) as c:
        r.post("/verify/batch").respond(202, json={"items": [{"task_id": "t1", "claim": "A."}], "count": 1})
        r.get("/verify/status/t1").respond(200, json={"status": "cancelled", "task_id": "t1"})
        results = c.verify_batch_and_wait(claims=[{"claim": "A."}])
    (item,) = results
    assert item.claim == "A."
    assert item.status_detail is not None
    assert item.status_detail.failure is None  # not the 2.x block: none was sent
    assert item.status_detail.error == ""


class TestReviewFollowUps:
    def test_a_namedtuple_in_a_body_goes_out_as_an_array(self, client: Any) -> None:
        from typing import NamedTuple

        class Two(NamedTuple):
            a: str
            b: str

        with respx.mock(base_url=BASE) as r:
            route = r.post("/assess").respond(200, json={"status": "ok", "claims": [], "more_claims": []})
            client.assess(claims=Two("One.", "Two\ud800."))
        assert json.loads(route.calls.last.request.content)["claims"] == ["One.", "Two�."]

    def test_a_lone_surrogate_in_a_path_id_is_an_invalid_id(self, client: Any) -> None:
        with pytest.raises(ValueError):
            client.get_status("a\ud800")

    def test_unsent_usage_aliases_stay_unset(self) -> None:
        with respx.mock(base_url=BASE) as r, make_client(api_key=KEY, legacy_aliases=False) as c:
            r.get("/me/usage").respond(200, json=_USAGE)
            u = c.usage()
        dumped = u.model_dump(exclude_unset=True)
        assert set(dumped) == set(_USAGE)
        assert "bonus" not in dumped["credits"]
        assert u.verify is None
