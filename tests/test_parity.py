"""Both response shapes give existing code exactly what the previous release did.

The oracle in ``tests/fixtures/parity/expected/`` was produced by the code
BEFORE this SDK read the newer response shape (see
``tests/parity_generate.py``), from the original-shape responses in
``tests/fixtures/parity/legacy/``. Each response has its newer-shape twin in
``tests/fixtures/parity/canonical/`` (the same answer, as the API's newer
version sends it).

* An original-shape response must produce the frozen output byte for byte:
  the model dump, every CLI rendering (text and ``--json``), the exception
  ``wait`` raises, the error an HTTP error maps to, the parsed webhook.
* Its newer-shape twin must produce the same values through every attribute
  the previous release had, the same CLI text, the same ``wait`` outcome and
  the same error, except for the few values the newer shape does not carry,
  listed one by one in ``KNOWN_GAPS`` with the reason.

``model_dump()`` and the CLI's ``--json`` show the shape the server sent, so
they are compared for the original shape only.
"""

from __future__ import annotations

import json
import warnings
from typing import Any

import pytest
from parity_observe import FIXTURES, MISSING, _model_for, attr_view, load, names, observe

EXPECTED = FIXTURES / "expected"

# ── What the newer shape cannot reproduce ───────────────────────────────
#
# Each gap is allowed only where the two recorded responses themselves differ
# in the value it comes from: a rule never excuses a difference the SDK made.

#: An exception's text fields: built from the body's ``detail``.
_MESSAGE_PATHS = ("message", "cause", "friendly_text", "payload_json.error.message")


def _failure(body: dict[str, Any]) -> dict[str, Any]:
    failure = body.get("failure")
    return failure if isinstance(failure, dict) else {}


def _validation_items(body: dict[str, Any]) -> Any:
    detail = body.get("detail")
    return detail if isinstance(detail, list) else body.get("errors", [])


def _allowed(name: str, path: str, legacy: dict[str, Any], canonical: dict[str, Any]) -> str | None:
    """Why ``path`` may differ for ``name``, or ``None`` when it may not."""
    lb, cb = legacy["body"], canonical["body"]
    head, _, rest = path.partition(".")
    if head == "error":
        if rest in _MESSAGE_PATHS and lb.get("detail") != cb.get("detail"):
            return "the server words `detail` differently (a 422 sends a sentence, not the list)"
        if rest == "code" and lb.get("code") != cb.get("code"):
            return "the server sends a different (or a first) `code`"
        if rest.startswith("errors") and _validation_items(lb) != _validation_items(cb):
            return "the server's field errors differ"
        if rest in ("hint", "fix", "friendly_text", "payload_json.error.fix") and lb.get("hint") != cb.get("hint"):
            return "the server words the hint differently"
    if head == "wait" or path in ("dump.error", "render"):
        sentence = lb.get("error")
        if isinstance(sentence, str) and sentence != _failure(cb).get("detail"):
            if path in ("dump.error", "render") or rest in _MESSAGE_PATHS:
                return "the failed run's sentence: the newer shape's `failure.detail` is worded differently"
    if path in ("dump.hint", "wait.hint") and lb.get("hint", "") != _failure(cb).get("hint", ""):
        return "the newer shape carries a hint the original did not"
    if path.startswith("wait.payload.") and name.startswith("verify__status_needs_input"):
        return "LenzNeedsInputError.payload is the status as sent (model_dump)"
    if path == "dump.chain_id" and "chain_id" in lb and "chain_id" not in cb:
        return "`chain_id` is not in the newer shape"
    if path == "event.task_id" and "task_id" in lb and "task_id" not in cb:
        return "review/citecheck webhooks drop `task_id`; the event reads `event_id` there"
    if path.startswith("event.result.") and name.startswith("webhook__verification_completed"):
        return "`result` is the dict as sent; the newer shape's carries `completed_at`"
    return KNOWN_GAPS.get((name, path))


