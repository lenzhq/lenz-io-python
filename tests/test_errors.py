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

    def test_402_maps_to_quota_error_with_credits_remaining(self):
        e = map_response_to_error(402, _body({"detail": "out of credits", "credits_remaining": 0}), {})
        assert isinstance(e, LenzQuotaExceededError)
        assert e.credits_remaining == 0

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
