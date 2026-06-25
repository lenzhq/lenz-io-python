"""CLI test suite — mocks at the SDK boundary (a FakeClient), so no test hits
the network. Covers the plan's matrix: key resolution + precedence, config
perms, the no-key path, the ``--json`` success/error contract, the corrected
status codes, the ``verify`` lifecycle (poll → needs_input → select → verdict),
Ctrl-C resume handle, ``--resume`` expiry fallback, and the lazy-import guard.
"""

from __future__ import annotations

import json
import stat

import pytest
from typer.testing import CliRunner

import lenz_io.cli
import lenz_io.cli as cli_pkg
from lenz_io.cli import _run, normalize_argv
from lenz_io.cli import config as cfg
from lenz_io.cli import verify as verify_mod
from lenz_io.cli.app import app
from lenz_io.cli.errors import CLIError
from lenz_io.errors import (
    LenzAuthError,
    LenzQuotaExceededError,
    LenzRateLimitError,
)
from lenz_io.models import (
    AssessClaim,
    AssessResponse,
    CandidateClaim,
    ExtractedClaims,
    Source,
    TaskAccepted,
    TaskStatus,
    Verification,
)

runner = CliRunner()


# ── fakes ───────────────────────────────────────────────────────────────────
class _FakeAsk:
    def __init__(self, reply):
        self._reply = reply
        self.sent: list[tuple] = []

    def send(self, vid, *, message, language=""):
        self.sent.append((vid, message))
        return self._reply


class _FakeVerifications:
    def __init__(self, mapping=None, error=None):
        self._mapping = mapping or {}
        self._error = error

    def get(self, vid):
        if self._error:
            raise self._error
        return self._mapping[vid]


