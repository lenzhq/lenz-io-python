"""``.verification`` on the ``verification.*`` webhook events: the
verification as ``client.get_status`` returns it (a ``TaskStatus``; on a
completed event the verdict is ``.verification.result``), built from either
payload shape, as in the Node SDK."""

from __future__ import annotations

import pytest
from parity_observe import load, names

from lenz_io import (
    TaskStatus,
    Verification,
    VerificationCompleted,
    VerificationFailed,
    VerificationNeedsInput,
    WebhookEvent,
    parse_webhook,
)

_VERIFICATION_EVENTS = [n for n in names() if n.startswith("webhook__verification_")]


def _both(name: str) -> tuple[WebhookEvent, WebhookEvent]:
    return parse_webhook(load("legacy", name)["body"]), parse_webhook(load("canonical", name)["body"])


@pytest.mark.parametrize("name", _VERIFICATION_EVENTS)
def test_both_shapes_give_the_same_status_envelope(name: str) -> None:
    old, new = _both(name)
    assert type(old) is type(new)
    a, b = old.verification, new.verification  # type: ignore[attr-defined]
    assert isinstance(a, TaskStatus) and isinstance(b, TaskStatus)
    assert a.status == b.status == old.status
    assert a.task_id == b.task_id == old.task_id
    if isinstance(old, VerificationCompleted):
        assert isinstance(a.result, Verification) and isinstance(b.result, Verification)
        for field in ("verification_id", "claim", "verdict", "confidence", "lenz_score", "key_finding"):
            assert getattr(a.result, field) == getattr(b.result, field), field
        assert a.result.verification_id == old.verification_id
        assert a.result.verdict == old.result.get("verdict")
    elif isinstance(old, VerificationFailed):
        assert a.failure is not None and b.failure is not None
        for field in ("code", "failure_class", "retryable"):
            assert getattr(a.failure, field) == getattr(b.failure, field), field
        assert a.failure_reason == old.error
        assert a.retryable == old.retryable
    else:
        assert isinstance(old, VerificationNeedsInput)
        assert a.reason == b.reason == old.reason
        assert [c.claim for c in a.claims] == [c.claim for c in b.claims] == [c.claim for c in old.claims]
        assert a.hint == b.hint == old.hint


def test_it_is_a_property_so_the_event_repr_and_fields_are_unchanged() -> None:
    event = parse_webhook(load("canonical", _VERIFICATION_EVENTS[0])["body"])
    assert "verification" not in vars(event)
    assert "verification=" not in repr(event)


@pytest.mark.parametrize(
    "payload",
    [
        {"event": "verification.completed", "task_id": "t", "verification": {"status": "completed", "result": 5}},
        {"event": "verification.completed", "task_id": "t", "result": "not an object"},
        {"event": "verification.failed", "task_id": "t", "verification": {"status": ["failed"]}},
        {"event": "verification.needs_input", "task_id": "t", "needs_input": {"claims": "x"}},
    ],
)
def test_a_malformed_recognised_event_reads_none_never_raises(payload: dict) -> None:
    event = parse_webhook(payload)
    assert event.verification is None  # type: ignore[attr-defined]


def test_a_completed_event_without_a_result_reads_a_status_without_one() -> None:
    event = parse_webhook({"event": "verification.completed", "task_id": "t"})
    assert isinstance(event, VerificationCompleted)
    assert event.verification is not None
    assert event.verification.status == "completed"
    assert event.verification.task_id == "t"
    assert event.verification.result is None


def test_other_events_do_not_carry_it() -> None:
    for payload in (
        {"event": "certificate.timestamped", "task_id": "t"},
        {"event": "review.completed", "review_id": "r"},
        {"event": "something.new", "verification": {"status": "completed"}},
    ):
        assert not hasattr(parse_webhook(payload), "verification")


_SPARSE_RESULT = {"verification_id": "v1", "claim": "A.", "verdict": "True", "lenz_score": 9}
_SPARSE = [
    pytest.param(
        {
            "event": "verification.completed",
            "event_id": "evt_1",
            "verification": {"status": "completed", "task_id": "t", "result": dict(_SPARSE_RESULT)},
        },
        id="current-shape",
    ),
    pytest.param(
        {"event": "verification.completed", "task_id": "t", "result": dict(_SPARSE_RESULT)},
        id="original-shape",
    ),
]


@pytest.mark.parametrize("payload", _SPARSE)
def test_a_sparse_result_reads_the_webhook_defaults(payload: dict) -> None:
    # The values ``event.result`` gives a key the payload left out (and the
    # Node SDK's ``event.verification.result``), not the model's own defaults.
    event = parse_webhook(payload)
    assert isinstance(event, VerificationCompleted)
    assert event.verification is not None
    result = event.verification.result
    assert isinstance(result, Verification)
    assert (result.visibility, result.depth, result.created_at) == ("private", "standard", "")
    assert (result.confidence, result.language, result.verdict, result.lenz_score) == ("low", "en", "True", 9)


def test_a_sparse_current_shape_result_matches_event_result() -> None:
    event = parse_webhook(_SPARSE[0].values[0])
    assert isinstance(event, VerificationCompleted)
    assert event.verification is not None and event.verification.result is not None
    result = event.verification.result
    for key in ("verification_id", "claim", "visibility", "depth", "verdict", "confidence", "created_at", "language"):
        assert getattr(result, key) == event.result[key], key


@pytest.mark.parametrize(
    ("event", "nested"),
    [
        ("verification.completed", "failed"),
        ("verification.failed", "completed"),
        ("verification.needs_input", "processing"),
        ("verification.completed", None),
    ],
)
def test_a_nested_status_of_another_kind_is_not_presented_as_this_one(event: str, nested: str | None) -> None:
    body: dict = {"task_id": "t"} if nested is None else {"status": nested, "task_id": "t"}
    parsed = parse_webhook({"event": event, "event_id": "evt_1", "verification": body})
    assert parsed.verification is None  # type: ignore[attr-defined]