#: Single gaps no rule covers, each with its reason.
KNOWN_GAPS: dict[tuple[str, str], str] = {
    ("assess__list_compound_item.json", "dump.claims[0].hint"): (
        "the newer shape sends no hint on a completed row with other claims found"
    ),
    ("assess__list_compound_item.json", "render"): "the same hint line, printed by the CLI",
    ("review__get_assessment_rows_full_fields.json", "dump.claims[0].assessment.hint"): (
        "the newer shape sends no hint on a completed quick check with other claims found"
    ),
    ("review__get_failed_every_assessment_failed.json", "dump.failure.hint"): "the server words the hint differently",
    ("review__get_failed_every_assessment_failed.json", "render"): "the same hint, printed by the CLI",
    ("review__get_failed_every_assessment_failed.json", "render_issues"): "the same hint, printed by the CLI",
    ("extract__not_a_claim_beside_claims.json", "dump.status"): (
        "the newer shape answers `ready` when claims came back beside a non-claim"
    ),
    ("verify__list_200.json", "dump.items[1].verification_id"): "the two recordings list different verifications",
    ("verify__list_200.json", "dump.items[1].created_at"): "the two recordings list different verifications",
    (
        "verify__verification_200_covered.json",
        "dump.coverage.certificate_id",
    ): "the two recordings issued different certificates",
    (
        "verify__verification_200_covered.json",
        "dump.coverage.certificate_url",
    ): "the two recordings issued different certificates",
    ("review__get_completed_issues_all_verified.json", "dump.claims[1].verification.task_id"): (
        "the two recordings ran different tasks"
    ),
    (
        "review__get_completed_issues_full.json",
        "dump.claims[2].verification.task_id",
    ): "the two recordings ran different tasks",
}


def _expected(name: str) -> dict[str, Any]:
    return json.loads((EXPECTED / name).read_text())


def _diff(a: Any, b: Any, path: str = "") -> list[tuple[str, Any, Any]]:
    if isinstance(a, dict) and isinstance(b, dict):
        out: list[tuple[str, Any, Any]] = []
        for key in sorted(set(a) | set(b)):
            out += _diff(a.get(key, MISSING), b.get(key, MISSING), f"{path}.{key}" if path else key)
        return out
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        out = []
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            out += _diff(x, y, f"{path}[{i}]")
        return out
    return [] if a == b else [(path, a, b)]


#: Attributes this release added to the webhook events. Everything else on an
#: event must read exactly as before.
NEW_EVENT_ATTRS = {"event_id", "failure", "reason", "claims"}


def _old_event_attrs(observed: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    if "event" not in observed:
        return observed
    event = observed["event"]
    added = set(event) - set(expected["event"])
    assert added <= NEW_EVENT_ATTRS, added
    return {**observed, "event": {k: v for k, v in event.items() if k in expected["event"]}}


@pytest.mark.parametrize("name", names())
def test_original_shape_is_unchanged(name: str) -> None:
    expected = _expected(name)
    assert _old_event_attrs(observe(name, load("legacy", name)), expected) == expected


def _canonical_view(name: str, expected: dict[str, Any]) -> dict[str, Any]:
    fixture = load("canonical", name)
    got = _old_event_attrs(observe(name, fixture), expected)
    view = {k: v for k, v in got.items() if k not in ("dump", "render_json")}
    if "dump" in expected:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            model = _model_for(name).model_validate(fixture["body"])
            view["dump"] = attr_view(model, expected["dump"])
            returned = expected.get("wait", {}).get("returned")
            if returned is not None:
                # ``wait`` returns the polled result: read it by attribute too.
                view["wait"] = {"returned": attr_view(model.result, returned)}
    return view


def _canonical_names() -> list[str]:
    return [n for n in names() if (FIXTURES / "canonical" / n).exists()]


@pytest.mark.parametrize("name", _canonical_names())
def test_newer_shape_reads_the_same(name: str) -> None:
    expected = {k: v for k, v in _expected(name).items() if k != "render_json"}
    got = _canonical_view(name, expected)
    legacy, canonical = load("legacy", name), load("canonical", name)
    gaps = [
        (path, old, new) for path, old, new in _diff(expected, got) if _allowed(name, path, legacy, canonical) is None
    ]
    assert not gaps, "\n".join(f"{path}: {old!r} -> {new!r}" for path, old, new in gaps)


def test_every_known_gap_is_still_a_gap() -> None:
    """A listed gap that closed is stale: drop it, so the list stays exact."""
    stale = []
    for (name, path), _reason in KNOWN_GAPS.items():
        expected = {k: v for k, v in _expected(name).items() if k != "render_json"}
        if path not in {p for p, _, _ in _diff(expected, _canonical_view(name, expected))}:
            stale.append((name, path))
    assert not stale


# ── Request bodies ──────────────────────────────────────────────────────────


def test_request_bodies_match_the_previous_release() -> None:
    """/review and /citecheck keep every body byte for byte, ``webhook_url``
    included in all three states (on those endpoints ``""`` means no webhook,
    so it must still be sent). A /verify or /verify/batch call with a webhook
    URL is unchanged too."""
    from parity_requests import bodies

    assert bodies() == json.loads((FIXTURES / "requests.json").read_text())
