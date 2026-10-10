"""What the SDK and the CLI make of one response, as plain JSON.

Shared by ``tests/test_parity.py`` and ``tests/parity_generate.py``. It uses
only the public surface that existed before this SDK read the newer response
shape, so the generator can run it against the previous release's code and
freeze the result (``tests/fixtures/parity/expected/``): the frozen output is
the oracle both response shapes are held to.

Fixture names are ``<area>__<case>.json``; the area picks what is observed.
"""

from __future__ import annotations

import contextlib
import inspect
import io
import json
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.console import Console

import lenz_io.cli.render as render_mod
from lenz_io import errors as errors_mod, models
from lenz_io.cli import citecheck as citecheck_cli, review as review_cli
from lenz_io.cli.errors import friendly_text, to_payload
from lenz_io.client import Lenz
from lenz_io.webhooks import parse_webhook

FIXTURES = Path(__file__).parent / "fixtures" / "parity"
MISSING = "<missing>"

#: The time the CLI's relative reset dates are rendered against.
FROZEN_NOW = datetime(2026, 10, 2, 12, 0, 0, tzinfo=timezone.utc)

#: Every attribute an SDK exception may carry, read when present.
_ERROR_ATTRS = (
    "message",
    "cause",
    "fix",
    "doc_url",
    "request_id",
    "status_code",
    "code",
    "retry_after",
    "limit",
    "reset_in_seconds",
    "upgrade_url",
    "remaining",
    "resets_at",
    "requested",
    "credit_balance",
    "cost",
    "errors",
    "task_id",
    "status",
    "hint",
    "failure_reason",
    "failure_class",
    "retryable",
    "purged_at",
    "kind",
    "payload",
)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz: Any = None) -> Any:  # type: ignore[override]
        return FROZEN_NOW if tz else FROZEN_NOW.replace(tzinfo=None)


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return json.loads(json.dumps(value, default=str))


def attr_view(obj: Any, template: Any) -> Any:
    """Read ``obj`` by ATTRIBUTE along the keys of ``template`` (a dump).

    A dump shows only what a model serialises; this reads what code reading
    the attributes gets, so a newer-shape response can be held to the dump the
    original shape produced.
    """
    if isinstance(template, dict):
        out = {}
        for key, sub in template.items():
            if isinstance(obj, dict):
                value = obj.get(key, MISSING)
            else:
                value = getattr(obj, key, MISSING)
            out[key] = MISSING if value is MISSING else attr_view(value, sub)
        return out
    if isinstance(template, list):
        if not isinstance(obj, (list, tuple)) or len(obj) != len(template):
            return _jsonable(obj)
        items = list(obj)
        return [attr_view(o, t) for o, t in zip(items, template, strict=True)]
    return _jsonable(obj)


def _console() -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    return Console(file=buf, no_color=True, width=120, highlight=False, force_terminal=False), buf


def _render(fn: Any, *args: Any, json_mode: bool = False, **kwargs: Any) -> str:
    out = render_mod.Output(json_mode=json_mode, no_color=True)
    out.json_mode = json_mode
    out.console, buf = _console()
    out.err, _ = _console()
    stdout = io.StringIO()
    real_dt = render_mod.datetime
    render_mod.datetime = _FrozenDatetime  # type: ignore[misc]
    try:
        with contextlib.redirect_stdout(stdout):
            fn(out, *args, **kwargs)
    except SystemExit as exc:
        buf.write(f"<exit {exc.code}>")
    finally:
        render_mod.datetime = real_dt  # type: ignore[misc]
    return buf.getvalue() + stdout.getvalue()


def _error_view(err: BaseException) -> dict[str, Any]:
    view: dict[str, Any] = {"type": type(err).__name__}
    for name in _ERROR_ATTRS:
        if name in vars(err) or hasattr(type(err), name):
            view[name] = _jsonable(getattr(err, name))
    view["friendly_text"] = friendly_text(err)
    view["payload_json"] = _jsonable(to_payload(err))
    return view


def endpoint_for(name: str) -> tuple[str, str]:
    """The ``(method, path)`` a recorded error answered, as precisely as the
    error mapping needs it: /review and /citecheck, /assess, or any other."""
    area, case = name.split("__", 1)
    if area == "review":
        return ("GET", "/reviews/r1") if case.startswith("get_") else ("POST", "/review")
    if area == "citecheck":
        return ("GET", "/citechecks/c1") if case.startswith("get_") else ("POST", "/citecheck")
    if area == "assess":
        return ("POST", "/assess")
    if area == "extract":
        return ("POST", "/extract")
    if area == "errors" and case.startswith("ask_"):
        return ("POST", "/ask/v1")
    return ("POST", "/verify")


