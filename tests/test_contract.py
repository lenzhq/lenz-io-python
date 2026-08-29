"""Strict-deserialization contract test.

The SDK's Pydantic models use ``extra="allow"`` so customers don't
break when the server adds fields. The downside: rename misses (server
emits ``foo`` but the SDK model still expects ``foo_old``) are silent —
the old field defaults to its zero value and the new field lands in
``__pydantic_extra__`` where nobody looks.

This test walks each captured server-response payload against the
corresponding SDK model and asserts that every key in the payload
maps to a typed field (recursively, into nested models and list
items). Any unmapped key fails CI with a precise location.

Fixtures are frozen JSON in ``tests/fixtures/contract/``. They mirror
the literal response shape the server emits — capture by ``curl``
against the live API or generate via
``manage.py shell -c "from lenz.api.public_api import public_api; ..."``
from the Lenz checkout.

Cross-language guarantee: the Node SDK validates the SAME fixture
files so any drift between the two SDKs surfaces in CI.
"""

from __future__ import annotations

import json
import types
import typing
from pathlib import Path

import pytest
from pydantic import BaseModel

from lenz_io.models import (
    AssessResponse,
    ExtractedClaims,
    TaskStatus,
    Usage,
    Verification,
)

# PEP 604 unions (`int | None`) produce `types.UnionType` on 3.10+, while
# `typing.Union[int, None]` produces `typing.Union`. Older 3.9 has only
# the typing form. Match against both so the walker handles either.
_UNION_ORIGINS: tuple = (typing.Union,)
if hasattr(types, "UnionType"):  # pragma: no cover  -- 3.10+
    _UNION_ORIGINS = (typing.Union, types.UnionType)

FIXTURES = Path(__file__).parent / "fixtures" / "contract"


