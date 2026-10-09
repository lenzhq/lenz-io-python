"""Edge inputs found in the final 3.0 reviews: an empty webhook secret, a
timeout too large for a socket, an infinite stated wait, the 2.x wording of
a blank claim, and a cancel's 422 keeping the server's code."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from lenz_io import Lenz, LenzRateLimitError, LenzWebhookSignatureError, verify_signature
from lenz_io.client import DEFAULT_BASE_URL
from lenz_io.errors import map_response_to_error
from lenz_io.webhooks import _sign

KEY = "lenz_" + "0" * 32


# ── 1. a webhook secret is never empty ─────────────────────────────────────


@pytest.mark.parametrize("secret", ["", None])
def test_verify_signature_refuses_an_empty_secret(secret: Any) -> None:
    body = b'{"event": "verification.completed"}'
    with pytest.raises(ValueError, match="non-empty secret"):
        verify_signature(body, _sign(body, ""), secret)


def test_a_real_secret_still_verifies() -> None:
    body = b"{}"
    assert verify_signature(body, _sign(body, "s3cret"), "s3cret") is True
    with pytest.raises(LenzWebhookSignatureError):
        verify_signature(body, _sign(body, "other"), "s3cret")


# ── 5. a timeout a socket can take ─────────────────────────────────────────

_TOO_LONG = [1e10, 2_147_484, (5, 1e10, 5, 5)]


@pytest.mark.parametrize("bad", _TOO_LONG)
def test_a_timeout_past_the_limit_is_refused_everywhere(bad: Any) -> None:
    for call in (
        lambda: Lenz(api_key=KEY, timeout=bad),
        lambda: Lenz(api_key=KEY).with_options(timeout=bad),
        lambda: Lenz(api_key=KEY).usage(timeout=bad),
        lambda: Lenz(api_key=KEY).extract(text="Doc.", timeout=bad),
    ):
        with pytest.raises(ValueError, match="2,147,483"):
            call()


def test_an_httpx_timeout_past_the_limit_is_refused() -> None:
    with pytest.raises(ValueError, match="2,147,483"):
        Lenz(api_key=KEY, timeout=httpx.Timeout(5, read=1e10))


def test_the_limit_itself_is_accepted() -> None:
    Lenz(api_key=KEY, timeout=2_147_483).with_options(timeout=(1, 2_147_483, None, 1)).close()


# ── 6. an infinite stated wait ─────────────────────────────────────────────


@pytest.mark.parametrize("header", ["1e999", "inf", "-inf", "-1e999", "nan"])
def test_a_non_finite_retry_after_is_no_stated_wait(header: str, monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr("lenz_io.client.time.sleep", slept.append)
    with respx.mock(base_url=DEFAULT_BASE_URL) as r:
        route = r.get("/me/usage").respond(
            429, json={"code": "rate_limited", "detail": "slow"}, headers={"Retry-After": header}
        )
        with pytest.raises(LenzRateLimitError):
            Lenz(api_key=KEY).usage()
    # As in Node: no stated wait, so the normal backoff ladder runs.
    assert route.call_count == 4
    assert slept == [1.0, 2.0, 4.0]


def test_a_huge_finite_retry_after_is_clamped_and_raises_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("lenz_io.client.time.sleep", lambda s: None)
    with respx.mock(base_url=DEFAULT_BASE_URL) as r:
        route = r.get("/me/usage").respond(
            429, json={"code": "rate_limited", "detail": "slow"}, headers={"Retry-After": "1e300"}
        )
        with pytest.raises(LenzRateLimitError):
            Lenz(api_key=KEY).usage()
    assert route.call_count == 1


def test_an_infinite_retry_after_in_an_error_body_reads_as_unknown() -> None:
    err = map_response_to_error(
        503, json.dumps({"code": "capacity", "detail": "busy", "retry_after": "1e999"}).encode(), {}
    )
    assert err.retry_after is None


# ── 7. a blank claim reads as 2.x worded it ────────────────────────────────


def _blank(loc: list[Any], detail: str) -> bytes:
    return json.dumps(
        {"detail": detail, "code": "blank_input", "errors": [{"loc": loc, "msg": detail, "type": "blank_input"}]}
    ).encode()


@pytest.mark.parametrize(
    ("method", "path", "loc", "expected"),
    [
        ("POST", "/verify", ["body", "claim"], "Text is required."),
        ("POST", "/assess", ["body", "claim"], "Text is required."),
        ("POST", "/verify/batch", ["body", "claims", 1, "claim"], "claims[1].text is required."),
        ("POST", "/verify/t1/select", ["body", "claims"], "texts is required and must be non-empty."),
    ],
)
def test_a_blank_claim_keeps_the_2x_sentence(method: str, path: str, loc: list[Any], expected: str) -> None:
    err = map_response_to_error(422, _blank(loc, "claim is required."), {}, endpoint=(method, path))
    assert err.message == expected


def test_another_blank_field_keeps_the_servers_sentence() -> None:
    err = map_response_to_error(422, _blank(["body", "text"], "text is required."), {}, endpoint=("POST", "/extract"))
    assert err.message == "text is required."


# ── 11. a cancel's 422 keeps the server's code ─────────────────────────────


@pytest.mark.parametrize("path", ["/verify/t1/cancel", "/reviews/r1/cancel", "/citechecks/c1/cancel"])
def test_a_cancel_schema_422_keeps_its_code_and_errors(path: str) -> None:
    errors = [{"type": "missing", "loc": ["body", "x"], "msg": "Field required"}]
    body = {"detail": "Field required", "code": "validation_error", "errors": errors}
    err = map_response_to_error(422, json.dumps(body).encode(), {}, endpoint=("POST", path))
    assert err.code == "validation_error"
    assert err.message == "Field required"
