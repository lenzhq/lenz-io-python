"""Pin the table-driven HTTP status -> exception mapping.

The TS SDK MUST mirror the same mapping. Any change to this table is a
cross-language invariant change.
"""

from __future__ import annotations

import json

import pytest

from lenz_io.errors import (
    LenzAPIError,
    LenzAuthError,
    LenzError,
    LenzQuotaExceededError,
    LenzRateLimitError,
    LenzValidationError,
    map_response_to_error,
)


def _body(payload: dict) -> bytes:
    return json.dumps(payload).encode()


class TestMapResponseToError:
    def test_401_maps_to_auth_error(self):
        e = map_response_to_error(401, _body({"detail": "bad key"}), {"X-Request-ID": "rq1"})
        assert isinstance(e, LenzAuthError)
        assert e.request_id == "rq1"
        assert "/docs/auth" in e.doc_url
        assert e.status_code == 401

    def test_403_maps_to_auth_error(self):
        e = map_response_to_error(403, _body({"detail": "forbidden"}), {})
        assert isinstance(e, LenzAuthError)

    def test_402_maps_to_quota_error_with_the_full_envelope(self):
        e = map_response_to_error(
            402,
            _body(
                {
                    "detail": "No remaining claim checks.",
                    "code": "no_credits",
                    "upgrade_url": "https://lenz.io/plans",
                    "remaining": 0,
                    "resets_at": "2026-09-01T00:00:00+00:00",
                }
            ),
            {},
        )
        assert isinstance(e, LenzQuotaExceededError)
        assert e.code == "no_credits"
        assert e.upgrade_url == "https://lenz.io/plans"
        assert e.remaining == 0
        assert e.resets_at == "2026-09-01T00:00:00+00:00"
        # Not an auth error — the whole point of the 402 migration.
        assert not isinstance(e, LenzAuthError)

    def test_402_remaining_is_none_when_the_server_omits_it(self):
        """None, not 0. The server omits rather than nulls precisely so
        'unknown' stays distinguishable from 'empty'."""
        e = map_response_to_error(402, _body({"detail": "out", "code": "no_credits"}), {})
        assert e.remaining is None
        assert e.resets_at is None
        assert e.requested is None
        assert e.credit_balance is None
        assert e.cost is None

    def test_402_carries_the_credit_pool_and_the_calls_price(self):
        """`remaining` is in the capability's unit, `credit_balance` and `cost`
        are in credits — together they say "4 credits, and this verify wants
        10", which "0 verifications left" alone cannot."""
        e = map_response_to_error(
            402,
            _body(
                {
                    "detail": "No remaining claim checks.",
                    "code": "no_credits",
                    "remaining": 0,
                    "credits_remaining": 4,
                    "cost": 10,
                }
            ),
            {},
        )
        assert e.remaining == 0  # verifications
        assert e.credit_balance == 4  # credits
        assert e.cost == 10  # credits this call would have taken

    def test_credit_balance_does_not_hijack_the_deprecated_alias(self):
        """The body's `credits_remaining` (the pool) must NOT land on the
        SDK's same-named deprecated property, which aliases `remaining` and
        means a different quantity. Silently repointing it would change the
        number under everyone still on the deprecated path."""
        e = map_response_to_error(
            402,
            _body({"detail": "out", "remaining": 0, "credits_remaining": 4, "cost": 10}),
            {},
        )
        with pytest.deprecated_call():
            assert e.credits_remaining == 0  # still `remaining`, not the pool's 4
        assert e.credit_balance == 4

    def test_402_requested_echoed_for_batch_shortfall(self):
        e = map_response_to_error(
            402,
            _body({"detail": "batch too big", "code": "no_credits", "requested": 5, "remaining": 2}),
            {},
        )
        assert e.requested == 5
        assert e.remaining == 2

    def test_credits_remaining_alias_still_works_but_warns(self):
        e = map_response_to_error(402, _body({"detail": "out", "remaining": 7}), {})
        with pytest.deprecated_call():
            assert e.credits_remaining == 7

    def test_credits_remaining_alias_flattens_unknown_to_zero(self):
        """Documents the exact ambiguity `remaining` exists to fix."""
        e = map_response_to_error(402, _body({"detail": "out"}), {})
        assert e.remaining is None
        with pytest.deprecated_call():
            assert e.credits_remaining == 0

    def test_credits_remaining_is_still_writable(self):
        """2.7.0 is a MINOR — a read-only property here would break anyone
        constructing or mutating the error, which `LenzError.__init__`'s
        **extra splat explicitly invites."""
        e = LenzQuotaExceededError(message="x")
        with pytest.deprecated_call():
            e.credits_remaining = 5
        assert e.remaining == 5

    def test_credits_remaining_accepted_as_a_constructor_kwarg(self):
        with pytest.deprecated_call():
            e = LenzQuotaExceededError(message="x", credits_remaining=7)
        assert e.remaining == 7

    def test_blank_strings_are_unknown_not_zero(self):
        """Whitespace-only must read as "unknown", matching Node's trim.
        Number(" ") === 0 in JS, so an untrimmed check diverges."""
        e = map_response_to_error(402, _body({"detail": "out", "remaining": "  "}), {})
        assert e.remaining is None

    def test_malformed_code_and_upgrade_url_do_not_stringify(self):
        """A dict rendered as "{'a': 1}" would be shown to a user as a URL."""
        e = map_response_to_error(402, _body({"detail": "out", "code": 42, "upgrade_url": {"a": 1}}), {})
        assert e.code == ""
        assert e.upgrade_url == ""

    def test_403_is_always_an_auth_error_even_with_a_quota_code(self):
        """Quota is 402 and only 402.

        There is no "403 + quota code also means quota" fallback: the only
        thing it would cover is a server rollback, and carrying it forever
        to insure against that is not worth the branch. The MCP server keeps
        an equivalent branch because it deploys as a separate service.
        """
        e = map_response_to_error(
            403,
            _body({"detail": "No remaining claim checks.", "code": "no_credits"}),
            {},
        )
        assert isinstance(e, LenzAuthError)
        assert not isinstance(e, LenzQuotaExceededError)
        assert e.code == "no_credits"  # still surfaced for the caller to inspect

    def test_code_is_carried_on_the_base_error(self):
        e = map_response_to_error(429, _body({"detail": "slow", "code": "extract_daily_limit"}), {})
        assert e.code == "extract_daily_limit"

    def test_422_maps_to_validation_error_with_field_errors(self):
        body = _body({"detail": [{"loc": ["text"], "msg": "required", "type": "missing"}]})
        e = map_response_to_error(422, body, {})
        assert isinstance(e, LenzValidationError)
        assert len(e.errors) == 1
        assert e.errors[0]["msg"] == "required"

    def test_429_maps_to_rate_limit_error_with_retry_after(self):
        e = map_response_to_error(429, _body({"detail": "slow down"}), {"Retry-After": "30"})
        assert isinstance(e, LenzRateLimitError)
        assert e.retry_after == 30

    def test_429_picks_retry_after_from_body_when_header_absent(self):
        e = map_response_to_error(429, _body({"detail": "slow", "retry_after": 12}), {})
        assert isinstance(e, LenzRateLimitError)
        assert e.retry_after == 12

    def test_429_reads_reset_in_seconds_the_key_the_server_actually_sends(self):
        """`retry_after` in the body was an SDK invention the server never
        emitted; `reset_in_seconds` is the real field."""
        e = map_response_to_error(
            429,
            _body({"detail": "capped", "code": "extract_daily_limit", "limit": 1000, "reset_in_seconds": 7200}),
            {},
        )
        assert e.retry_after == 7200
        assert e.reset_in_seconds == 7200
        assert e.limit == 1000
        assert e.code == "extract_daily_limit"

    def test_429_blank_retry_after_falls_through_to_the_body(self):
        """A blank header must not win the resolution chain.

        With the body carrying a real wait, `0` here would mean the header
        short-circuited and the caller was told to retry immediately against
        a server that just throttled it.
        """
        e = map_response_to_error(429, _body({"detail": "slow", "reset_in_seconds": 42}), {"Retry-After": ""})
        assert e.retry_after == 42

    def test_429_unparseable_retry_after_falls_through_to_the_body(self):
        """`Retry-After` may legally be an HTTP-date (RFC 7231).

        Truthy but unparseable, so a first-truthy-wins chain would take it,
        coerce to None, and land on 0 — discarding the body's real value.
        """
        e = map_response_to_error(
            429,
            _body({"detail": "slow", "reset_in_seconds": 42}),
            {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"},
        )
        assert e.retry_after == 42

    def test_429_with_no_wait_anywhere_is_zero(self):
        e = map_response_to_error(429, _body({"detail": "slow"}), {"Retry-After": ""})
        assert e.retry_after == 0
        assert e.reset_in_seconds is None

    def test_429_carries_upgrade_url(self):
        """The server puts upgrade_url on 429 too — a developer hitting the
        daily /extract cap also wants to know a paid plan lifts it."""
        e = map_response_to_error(
            429,
            _body({"detail": "capped", "reset_in_seconds": 60, "upgrade_url": "https://lenz.io/plans"}),
            {},
        )
        assert e.upgrade_url == "https://lenz.io/plans"

    def test_5xx_maps_to_api_error(self):
        e = map_response_to_error(503, _body({"detail": "unavailable"}), {"x-request-id": "rq2"})
        assert isinstance(e, LenzAPIError)
        assert e.request_id == "rq2"

    def test_unknown_status_falls_through_to_base(self):
        e = map_response_to_error(418, _body({"detail": "i'm a teapot"}), {})
        # Not in our table; should be base LenzError, not raised
        assert isinstance(e, LenzError)
        assert not isinstance(e, (LenzAuthError, LenzAPIError, LenzRateLimitError))

    def test_malformed_body_does_not_explode(self):
        e = map_response_to_error(500, b"not json {", {})
        assert isinstance(e, LenzAPIError)
        assert e.message  # has a default

    def test_str_includes_fix_and_doc_url_and_request_id(self):
        e = map_response_to_error(401, _body({"detail": "bad key"}), {"X-Request-ID": "rq_abc"})
        s = str(e)
        assert "Cause:" in s
        assert "Fix:" in s
        assert "Docs:" in s
        assert "rq_abc" in s


@pytest.mark.parametrize(
    "status,expected_cls",
    [
        (401, LenzAuthError),
        (403, LenzAuthError),
        (402, LenzQuotaExceededError),
        (422, LenzValidationError),
        (429, LenzRateLimitError),
        (500, LenzAPIError),
        (502, LenzAPIError),
        (503, LenzAPIError),
        (504, LenzAPIError),
    ],
)
def test_status_to_class_table(status, expected_cls):
    e = map_response_to_error(status, b"{}", {})
    assert isinstance(e, expected_cls)