def _model_for(name: str) -> Any:
    area = name.split("__", 1)[0]
    case = name.split("__", 1)[1]
    if area == "extract":
        return models.ExtractedClaims
    if area == "assess":
        return models.AssessResponse
    if area == "account" and case.startswith("me_usage"):
        return models.Usage
    if area == "account" and case.startswith("library"):
        return models.LibraryList
    if area == "review":
        return (
            models.ReviewStarted
            if case.startswith(("receipt", "idempotent", "replay", "stored_replay"))
            else models.ReviewFull
        )
    if area == "citecheck":
        return models.CitecheckStarted if case.startswith(("receipt", "idempotent")) else models.Citecheck
    if area == "verify":
        if case.startswith(("status_", "stored_progress")):
            return models.TaskStatus
        if case.startswith(("batch_", "select_")):
            return models.BatchAccepted
        if case.startswith("list"):
            return models.VerificationList
        if case.startswith("verification_"):
            return models.Verification
        return models.TaskAccepted
    return None


def observe(name: str, fixture: dict[str, Any]) -> dict[str, Any]:
    """Everything the SDK and the CLI produce from one recorded response."""
    status, body = fixture["status"], fixture["body"]
    headers = {k: str(v) for k, v in (fixture.get("headers") or {}).items()}
    result: dict[str, Any] = {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        if name.startswith("webhook__"):
            event = parse_webhook(body)
            view: dict[str, Any] = {"type": type(event).__name__}
            for key, value in vars(event).items():
                if key != "raw":
                    view[key] = _jsonable(value)
            result["event"] = view
            return result
        if status >= 400:
            kwargs: dict[str, Any] = {}
            if "endpoint" in inspect.signature(errors_mod.map_response_to_error).parameters:
                # The request the client sends, as far as the mapping reads it
                # (releases before the newer shape take no endpoint).
                kwargs["endpoint"] = endpoint_for(name)
            err = errors_mod.map_response_to_error(status, json.dumps(body).encode(), headers, **kwargs)
            result["error"] = _error_view(err)
            return result
        cls = _model_for(name)
        if cls is None:
            return result
        model = _extract_via_client(name, body) if cls is models.ExtractedClaims else cls.model_validate(body)
        result["dump"] = model.model_dump(mode="json")
        result["dump_unset"] = model.model_dump(mode="json", exclude_unset=True)
        result["repr"] = repr(model)
        result["render_json"] = None
        if cls is models.ExtractedClaims:
            result["render"] = _render(render_mod.render_extract, model)
            result["render_json"] = _render(render_mod.render_extract, model, json_mode=True)
        elif cls is models.AssessResponse:
            result["render"] = _render(render_mod.render_assess, model)
            result["render_json"] = _render(render_mod.render_assess, model, json_mode=True)
        elif cls is models.Usage:
            result["render"] = _render(render_mod.render_usage, model)
            result["render_json"] = _render(render_mod.render_usage, model, json_mode=True)
        elif cls is models.Verification:
            result["render"] = _render(render_mod.render_verification_full, model)
            result["render_concise"] = _render(render_mod.render_verification, model)
            result["render_json"] = _render(render_mod.render_verification_full, model, json_mode=True)
        elif cls is models.TaskStatus:
            result["render"] = _render(render_mod.render_task_status, model, task_id="t")
            result["render_json"] = _render(render_mod.render_task_status, model, task_id="t", json_mode=True)
            client = Lenz(api_key="lenz_" + "0" * 32)
            try:
                verification = client._verification_from_terminal(model, "t")
                result["wait"] = {"returned": verification.model_dump(mode="json")}
            except errors_mod.LenzError as exc:
                result["wait"] = _error_view(exc)
            finally:
                client.close()
            if model.status == "completed" and model.result is not None:
                result["render_verification"] = _render(render_mod.render_verification_full, model.result)
        elif cls is models.ReviewFull:
            result["render"] = _render(review_cli.render_review, model)
            result["render_issues"] = _render(review_cli.render_review, model, issues_only=True)
            result["exit_code"] = review_cli.exit_code_for_review(model)
        elif cls is models.Citecheck:
            result["render"] = _render(citecheck_cli.render_citecheck, model)
            result["exit_code"] = citecheck_cli.exit_code_for_citecheck(model)
    return result


def located(name: str) -> bool:
    """Whether the recorded /extract call asked to locate its claims."""
    return "locate" in name


def _extract_via_client(name: str, body: dict[str, Any]) -> Any:
    """``client.extract`` on the recorded body, with the recorded ``locate``."""
    import respx

    from lenz_io.client import DEFAULT_BASE_URL

    client = Lenz(api_key="lenz_" + "0" * 32)
    try:
        with respx.mock(base_url=DEFAULT_BASE_URL) as mock:
            mock.post("/extract").respond(200, json=body)
            return client.extract(text="text", locate=True if located(name) else None)
    finally:
        client.close()


def load(shape: str, name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / shape / name).read_text())


def names() -> list[str]:
    return sorted(p.name for p in (FIXTURES / "legacy").glob("*.json"))
