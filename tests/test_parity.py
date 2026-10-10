"""The current response shape gives existing code exactly what 2.x did.

The oracle in ``tests/fixtures/parity/expected/`` was produced by the 2.x code
(see ``tests/parity_generate.py``), from the 2.x-shape responses in
``tests/fixtures/parity/legacy/``. Each response has its current-shape twin in
``tests/fixtures/parity/canonical/`` (the same answer, as the API's current
version sends it).

* A 2.x-shape WEBHOOK payload must produce the frozen output byte for byte:
  the parsed event. (The SDK's own calls no longer read the 2.x response
  shape; webhooks of work submitted by older clients still arrive in it.)
* A current-shape response must produce the same values through every
  attribute 2.x had, the same CLI text, the same ``wait`` outcome and the
  same error, except for the few values the current shape does not carry,
  listed one by one in ``KNOWN_GAPS`` with the reason.

``model_dump()`` and the CLI's ``--json`` show the shape the server sent, so
they are not compared for the current shape.
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


#: What a cancelled task reads differently in the current version: its own
#: status and event (the original said ``failed``), no failure block (the
#: original sent one with ``failure_class`` ``cancelled``; a wait and the
#: failed error rebuild it, which the ``wait`` paths below still hold to the
#: original), and the CLI's own words for it.
_CANCELLED_SENTENCE_PATHS = (
    "dump.error",
    "wait.cause",
    "wait.message",
    "wait.friendly_text",
    "wait.payload_json.error.message",
)


def _cancelled_gap(name: str, path: str) -> str | None:
    last = path.rsplit(".", 1)[-1]
    if path in ("dump.status", "event.status", "event.event", "event.review.status", "event.citecheck.status"):
        return "a cancelled task is its own status in the current version; the original said `failed`"
    top_level_block = path.startswith(("dump.failure.", "event.review.failure.", "event.citecheck.failure."))
    if top_level_block and last in ("docs_url", "failure_class", "failure_reason", "hint", "retryable"):
        return "the current version sends no failure block for a cancelled task"
    if name == "webhook__verification_cancelled.json" and path in (
        "event.type",
        "event.error",
        "event.failure_class",
        "event.retryable",
    ):
        return "`verification.cancelled` is its own event (`VerificationCancelled`), with no failure fields"
    if name == "verify__status_cancelled_live.json" and path in _CANCELLED_SENTENCE_PATHS:
        return "a running task's original sentence was `Pipeline stopped at: cancelled`; the status reads `Cancelled.`"
    return None


#: The lines the CLI prints for a cancelled task's status, in the original
#: shape and now: the rest of the text must be the same.
_CANCELLED_RENDER_LINES = {
    "failed  — Cancelled.": "cancelled",
    "failed  — Pipeline stopped at: cancelled": "cancelled",
    "Failed: cancelled": "Cancelled.",
}


def _cancelled_render_gap(path: str, old: Any, new: Any) -> bool:
    """Whether a `render` text differs only by the line the CLI words anew for
    a cancelled task."""
    if path not in ("render", "render_issues") or not isinstance(old, str) or not isinstance(new, str):
        return False
    lines = [_CANCELLED_RENDER_LINES.get(line, line) for line in old.splitlines()]
    return lines == new.splitlines()


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
        if rest in ("hint", "fix", "friendly_text", "payload_json.error.fix") and lb.get("hint") != cb.get("hint"):
            return "the server words the hint differently"
    if head == "wait" or path in ("dump.error", "render"):
        sentence = lb.get("error")
        if (
            name.startswith("verify__")
            and isinstance(sentence, str)
            and sentence.startswith("Pipeline stopped: ")
            and "failure" in cb
        ):
            if path in ("dump.error", "render") or rest in _MESSAGE_PATHS:
                return (
                    "a failure read back from storage said 'Pipeline stopped: <code>.'; "
                    "the sentence is rebuilt in a running check's form"
                )
    if cb.get("status") == "cancelled":
        why = _cancelled_gap(name, path)
        if why is not None:
            return why
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
    ("review__delete_not_a_route.json", "error.code"): (
        "a route no SDK method calls: the original answered it without a JSON body"
    ),
    ("review__get_failed_every_assessment_failed.json", "dump.failure.hint"): "the server words the hint differently",
    ("review__get_failed_every_assessment_failed.json", "render"): "the same hint, printed by the CLI",
    ("review__get_failed_every_assessment_failed.json", "render_issues"): "the same hint, printed by the CLI",
    ("assess__single_one_claim.json", "render"): "the CLI no longer prints the deprecated `dissent`",
    ("extract__not_a_claim_beside_claims.json", "dump.status"): (
        "the newer shape answers `ready` when claims came back beside a non-claim"
    ),
    ("webhook__review_completed.json", "event.review.claims[0].verification.task_id"): (
        "the two recordings number their task ids differently (the newer payload has no delivery task_id)"
    ),
}


#: The fix line 3.0 gives a 404 (CHANGELOG "Changed"): 2.x said to retry.
_NOT_FOUND_FIX = (
    "Check the id or key the call names: nothing with it is visible to this credential. Retrying will not help."
)


def _intended(path: str, old: Any, new: Any, legacy: dict[str, Any]) -> str | None:
    """Why ``path`` changed on purpose in 3.0 (each listed in the CHANGELOG),
    or ``None``."""
    head, _, rest = path.partition(".")
    if head in ("error", "wait") and rest == "retryable" and old is MISSING and new in (True, False, None):
        return "3.0 sets `retryable` on every error"
    if legacy["status"] == 404 and head == "error":
        if rest == "type" and (old, new) == ("LenzError", "LenzNotFoundError"):
            return "3.0 raises LenzNotFoundError (a LenzError) for a 404"
        if rest in ("fix", "payload_json.error.fix") and new == _NOT_FOUND_FIX:
            return "3.0 says what to check on a 404 instead of advising a retry"
        if rest == "friendly_text" and isinstance(new, str) and new.endswith(_NOT_FOUND_FIX):
            return "the same fix line, printed by the CLI"
    return None


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


@pytest.mark.parametrize("name", [n for n in names() if n.startswith("webhook__")])
def test_original_shape_webhook_is_unchanged(name: str) -> None:
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
        (path, old, new)
        for path, old, new in _diff(expected, got)
        if _allowed(name, path, legacy, canonical) is None
        and _intended(path, old, new, legacy) is None
        and not (canonical["body"].get("status") == "cancelled" and _cancelled_render_gap(path, old, new))
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
    URL is unchanged too, except that since 3.2 /verify leaves out an empty
    ``source_url`` (it sent ``"source_url": ""``)."""
    from parity_requests import bodies

    assert bodies() == json.loads((FIXTURES / "requests.json").read_text())


