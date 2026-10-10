"""Every successful contract body the API can send, read through the SDK.

``fixtures/goldens/{canonical,legacy}/`` holds the API's own contract goldens
(synthetic data): each response body that a 2xx carries, in both API
versions. Each is read through the client's real parse path for its
endpoint, in both ``legacy_aliases`` modes, and must not raise: a field the
API sends as ``null`` must read as ``None``, never a ``ValidationError``.
The legacy shape still reaches the models through webhooks and stored
replays.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from lenz_io import models
from lenz_io._core import _extracted
from lenz_io.models import LEGACY_ALIASES

GOLDENS = Path(__file__).parent / "fixtures" / "goldens"

#: (name prefix, how the client reads that endpoint's body). First match wins.
_PARSERS: list[tuple[str, Callable[[Any, Any], Any]]] = [
    ("account__ask_history", lambda b, c: models.AskHistory.model_validate(b, context=c)),
    ("account__ask_send", lambda b, c: models.AskReply.model_validate(b, context=c)),
    ("account__library", lambda b, c: models.LibraryList.model_validate(b, context=c)),
    ("account__me_usage", lambda b, c: models.Usage.model_validate(b, context=c)),
    ("assess__", lambda b, c: models.AssessResponse.model_validate(b, context=c)),
    ("citecheck__get_", lambda b, c: models.Citecheck.model_validate(b, context=c)),
    ("citecheck__cancel_", lambda b, c: models.Citecheck.model_validate(b, context=c)),
    ("citecheck__", lambda b, c: models.CitecheckStarted.model_validate(b, context=c)),
    ("extract__", lambda b, c: _extracted(b, locate=None, context=c)),
    ("review__get_completed_issues_view", lambda b, c: models.ReviewIssues.model_validate(b, context=c)),
    ("review__get_citations_issue_issues_view", lambda b, c: models.ReviewIssues.model_validate(b, context=c)),
    ("review__get_suggested_edits_issues_view", lambda b, c: models.ReviewIssues.model_validate(b, context=c)),
    ("review__get_", lambda b, c: models.ReviewFull.model_validate(b, context=c)),
    ("review__cancel_", lambda b, c: models.ReviewFull.model_validate(b, context=c)),
    ("review__", lambda b, c: models.ReviewStarted.model_validate(b, context=c)),
    ("verify__batch_", lambda b, c: models.BatchAccepted.model_validate(b, context=c)),
    ("verify__select_", lambda b, c: models.BatchAccepted.model_validate(b, context=c)),
    ("verify__cancel_", lambda b, c: models.CancelResult.model_validate(b, context=c)),
    ("verify__list", lambda b, c: models.VerificationList.model_validate(b, context=c)),
    ("verify__status_", lambda b, c: models.TaskStatus.model_validate(b, context=c)),
    ("verify__stored_progress_", lambda b, c: models.TaskStatus.model_validate(b, context=c)),
    ("verify__verification_", lambda b, c: models.Verification.model_validate(b, context=c)),
    ("verify__", lambda b, c: models.TaskAccepted.model_validate(b, context=c)),
]
#: 2xx bodies the SDK reads no model from (a root document, ``{"ok": true}``).
_NO_MODEL = ("account__api_", "account__ask_reset", "verify__delete_")

_CASES = sorted((d.name, f.name) for d in GOLDENS.iterdir() if d.is_dir() for f in d.glob("*.json"))


#: The goldens' placeholders for values that change per run: ``<name#n:kind>``.
_PLACEHOLDER = re.compile(r"^<([a-z_]+)#(\d+):([a-z0-9]+)(?::([^>]*))?>$")


def _filled(value: Any) -> Any:
    """``value`` with each placeholder replaced by a value of its kind."""
    if isinstance(value, dict):
        return {k: _filled(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_filled(v) for v in value]
    match = _PLACEHOLDER.match(value) if isinstance(value, str) else None
    if match is None:
        return value
    _, n, kind, _rest = match.groups()
    if kind == "int":
        return int(n)
    if kind == "ts":
        return f"2026-10-0{int(n) % 9 + 1}T12:00:00+00:00"
    width = int(kind[3:]) if kind.startswith("hex") else 8
    return format(int(n), "x").rjust(width, "a")


def _parser(name: str) -> Callable[[Any, Any], Any] | None:
    if name.startswith(_NO_MODEL):
        return None
    return next((parse for prefix, parse in _PARSERS if name.startswith(prefix)), None)


def test_the_goldens_are_there() -> None:
    assert len([c for c in _CASES if c[0] == "canonical"]) > 150
    assert len([c for c in _CASES if c[0] == "legacy"]) > 150


@pytest.mark.parametrize("legacy_aliases", [True, False], ids=["aliases", "as_sent"])
@pytest.mark.parametrize(("version", "name"), _CASES)
def test_every_golden_body_reads(version: str, name: str, legacy_aliases: bool) -> None:
    parse = _parser(name)
    if parse is None:
        pytest.skip("no model is read from this body")
    body = _filled(json.loads((GOLDENS / version / name).read_text())["body"])
    context = None if legacy_aliases else {LEGACY_ALIASES: False}
    result = parse(body, context)
    # Every property is readable too (the current names are read lazily).
    for attr in dir(type(result)):
        if isinstance(getattr(type(result), attr, None), property) and not attr.startswith("_"):
            getattr(result, attr)
    assert result.raw == body or isinstance(result, models.ExtractedClaims)


_NULLS = [
    ("CandidateClaim", {"claim": "A.", "text": None}, "text", ""),
    ("EntityRef", {"name": None}, "name", ""),
    ("AssessClaim", {"claim": "A.", "verdict": None, "confidence": None}, "confidence", "low"),
    ("AssessResponse", {"claims": [], "error": None, "error_code": None}, "error_code", ""),
    ("TaskStatus", {"status": "processing", "task_id": "t", "reason": None, "hint": None}, "hint", ""),
    ("TaskStatus", {"status": "completed", "task_id": "t", "claims": None, "docs_url": None}, "docs_url", ""),
]


@pytest.mark.parametrize(("model", "body", "field", "default"), _NULLS)
def test_a_null_the_api_can_send_reads_as_unsent(model: str, body: dict[str, Any], field: str, default: Any) -> None:
    cls = getattr(models, model)
    assert getattr(cls.model_validate(body), field) == default
    assert getattr(cls.model_validate(body, context={LEGACY_ALIASES: False}), field) is None


def test_a_null_in_a_required_answer_is_still_refused() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        models.AskReply.model_validate({"content": None, "message_id": 5})