def _unwrap_model(annotation) -> type[BaseModel] | None:
    """Return the BaseModel class inside ``annotation`` (handles Optional, Union)."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin in _UNION_ORIGINS:
        for arg in args:
            if isinstance(arg, type) and issubclass(arg, BaseModel):
                return arg
    return None


def _unwrap_list_item_model(annotation) -> type[BaseModel] | None:
    """Return the BaseModel inside ``list[Model]`` / ``list[Model] | None``."""
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin is list and args:
        return _unwrap_model(args[0])
    # Optional[list[Model]] — peel the union, then the list
    if args:
        for arg in args:
            if typing.get_origin(arg) is list:
                inner = typing.get_args(arg)
                if inner:
                    return _unwrap_model(inner[0])
    return None


# Per-field annotation overrides for dict-typed bag fields whose values
# we don't want to walk strictly. Currently empty — webhook ``result`` is
# treated as a dict[str, Any] on the dataclass side (event.result is
# ``dict[str, Any]``) and we cover its shape separately via the
# ``Verification`` walk.
_DICT_BAG_FIELDS: set[tuple[str, str]] = set()


def _check(payload, model_cls: type[BaseModel], path: str = "") -> list[str]:
    """Recursively compare ``payload`` keys to ``model_cls.model_fields``.

    Returns the list of error strings (empty when clean).
    """
    if not isinstance(payload, dict):
        return []
    fields = set(model_cls.model_fields.keys())
    payload_keys = set(payload.keys())
    extras = sorted(payload_keys - fields)
    errors: list[str] = []
    if extras:
        errors.append(f"{path or model_cls.__name__}: unknown server fields {extras} (model={model_cls.__name__})")
    for key, value in payload.items():
        if key not in fields:
            continue
        field = model_cls.model_fields[key]
        ann = field.annotation
        # Skip bag fields explicitly marked as opaque
        if (model_cls.__name__, key) in _DICT_BAG_FIELDS:
            continue
        if isinstance(value, dict):
            nested = _unwrap_model(ann)
            if nested is not None:
                errors.extend(_check(value, nested, f"{path}.{key}" if path else key))
        elif isinstance(value, list):
            inner_cls = _unwrap_list_item_model(ann)
            if inner_cls is not None:
                for i, item in enumerate(value):
                    if isinstance(item, dict):
                        errors.extend(_check(item, inner_cls, f"{path}.{key}[{i}]" if path else f"{key}[{i}]"))
    return errors


def _load(name: str) -> dict:
    path = FIXTURES / name
    return json.loads(path.read_text())


@pytest.mark.parametrize(
    "fixture_name,model_cls",
    [
        ("extract_response.json", ExtractedClaims),
        ("assess_single_claim.json", AssessResponse),
        ("assess_multiclaim.json", AssessResponse),
        ("verify_status_completed.json", TaskStatus),
        ("verify_status_failed.json", TaskStatus),
        ("verifications_detail.json", Verification),
        ("usage.json", Usage),
    ],
)
def test_contract_no_unknown_fields(fixture_name, model_cls):
    """Every field in the captured server response is typed in the SDK model.

    If this fails, the server emitted a field the SDK doesn't recognize.
    Either:
      - Add the field to the SDK model (when the server contract grew),
      - Or update the fixture (when the test capture is stale).

    DO NOT relax this test by allowing extras — that's exactly what the
    contract test exists to catch.
    """
    payload = _load(fixture_name)
    errors = _check(payload, model_cls)
    if errors:
        pytest.fail("\n".join([f"{fixture_name} → {model_cls.__name__}:", *errors]))
    # Sanity: model_validate also succeeds (smoke against the lax model)
    model_cls.model_validate(payload)


def test_webhook_payload_completed_walks_result_as_verification():
    """The webhook ``result`` field is typed as ``dict[str, Any]`` on the
    dataclass side, but its server contents match ``Verification`` exactly.
    Walk it via Verification to catch verdict-block shape drift."""
    payload = _load("webhook_payload_completed.json")
    # Top-level dataclass fields (event, task_id, etc.) are positional;
    # only assert the result block is Verification-shaped.
    result = payload.get("result") or {}
    errors = _check(result, Verification, "result")
    if errors:
        pytest.fail("\n".join(["webhook_payload_completed.json → Verification:", *errors]))


def test_assess_multiclaim_round_trips():
    """Sanity-check the model can re-emit JSON that round-trips through
    the schema. Distinct from the strict check above — this verifies
    serialization, not just validation."""
    payload = _load("assess_multiclaim.json")
    parsed = AssessResponse.model_validate(payload)
    assert len(parsed.claims) == 3
    assert parsed.claims[0].verdict == "True"
    assert parsed.claims[0].confidence == "high"
    assert parsed.claims[0].verification_url is not None


# ── Error envelopes ─────────────────────────────────────────────────────────
# The 402/429 bodies are part of the wire contract too, and drift between the
# two SDKs' error mapping is exactly what this file exists to catch. Node
# validates the SAME fixture files in test/contract.test.ts.


def test_quota_402_envelope_maps_every_field():
    from lenz_io.errors import LenzQuotaExceededError, map_response_to_error

    fixture = _load("error_quota_402.json")
    err = map_response_to_error(402, json.dumps(fixture), {})

    assert isinstance(err, LenzQuotaExceededError)
    assert err.message == fixture["detail"]
    assert err.code == fixture["code"]
    assert err.upgrade_url == fixture["upgrade_url"]
    assert err.remaining == fixture["remaining"]
    assert err.resets_at == fixture["resets_at"]
    # The pool behind the capability figure, and this call's price in credits.
    # The server's `credits_remaining` lands on `credit_balance`: the SDK's
    # own `credits_remaining` is an older deprecated alias of `remaining`, in
    # the capability's unit, and keeps that meaning.
    assert err.credit_balance == fixture["credits_remaining"]
    assert err.cost == fixture["cost"]

    # Every key the server sends must be consumed by a typed field or
    # deliberately skipped — an unhandled key means the mapper drifted.
    # `doc_url` is intentionally not mapped: the SDK sets its own doc_url from
    # the status table so the link is right even against an older server.
    handled = {
        "detail",
        "code",
        "upgrade_url",
        "remaining",
        "credits_remaining",
        "cost",
        "resets_at",
        "requested",
        "doc_url",
    }
    assert not set(fixture) - handled


def test_rate_limit_429_envelope_maps_every_field():
    from lenz_io.errors import LenzRateLimitError, map_response_to_error

    fixture = _load("error_rate_limit_429.json")
    err = map_response_to_error(429, json.dumps(fixture), {})

    assert isinstance(err, LenzRateLimitError)
    assert err.message == fixture["detail"]
    assert err.code == fixture["code"]
    assert err.limit == fixture["limit"]
    assert err.reset_in_seconds == fixture["reset_in_seconds"]
    assert err.retry_after == fixture["reset_in_seconds"]
    # upgrade_url is on 429 too — the daily /extract cap is lifted by a plan.
    assert err.upgrade_url == fixture["upgrade_url"]

    handled = {"detail", "code", "limit", "reset_in_seconds", "upgrade_url", "doc_url"}
    assert not set(fixture) - handled


@pytest.mark.parametrize(
    "fixture_name,expected_code,expected_retry_after",
    [
        ("error_upstream_unavailable_503.json", "upstream_unavailable", 90),
        ("error_capacity_503.json", "capacity", 105),
    ],
)
def test_unavailable_503_envelope_maps_every_field(fixture_name, expected_code, expected_retry_after):
    """The two 503 shapes (provider exhaustion on the sync endpoints; the
    admission-control shed on /verify) map to LenzUpstreamUnavailableError —
    a LenzAPIError subclass so existing handlers keep catching it."""
    from lenz_io.errors import LenzAPIError, LenzUpstreamUnavailableError, map_response_to_error

    fixture = _load(fixture_name)
    err = map_response_to_error(503, json.dumps(fixture), {})

    assert isinstance(err, LenzUpstreamUnavailableError)
    assert isinstance(err, LenzAPIError)
    assert err.message == fixture["detail"]
    assert err.code == expected_code
    assert err.retry_after == expected_retry_after
    assert err.body == fixture

    handled = {"detail", "code", "retry_after", "doc_url"}
    assert not set(fixture) - handled


def test_webhook_payload_failed_maps_every_field():
    """Every key of the failed-event payload is consumed by a typed
    VerificationFailed attribute (or is one of the always-present-but-null
    payload slots that belong to the other events)."""
    import dataclasses

    from lenz_io.webhooks import VerificationFailed, _build_event

    payload = _load("webhook_payload_failed.json")
    event = _build_event(payload)
    assert isinstance(event, VerificationFailed)
    assert event.error == payload["error"]
    assert event.failure_class == payload["failure_class"]
    assert event.retryable is payload["retryable"]
    assert event.status == "failed"

    typed = {f.name for f in dataclasses.fields(VerificationFailed)}
    # `result` / `needs_input` ride as null on the failed event — the wire
    # payload has one field set per event kind.
    handled = typed | {"result", "needs_input"}
    assert not set(payload) - handled