# ── Schemas and pickles ─────────────────────────────────────────────────────


def test_model_schemas_match_the_previous_release() -> None:
    """Every model the previous release had publishes the same validation and
    serialization JSON schema: the newer names are properties, not fields."""
    from parity_static import schemas

    # Fields added since the frozen release. They are additive and optional
    # (a response without them parses as before), so they are left out of the
    # comparison; ``test_snippet_language_on_sources`` pins each one.
    added_since_frozen = {"snippet_language"}
    # ``language`` is a field of other models too, so it is dropped from
    # ``ExtractedClaims`` alone; ``test_extract_result_exposes_language`` pins it.
    added_to_models = {"ExtractedClaims": {"language"}}

    def _shape(schema: Any) -> Any:
        # Doc text (class docstrings) and deprecation markers may change; the
        # shape may not.
        if isinstance(schema, dict):
            return {
                k: _shape({n: p for n, p in v.items() if n not in added_since_frozen} if k == "properties" else v)
                for k, v in schema.items()
                if k not in ("description", "deprecated")
            }
        if isinstance(schema, list):
            return [_shape(v) for v in schema]
        return schema

    frozen = _shape(json.loads((FIXTURES / "schemas.json").read_text()))
    current = _shape(schemas())
    for key, schema in current.items():
        for gone in added_to_models.get(key.split(":")[0], ()):
            schema["properties"].pop(gone, None)
    assert {k: current.get(k) for k in frozen} == frozen


def test_objects_pickled_by_the_previous_release_still_dump() -> None:
    import base64
    import pickle

    frozen = json.loads((FIXTURES / "pickles.json").read_text())
    assert frozen
    for name, blob in frozen.items():
        model = pickle.loads(base64.b64decode(blob))
        assert model.model_dump(mode="json") == _expected(name)["dump"], name