class FakeClient:
    """Scriptable stand-in for ``lenz_io.Lenz`` used by the CLI."""

    def __init__(
        self,
        *,
        extract_result=None,
        assess_result=None,
        verify_task=None,
        statuses=None,
        select_task=None,
        ask_reply=None,
        verifications=None,
        raises=None,
    ):
        self._extract = extract_result
        self._assess = assess_result
        self._verify_task = verify_task or TaskAccepted(task_id="t-1", status="queued")
        self._statuses = list(statuses or [])
        self._select_task = select_task or TaskAccepted(task_id="t-2", status="queued")
        self.ask = _FakeAsk(ask_reply)
        self.verifications = verifications or _FakeVerifications()
        self._raises = raises or {}
        self.verify_calls: list = []
        self.select_calls: list = []
        self.status_calls: list = []

    def _maybe_raise(self, name):
        if name in self._raises:
            raise self._raises[name]

    def extract(self, *, text, language=""):
        self._maybe_raise("extract")
        return self._extract

    def assess(self, *, text, language=""):
        self._maybe_raise("assess")
        return self._assess

    def verify(self, claim, **kwargs):
        self.verify_calls.append((claim, kwargs))
        return self._verify_task

    def get_status(self, task_id):
        self.status_calls.append(task_id)
        self._maybe_raise("get_status")
        return self._statuses.pop(0)

    def select(self, task_id, *, text="", claim_index=None):
        self.select_calls.append((task_id, text, claim_index))
        return self._select_task

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """No ambient key/env, config in a temp dir, instant polling."""
    monkeypatch.delenv("LENZ_API_KEY", raising=False)
    monkeypatch.delenv("LENZ_BASE_URL", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(cfg, "config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(verify_mod, "POLL_INTERVAL", 0)


def _patch_client(monkeypatch, fake):
    monkeypatch.setattr(_run, "build_client", lambda **kw: fake)
    return fake


# ── key resolution + config ─────────────────────────────────────────────────
def test_key_precedence_flag_over_env_over_file(monkeypatch, tmp_path):
    cfg.save_api_key("file_key")
    assert cfg.resolve_api_key(None) == ("file_key", "file")
    monkeypatch.setenv("LENZ_API_KEY", "env_key")
    assert cfg.resolve_api_key(None) == ("env_key", "env")
    assert cfg.resolve_api_key("flag_key") == ("flag_key", "flag")


def test_resolve_strips_whitespace_from_key(monkeypatch, tmp_path):
    """A pasted key with a trailing newline must not survive to the wire (it's an
    illegal header value → cryptic 400). Strip at every source."""
    monkeypatch.setenv("LENZ_API_KEY", "  lenz_padded\n")
    assert cfg.resolve_api_key(None) == ("lenz_padded", "env")
    monkeypatch.delenv("LENZ_API_KEY")
    cfg.save_api_key("lenz_filekey")
    # simulate a polluted on-disk value
    (tmp_path / "config.json").write_text(json.dumps({"api_key": "lenz_filekey\n "}))
    assert cfg.resolve_api_key(None) == ("lenz_filekey", "file")
    assert cfg.resolve_api_key("  flagkey ") == ("flagkey", "flag")


def test_save_api_key_perms_0600(tmp_path):
    path = cfg.save_api_key("secret")
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600
    assert json.loads(path.read_text())["api_key"] == "secret"


def test_corrupt_config_is_friendly(monkeypatch, tmp_path):
    (tmp_path / "config.json").write_text("{not json")
    with pytest.raises(cfg.ConfigError):
        cfg.resolve_api_key(None)


# ── global options accepted in any position (argv normalization) ─────────────
def test_normalize_argv_hoists_json_after_command():
    assert normalize_argv(["extract", "x", "--json"]) == ["--json", "extract", "x"]


def test_normalize_argv_before_command_unchanged():
    assert normalize_argv(["--json", "extract", "x"]) == ["--json", "extract", "x"]


def test_normalize_argv_value_option_carries_value():
    assert normalize_argv(["config", "--base-url", "https://s"]) == ["--base-url", "https://s", "config"]
    assert normalize_argv(["config", "--base-url=https://s"]) == ["--base-url=https://s", "config"]


def test_normalize_argv_respects_double_dash():
    # text that literally looks like a flag, after `--`, is left in place
    assert normalize_argv(["extract", "--", "--json"]) == ["extract", "--", "--json"]


def test_normalize_argv_leaves_command_options_in_place():
    assert normalize_argv(["verify", "claim", "--timeout", "5", "--json"]) == [
        "--json",
        "verify",
        "claim",
        "--timeout",
        "5",
    ]


def test_json_flag_after_command_produces_json(monkeypatch):
    # end-to-end: normalized argv flows through the app and yields JSON output
    monkeypatch.setenv("LENZ_API_KEY", "k")
    _patch_client(
        monkeypatch,
        FakeClient(assess_result=AssessResponse(claims=[AssessClaim(claim="c", verdict="True", confidence="high")])),
    )
    result = runner.invoke(app, normalize_argv(["assess", "c", "--json"]))
    assert result.exit_code == 0
    assert json.loads(result.stdout)["claims"][0]["verdict"] == "True"


def test_main_normalizes_argv_and_sets_no_color(monkeypatch):
    import importlib
    import os
    import sys
    import types

    ran = {}
    monkeypatch.setattr(
        importlib, "import_module", lambda name: types.SimpleNamespace(app=lambda: ran.setdefault("ok", True))
    )
    monkeypatch.setattr(sys, "argv", ["lenz", "extract", "x", "--no-color"])
    monkeypatch.delenv("NO_COLOR", raising=False)
    cli_pkg.main()
    assert ran["ok"] is True
    assert sys.argv == ["lenz", "--no-color", "extract", "x"]  # hoisted
    assert os.environ.get("NO_COLOR") == "1"


# ── `lenz help` (hidden alias) ───────────────────────────────────────────────
def test_help_command_shows_group_help():
    result = runner.invoke(app, ["help"])
    assert result.exit_code == 0
    assert "Usage:" in result.output


def test_help_command_shows_subcommand_help():
    result = runner.invoke(app, ["help", "verify"])
    assert result.exit_code == 0
    assert "--resume" in result.output


def test_help_command_unknown_is_friendly():
    result = runner.invoke(app, ["help", "nope"])
    assert result.exit_code == 2
    assert "No such command" in result.output


# ── read_text_arg: no-arg must not hang on an interactive terminal ───────────
def test_read_text_arg_tty_no_arg_errors_not_hangs(monkeypatch):
    class _Tty:
        def isatty(self):
            return True

        def read(self):
            raise AssertionError("must not block on stdin when no arg + interactive tty")

    monkeypatch.setattr("sys.stdin", _Tty())
    with pytest.raises(CLIError) as exc:
        _run.read_text_arg(None)
    assert exc.value.code == "no_input"
    assert exc.value.exit_code == 2


def test_read_text_arg_piped_still_reads(monkeypatch):
    import io

    class _Pipe(io.StringIO):
        def isatty(self):
            return False

    monkeypatch.setattr("sys.stdin", _Pipe("piped claim"))
    assert _run.read_text_arg(None) == "piped claim"


# ── corrupt config must not brick the whole CLI ──────────────────────────────
def test_corrupt_config_does_not_traceback(monkeypatch, tmp_path):
    (tmp_path / "config.json").write_text("{not json")
    result = runner.invoke(app, ["--json", "config"])
    assert result.exit_code == 0  # degraded, not a traceback (was exit 1)
    assert json.loads(result.stdout)["key_source"] == "none"


def test_logout_recovers_corrupt_config(monkeypatch, tmp_path):
    (tmp_path / "config.json").write_text("{not json")
    result = runner.invoke(app, ["logout"])
    assert result.exit_code == 0
    assert not cfg.config_path().exists()


def test_logout_clears_stored_key(monkeypatch):
    cfg.save_api_key("lenz_tokill")
    assert cfg.resolve_api_key(None) == ("lenz_tokill", "file")
    result = runner.invoke(app, ["logout"])
    assert result.exit_code == 0
    assert cfg.resolve_api_key(None) == ("", "none")


def test_logout_preserves_base_url(monkeypatch):
    cfg.save_api_key("lenz_tokill")
    path = cfg.config_path()
    path.write_text(json.dumps({"api_key": "lenz_tokill", "base_url": "https://staging.lenz.io/api/v1"}))
    runner.invoke(app, ["logout"])
    assert cfg.resolve_base_url(None) == "https://staging.lenz.io/api/v1"
    assert cfg.resolve_api_key(None) == ("", "none")


def test_logout_no_key_is_friendly(monkeypatch):
    # Idempotent: clearing when nothing is stored exits clean, not an error.
    result = runner.invoke(app, ["logout"])
    assert result.exit_code == 0


def test_logout_json_warns_env_shadow(monkeypatch):
    cfg.save_api_key("lenz_tokill")
    monkeypatch.setenv("LENZ_API_KEY", "lenz_envstillset")
    result = runner.invoke(app, ["--json", "logout"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "logged_out"
    assert payload["env_key_present"] is True


def test_config_command_masks_key(monkeypatch):
    monkeypatch.setenv("LENZ_API_KEY", "lenz_supersecretvalue")
    result = runner.invoke(app, ["--json", "config"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["key_source"] == "env"
    assert "supersecret" not in payload["api_key"]
    assert payload["api_key"].startswith("lenz_s")


# ── no-key path ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("cmd", [["extract", "x"], ["assess", "x"], ["verify", "x"]])
def test_no_key_is_friendly_not_traceback(monkeypatch, cmd):
    _patch_client(monkeypatch, FakeClient())
    result = runner.invoke(app, ["--json", *cmd])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "no_api_key"
    assert "lenz login" in payload["error"]["fix"]


# ── --json success contract ─────────────────────────────────────────────────
def test_extract_json_success(monkeypatch):
    monkeypatch.setenv("LENZ_API_KEY", "k")
    _patch_client(
        monkeypatch,
        FakeClient(extract_result=ExtractedClaims(identified_claims=["Earth is round", "Sky is blue"])),
    )
    result = runner.invoke(app, ["--json", "extract", "some text"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["identified_claims"] == ["Earth is round", "Sky is blue"]


def test_extract_reads_stdin(monkeypatch):
    monkeypatch.setenv("LENZ_API_KEY", "k")
    _patch_client(monkeypatch, FakeClient(extract_result=ExtractedClaims(identified_claims=["c"])))
    result = runner.invoke(app, ["--json", "extract", "-"], input="piped text")
    assert result.exit_code == 0


def test_extract_pretty_renders_atomic_claim():
    """Single-claim input fills atomic_claim, not identified_claims — the pretty
    renderer must surface it (regression: it used to print 'no claims found')."""
    import io

    from rich.console import Console

    from lenz_io.cli.render import Output, render_extract

    # extra="allow" carries atomic_claim/domain through model_validate.
    extracted = ExtractedClaims.model_validate(
        {
            "domain": "History",
            "atomic_claim": "Einstein won the 1921 Nobel Prize in Physics.",
            "key_entities": [{"name": "Albert Einstein", "type": "person"}],
        }
    )
    buf = io.StringIO()
    out = Output(json_mode=False, no_color=True)
    out.json_mode = False  # force pretty even though the test stdout isn't a tty
    out.console = Console(file=buf, no_color=True, width=100)
    render_extract(out, extracted)
    text = buf.getvalue()
    assert "Einstein won the 1921 Nobel Prize" in text
    assert "No verifiable claim" not in text
    assert "History" in text


def test_extract_pretty_multi_claim_includes_primary():
    """The server puts the primary claim in atomic_claim and extras in
    identified_claims — the rendered list must include BOTH (regression: the
    primary used to be dropped from the list and only shown in the verify hint)."""
    import io

    from rich.console import Console

    from lenz_io.cli.render import Output, render_extract

    extracted = ExtractedClaims.model_validate(
        {
            "domain": "Science",
            "atomic_claim": "The Earth is flat.",
            "identified_claims": ["Ruby is harder than diamond."],
        }
    )
    buf = io.StringIO()
    out = Output(json_mode=False, no_color=True)
    out.json_mode = False
    out.console = Console(file=buf, no_color=True, width=100)
    render_extract(out, extracted)
    text = buf.getvalue()
    assert "The Earth is flat." in text  # primary not dropped
    assert "Ruby is harder than diamond." in text
    assert "2 claim" in text  # counted as two, not one
    # no single 'Verify it:' hint when there are multiple claims (it was orphaned)
    assert "Verify it:" not in text


def test_assess_json_success(monkeypatch):
    monkeypatch.setenv("LENZ_API_KEY", "k")
    _patch_client(
        monkeypatch,
        FakeClient(assess_result=AssessResponse(claims=[AssessClaim(claim="c", verdict="False", confidence="high")])),
    )
    result = runner.invoke(app, ["--json", "assess", "c"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["claims"][0]["verdict"] == "False"


# ── --json error contract + corrected status codes ──────────────────────────
@pytest.mark.parametrize(
    "exc,code,status",
    [
        (LenzQuotaExceededError(message="no credits", status_code=403), "no_credits", 403),
        (LenzAuthError(message="bad key", status_code=401), "unauthorized", 401),
        (LenzRateLimitError(message="slow down", status_code=429), "rate_limited", 429),
    ],
)
def test_error_mapping_json(monkeypatch, exc, code, status):
    monkeypatch.setenv("LENZ_API_KEY", "k")
    _patch_client(monkeypatch, FakeClient(raises={"extract": exc}))
    result = runner.invoke(app, ["--json", "extract", "x"])
    assert result.exit_code == 1
    err = json.loads(result.stdout)["error"]
    assert err["code"] == code
    assert err["status"] == status


def test_ask_bad_id_is_friendly_not_retry(monkeypatch):
    """A 404 from ask means a wrong/expired id — say so, don't tell the user to
    'retry and file an issue' (the generic LenzError fix)."""
    monkeypatch.setenv("LENZ_API_KEY", "k")
    fake = FakeClient()
    fake.ask = _FakeAsk(None)

    def _raise(vid, *, message, language=""):
        raise LenzAuthError(message="Verification not found.", status_code=404)

    fake.ask.send = _raise
    _patch_client(monkeypatch, fake)
    result = runner.invoke(app, ["--json", "ask", "435100acc5464911", "what else?"])
    assert result.exit_code == 1
    err = json.loads(result.stdout)["error"]
    assert err["code"] == "not_found"
    assert "verification_id" in err["message"]
    assert "task_id" in err["message"]


# ── verify lifecycle ────────────────────────────────────────────────────────
def _verification():
    return Verification(
        verification_id="v-1",
        verdict="False",
        confidence="high",
        lenz_score=1,
        executive_summary="Nope.",
        sources=[Source(title="A", url="https://a.test"), Source(title="B", url="https://b.test")],
    )


def test_verify_happy_path(monkeypatch):
    monkeypatch.setenv("LENZ_API_KEY", "k")
    fake = _patch_client(
        monkeypatch,
        FakeClient(statuses=[TaskStatus(status="completed", result=_verification())]),
    )
    result = runner.invoke(app, ["--json", "verify", "claim"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["verdict"] == "False"
    # idempotency key is sent on submit (avoids dup paid pipeline on retry)
    assert "idempotency_key" in fake.verify_calls[0][1]


def test_verify_multi_claim_with_preselect(monkeypatch):
    monkeypatch.setenv("LENZ_API_KEY", "k")
    fake = _patch_client(
        monkeypatch,
        FakeClient(
            statuses=[
                TaskStatus(
                    status="needs_input",
                    reason="multi_claim",
                    claims=[CandidateClaim(text="claim A"), CandidateClaim(text="claim B")],
                ),
                TaskStatus(status="completed", result=_verification()),
            ],
        ),
    )
    # --claim 2 (1-based) → select by the claim's TEXT (the server's /select only
    # accepts ``text``; sending claim_index 422s). No hang, verdict rendered.
    result = runner.invoke(app, ["--json", "verify", "blob", "--claim", "2"])
    assert result.exit_code == 0
    assert fake.select_calls == [("t-1", "claim B", None)]
    assert json.loads(result.stdout)["verdict"] == "False"


def test_verify_json_needs_input_emits_and_exits(monkeypatch):
    monkeypatch.setenv("LENZ_API_KEY", "k")
    _patch_client(
        monkeypatch,
        FakeClient(
            statuses=[
                TaskStatus(
                    status="needs_input",
                    reason="multi_claim",
                    claims=[CandidateClaim(text="A"), CandidateClaim(text="B")],
                ),
            ],
        ),
    )
    # No --claim and json mode → emit the needs_input object, exit 3, never hang
    result = runner.invoke(app, ["--json", "verify", "blob"])
    assert result.exit_code == 3
    payload = json.loads(result.stdout)
    assert payload["status"] == "needs_input"
    assert payload["reason"] == "multi_claim"
    assert len(payload["claims"]) == 2


def test_verify_failed(monkeypatch):
    monkeypatch.setenv("LENZ_API_KEY", "k")
    _patch_client(monkeypatch, FakeClient(statuses=[TaskStatus(status="failed", error="pipeline boom")]))
    result = runner.invoke(app, ["--json", "verify", "claim"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "pipeline_failed"


def test_resume_expired_task_is_friendly(monkeypatch):
    monkeypatch.setenv("LENZ_API_KEY", "k")
    fake = FakeClient(
        raises={"get_status": LenzAuthError(message="Task not found.", status_code=404)},
        verifications=_FakeVerifications(error=LenzAuthError(message="not found", status_code=404)),
    )
    _patch_client(monkeypatch, fake)
    result = runner.invoke(app, ["--json", "verify", "--resume", "t-gone"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "not_found"


def test_resume_falls_back_to_verification_id(monkeypatch):
    monkeypatch.setenv("LENZ_API_KEY", "k")
    fake = FakeClient(
        raises={"get_status": LenzAuthError(message="Task not found.", status_code=404)},
        verifications=_FakeVerifications(mapping={"v-1": _verification()}),
    )
    _patch_client(monkeypatch, fake)
    result = runner.invoke(app, ["--json", "verify", "--resume", "v-1"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["verdict"] == "False"


# ── lazy-import guard ───────────────────────────────────────────────────────
def test_lazy_import_guard(monkeypatch, capsys):
    import importlib

    def _raise(name):
        raise ModuleNotFoundError(name="typer")

    monkeypatch.setattr(importlib, "import_module", _raise)
    with pytest.raises(SystemExit) as exc:
        lenz_io.cli.main()
    assert exc.value.code == 1
    assert "lenz-io[cli]" in capsys.readouterr().err
