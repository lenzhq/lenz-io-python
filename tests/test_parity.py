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
from parity_observe import FIXTURES, MISSING, _extract_via_client, _model_for, attr_view, load, names, observe

from lenz_io import models
from lenz_io.webhooks import parse_webhook

EXPECTED = FIXTURES / "expected"

# ── What the newer shape cannot reproduce ───────────────────────────────
#
# Each gap is allowed only where the two recorded responses themselves differ
# in the value it comes from: a rule never excuses a difference the SDK made.
# The categories are the ones the Node SDK allows, so the two behave alike:
# the server's sentences (error / failure text), the codes and field errors it
# sends on a 422, `chain_id`, the review / citation-check webhook `task_id`,
# and a row hint the newer shape does not carry. The single entries in
# ``KNOWN_GAPS`` below fall in those categories too, plus one where the two
# recordings number an id differently.

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
        legacy_sentence = isinstance(lb.get("detail"), str)
        if rest in _MESSAGE_PATHS and legacy_sentence and lb.get("detail") != cb.get("detail"):
            return "the server words its `detail` sentence differently"
        if rest in _MESSAGE_PATHS and "detail" not in lb and isinstance(cb.get("detail"), str):
            return "the newer shape sends a `detail` sentence where the original sent none"
        if rest == "code" and "code" not in lb and "code" in cb:
            return "the newer shape names a `code` where the original sent none"
        if rest.startswith("errors") and not _validation_items(lb) and _validation_items(cb):
            return "the newer shape lists the field errors where the original listed none"
        if rest in ("hint", "fix", "friendly_text", "payload_json.error.fix") and lb.get("hint") != cb.get("hint"):
            return "the server words the hint differently"
    if head == "wait" or path in ("dump.error", "render"):
        sentence = lb.get("error")
        if (
            name.startswith("verify__")
            and isinstance(sentence, str)
            and sentence != _failure(cb).get("detail")
            and "failure" in cb
        ):
            if path in ("dump.error", "render") or rest in _MESSAGE_PATHS:
                return "the failed run's sentence: the newer shape's `failure.detail` is worded differently"
    if path in ("dump.hint", "wait.hint") and lb.get("hint", "") != _failure(cb).get("hint", ""):
        return "the newer shape carries a hint the original did not"
    option_claim = path.startswith("wait.payload.claims[") and path.endswith("].claim")
    if option_claim and name.startswith("verify__status_needs_input"):
        return "LenzNeedsInputError.payload is the status dump: an option also carries its newer name `claim`"
    if path == "dump.chain_id" and "chain_id" in lb and "chain_id" not in cb:
        return "`chain_id` is not in the newer shape"
    if path == "event.task_id" and "task_id" in lb and "task_id" not in cb:
        return "review/citecheck webhooks drop `task_id` (never pollable); it reads the review or check id"
    if path == "event.result.completed_at" and name.startswith("webhook__verification_completed"):
        return "`result` also carries the newer `completed_at` beside `modified_at`"
    return KNOWN_GAPS.get((name, path))


#: Single gaps no rule covers, each with its reason.
KNOWN_GAPS: dict[tuple[str, str], str] = {
    ("review__get_assessment_rows_full_fields.json", "dump.claims[0].assessment.hint"): (
        "the newer shape sends no hint on a completed quick check with other claims found"
    ),
    ("review__get_failed_every_assessment_failed.json", "dump.failure.hint"): "the server words the hint differently",
    ("review__get_failed_every_assessment_failed.json", "render"): "the same hint, printed by the CLI",
    ("review__get_failed_every_assessment_failed.json", "render_issues"): "the same hint, printed by the CLI",
    ("extract__not_a_claim_beside_claims.json", "dump.status"): (
        "the newer shape answers `ready` when claims came back beside a non-claim"
    ),
    ("webhook__review_completed.json", "event.review.claims[0].verification.task_id"): (
        "the two recordings number their task ids differently (the newer payload has no delivery task_id)"
    ),
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
    view = {k: v for k, v in got.items() if k not in ("dump", "dump_unset", "repr", "render_json")}
    if "event" in expected:
        # A review or citation check on an event: read it by attribute too.
        event = parse_webhook(fixture["body"])
        for key, old in expected["event"].items():
            value = getattr(event, key, None)
            if isinstance(old, dict) and hasattr(value, "model_dump"):
                view["event"][key] = attr_view(value, old)
    if "dump" in expected:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            cls = _model_for(name)
            model = (
                _extract_via_client(name, fixture["body"])
                if cls is models.ExtractedClaims
                else cls.model_validate(fixture["body"])
            )
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
    expected = {k: v for k, v in _expected(name).items() if k not in ("render_json", "dump_unset", "repr")}
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
        expected = {k: v for k, v in _expected(name).items() if k not in ("render_json", "dump_unset", "repr")}
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


# ── Schemas and pickles ─────────────────────────────────────────────────────


def test_model_schemas_match_the_previous_release() -> None:
    """Every model the previous release had publishes the same validation and
    serialization JSON schema: the newer names are properties, not fields."""
    from parity_static import schemas

    def _shape(schema: Any) -> Any:
        # Doc text (class docstrings) may change; the shape may not.
        if isinstance(schema, dict):
            return {k: _shape(v) for k, v in schema.items() if k != "description"}
        if isinstance(schema, list):
            return [_shape(v) for v in schema]
        return schema

    frozen = _shape(json.loads((FIXTURES / "schemas.json").read_text()))
    current = _shape(schemas())
    assert {k: current.get(k) for k in frozen} == frozen


def test_objects_pickled_by_the_previous_release_still_dump() -> None:
    import base64
    import pickle

    frozen = json.loads((FIXTURES / "pickles.json").read_text())
    assert frozen
    for name, blob in frozen.items():
        model = pickle.loads(base64.b64decode(blob))
        assert model.model_dump(mode="json") == _expected(name)["dump"], name
