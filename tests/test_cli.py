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
    BatchAccepted,
    CandidateClaim,
    ExtractedClaims,
    SimilarVerification,
    Source,
    TaskAccepted,
    TaskStatus,
    Usage,
    UsageCapacity,
    UsageExtract,
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
        usage_result=None,
        raises=None,
    ):
        self._extract = extract_result
        self._assess = assess_result
        self._verify_task = verify_task or TaskAccepted(task_id="t-1", status="queued")
        self._statuses = list(statuses or [])
        # None → select() builds one task per selected text (realistic batch
        # fan-out); pass select_task to override with a fixed BatchAccepted.
        self._select_task = select_task
        self.ask = _FakeAsk(ask_reply)
        self.verifications = verifications or _FakeVerifications()
        self._usage = usage_result
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

    def select(self, task_id, *, texts):
        self.select_calls.append((task_id, texts))
        if self._select_task is not None:
            return self._select_task
        return BatchAccepted(
            batch_id="b-1",
            items=[TaskAccepted(task_id=f"sel-{i + 1}", claim_text=t) for i, t in enumerate(texts)],
        )

    def usage(self):
        self._maybe_raise("usage")
        return self._usage

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


def test_build_client_overrides_only_user_agent():
    """The CLI sets a distinct UA via the SDK's user_agent= param while the SDK
    keeps ownership of its other default headers (so a new one can't be dropped),
    and owns/closes the client (no injected-client leak)."""
    from lenz_io.cli.client import build_client, cli_user_agent

    c = build_client(api_key="k", base_url="")
    assert c._client.headers["User-Agent"] == cli_user_agent()
    assert c._client.headers["X-Lenz-API-Version"]  # SDK default preserved, not hand-copied
    assert c._owns_client is True  # close() will actually close it
    c.close()


def test_normalize_argv_value_opt_does_not_swallow_a_flag():
    # `--api-key` with a flag-looking next token must NOT consume it as its value
    # (that silently loses the --json flag + makes the key the string "--json").
    # Leave --api-key in place so Click reports a clear error; --json still hoists.
    assert normalize_argv(["verify", "--api-key", "--json", "claim"]) == [
        "--json",
        "verify",
        "--api-key",
        "claim",
    ]


def test_normalize_argv_trailing_value_opt_with_no_value():
    # `--api-key` at the very end (forgot the value) is left in place, not hoisted
    # bare, so Click errors at the right spot instead of a confusing front error.
    assert normalize_argv(["extract", "text", "--api-key"]) == ["extract", "text", "--api-key"]


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


def _strip_ansi(s: str) -> str:
    import re

    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def test_help_command_shows_subcommand_help():
    result = runner.invoke(app, ["help", "verify"])
    assert result.exit_code == 0
    # Strip ANSI: with color on (e.g. CI), Rich splits option names with escape
    # codes so "--resume" isn't a raw substring — assert on the rendered text.
    assert "--resume" in _strip_ansi(result.output)


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


# ── render layer (pretty / TTY output) ───────────────────────────────────────
def _render(fn, *args):
    """Run a render_* function in pretty mode, capturing both consoles."""
    import io

    from rich.console import Console

    from lenz_io.cli.render import Output

    buf = io.StringIO()
    out = Output(json_mode=False, no_color=True)
    out.json_mode = False
    out.console = Console(file=buf, no_color=True, width=100)
    out.err = Console(file=buf, no_color=True, width=100)
    fn(out, *args)
    return buf.getvalue()


def test_render_assess_shows_verdict_and_ask_hint():
    from lenz_io.cli.render import render_assess

    resp = AssessResponse(
        claims=[
            AssessClaim(
                claim="The sky is green",
                verdict="False",
                confidence="high",
                verification_url="https://lenz.io/api/v1/verifications/abcd1234",
            )
        ]
    )
    text = _render(render_assess, resp)
    assert "False" in text and "The sky is green" in text
    assert "lenz ask abcd1234" in text  # follow-up hint with the parsed id


def test_render_assess_no_claims():
    from lenz_io.cli.render import render_assess

    assert "No claims assessed" in _render(render_assess, AssessResponse(claims=[]))


def test_render_verification_full():
    from lenz_io.cli.render import render_verification

    text = _render(render_verification, _verification())
    assert "False" in text  # verdict
    assert "Nope." in text  # executive summary
    assert "https://a.test" in text  # a source url
    assert "verification_id: v-1" in text
    assert "lenz ask v-1" in text  # follow-up hint


def test_render_verification_none_errors():
    from lenz_io.cli.render import render_verification

    with pytest.raises(SystemExit):
        _render(render_verification, None)


def test_render_ask_renders_markdown_not_literal():
    from types import SimpleNamespace

    from lenz_io.cli.render import render_ask

    text = _render(render_ask, SimpleNamespace(content="The **600 Nm** at *6,500 rpm*."))
    assert "600 Nm" in text
    assert "**" not in text  # markdown rendered, not dumped literally


def test_render_ask_empty_reply():
    from types import SimpleNamespace

    from lenz_io.cli.render import render_ask

    assert "empty reply" in _render(render_ask, SimpleNamespace(content=""))


def test_render_config_shows_source_and_masked_key():
    from lenz_io.cli.render import render_config

    text = _render(
        render_config,
        {
            "key_source": "file",
            "api_key": "lenz_x…1234",
            "base_url": "https://lenz.io/api/v1",
            "config_file": "/tmp/c.json",
        },
    )
    assert "file" in text and "lenz_x…1234" in text and "https://lenz.io/api/v1" in text


def test_render_extract_no_claim_found():
    from lenz_io.cli.render import render_extract

    text = _render(render_extract, ExtractedClaims.model_validate({"identified_claims": [], "atomic_claim": ""}))
    assert "No verifiable claim found" in text


def test_render_extract_ambiguous_candidates():
    from lenz_io.cli.render import render_extract

    extracted = ExtractedClaims.model_validate(
        {"atomic_claim": "X is Y", "candidate_claims": ["did you mean A?", "or B?"]}
    )
    text = _render(render_extract, extracted)
    assert "candidate readings" in text
    assert "did you mean A?" in text


def test_assess_json_success(monkeypatch):
    monkeypatch.setenv("LENZ_API_KEY", "k")
    _patch_client(
        monkeypatch,
        FakeClient(assess_result=AssessResponse(claims=[AssessClaim(claim="c", verdict="False", confidence="high")])),
    )
    result = runner.invoke(app, ["--json", "assess", "c"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["claims"][0]["verdict"] == "False"


def _usage(**kw):
    """Build a Usage with the nested per-capability shape (verify/ask/assess/extract)."""
    verify = UsageCapacity(**kw.pop("verify", {}))
    ask = UsageCapacity(**kw.pop("ask", {}))
    assess = UsageCapacity(**kw.pop("assess", {}))
    extract = UsageExtract(**kw.pop("extract", {}))
    return Usage(verify=verify, ask=ask, assess=assess, extract=extract, **kw)


def test_usage_json_success(monkeypatch):
    monkeypatch.setenv("LENZ_API_KEY", "k")
    _patch_client(
        monkeypatch,
        FakeClient(
            usage_result=_usage(
                plan="developer",
                quota_resets_at="2026-07-01",
                verify={"quota_used": 120, "quota_total": 500, "quota_remaining": 380, "credits": 25, "remaining": 405},
                ask={"quota_used": 10, "quota_total": 200, "quota_remaining": 190, "remaining": 190},
                assess={"quota_used": 45, "quota_total": 1000, "quota_remaining": 955, "remaining": 955},
                extract={"calls_today": 3, "daily_limit": 1000},
            )
        ),
    )
    result = runner.invoke(app, ["--json", "usage"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["plan"] == "developer"
    assert payload["verify"]["remaining"] == 405
    assert payload["verify"]["credits"] == 25
    assert payload["ask"]["quota_total"] == 200
    assert payload["assess"]["remaining"] == 955
    assert payload["assess"]["credits"] == 0
    assert payload["extract"]["calls_today"] == 3


def test_usage_pretty_shows_bonus_breakdown():
    """Human render: per-capability 'remaining left' headline + quota breakdown,
    with a '+ N bonus' tail when the key holds top-up credits. CliRunner forces
    json_mode (non-tty stdout), so render directly like other pretty tests."""
    import io

    from rich.console import Console

    from lenz_io.cli.render import Output, render_usage

    buf = io.StringIO()
    out = Output(json_mode=False, no_color=True)
    out.json_mode = False  # force pretty even though the test stdout isn't a tty
    out.console = Console(file=buf, no_color=True, width=100)
    render_usage(
        out,
        _usage(
            plan="developer",
            quota_resets_at="2026-07-01",
            verify={"quota_used": 120, "quota_total": 500, "quota_remaining": 380, "credits": 25, "remaining": 405},
            ask={"quota_used": 10, "quota_total": 200, "quota_remaining": 190, "remaining": 190},
            assess={"quota_used": 45, "quota_total": 1000, "quota_remaining": 955, "remaining": 955},
            extract={"calls_today": 3, "daily_limit": 1000},
        ),
    )
    text = buf.getvalue()
    assert "developer plan" in text
    assert "Verify:" in text
    assert "405 left" in text  # quota_remaining + bonus
    assert "120 / 500 quota + 25 bonus" in text  # bonus tail present
    assert "Ask:" in text
    assert "190 left" in text
    assert "Assess:" in text
    assert "955 left" in text  # quota-only capability, no bonus tail
    # Row order: Verify → Ask → Assess → Extract
    assert text.index("Verify:") < text.index("Ask:") < text.index("Assess:") < text.index("Extract:")
    # No bonus tail on Ask or Assess (credits==0 for both)
    assert "bonus" not in text.split("Ask:")[1].split("Extract")[0]
    assert "Quota resets 2026-07-01" in text


def test_usage_pretty_unlimited_extract():
    """unlimited extract → 'unlimited', not 'N / 0 today'."""
    import io

    from rich.console import Console

    from lenz_io.cli.render import Output, render_usage

    buf = io.StringIO()
    out = Output(json_mode=False, no_color=True)
    out.json_mode = False
    out.console = Console(file=buf, no_color=True, width=100)
    render_usage(out, _usage(plan="scale", extract={"unlimited": True}))
    text = buf.getvalue()
    assert "Extract:" in text
    assert "unlimited" in text
    assert "/ 0 today" not in text


def test_usage_needs_key(monkeypatch):
    _patch_client(monkeypatch, FakeClient())
    result = runner.invoke(app, ["--json", "usage"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "no_api_key"


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
    # --claim 2 (1-based) → select by the claim's TEXT as a one-element list
    # (/select is list-only and fans out one pipeline per pick). No hang.
    result = runner.invoke(app, ["--json", "verify", "blob", "--claim", "2"])
    assert result.exit_code == 0
    assert fake.select_calls == [("t-1", ["claim B"])]
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


# ── login (was zero coverage) ────────────────────────────────────────────────
def test_login_saves_flag_key(monkeypatch):
    result = runner.invoke(app, ["--api-key", "lenz_flagkey", "login"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "ok"  # non-tty → json contract
    assert cfg.resolve_api_key(None) == ("lenz_flagkey", "file")


def test_login_json_no_key_points_to_dashboard():
    result = runner.invoke(app, ["--json", "login"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "no_key"
    assert "api-integration" in payload["dashboard"]


def test_login_write_failure_is_friendly(monkeypatch):
    from lenz_io.cli import commands

    def _boom(key):
        raise OSError("disk full")

    monkeypatch.setattr(commands, "save_api_key", _boom)
    result = runner.invoke(app, ["--json", "--api-key", "lenz_k", "login"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "write_failed"


# ── verify lifecycle branches ────────────────────────────────────────────────
def test_verify_detach_emits_task_id(monkeypatch):
    monkeypatch.setenv("LENZ_API_KEY", "k")
    _patch_client(monkeypatch, FakeClient())
    result = runner.invoke(app, ["--json", "verify", "claim", "--detach"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "submitted"
    assert payload["task_id"] == "t-1"


def test_verify_claim_index_out_of_range(monkeypatch):
    monkeypatch.setenv("LENZ_API_KEY", "k")
    _patch_client(
        monkeypatch,
        FakeClient(
            statuses=[
                TaskStatus(
                    status="needs_input",
                    reason="multi_claim",
                    claims=[CandidateClaim(text="A"), CandidateClaim(text="B")],
                )
            ]
        ),
    )
    result = runner.invoke(app, ["--json", "verify", "blob", "--claim", "9"])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["code"] == "invalid_selection"


def test_verify_clarification_json_emits_and_exits(monkeypatch):
    monkeypatch.setenv("LENZ_API_KEY", "k")
    _patch_client(
        monkeypatch,
        FakeClient(
            statuses=[
                TaskStatus(status="needs_input", reason="clarification_required", candidates=["mean A?", "or B?"])
            ]
        ),
    )
    result = runner.invoke(app, ["--json", "verify", "blob"])
    assert result.exit_code == 3
    payload = json.loads(result.stdout)
    assert payload["reason"] == "clarification_required"
    assert payload["candidates"] == ["mean A?", "or B?"]


def test_verify_duplicate_found_json_emits_and_exits(monkeypatch):
    monkeypatch.setenv("LENZ_API_KEY", "k")
    _patch_client(
        monkeypatch,
        FakeClient(
            statuses=[
                TaskStatus(
                    status="needs_input",
                    reason="duplicate_found",
                    similar_claims=[SimilarVerification(verification_id="dup12345", verdict="False", lenz_score=2)],
                )
            ]
        ),
    )
    result = runner.invoke(app, ["--json", "verify", "blob"])
    assert result.exit_code == 3
    payload = json.loads(result.stdout)
    assert payload["reason"] == "duplicate_found"
    assert payload["similar"][0]["verification_id"] == "dup12345"


def test_verify_timeout_emits_resume_fix(monkeypatch):
    monkeypatch.setenv("LENZ_API_KEY", "k")
    _patch_client(monkeypatch, FakeClient(statuses=[TaskStatus(status="processing")] * 3))
    result = runner.invoke(app, ["--json", "verify", "claim", "--timeout", "0"])
    assert result.exit_code == 1
    err = json.loads(result.stdout)["error"]
    assert err["code"] == "timeout"
    assert "--resume" in err["fix"]


def test_verify_ctrl_c_prints_resume_handle(monkeypatch):
    monkeypatch.setenv("LENZ_API_KEY", "k")
    fake = FakeClient()

    def _interrupt(task_id):
        raise KeyboardInterrupt

    fake.get_status = _interrupt
    _patch_client(monkeypatch, fake)
    result = runner.invoke(app, ["--json", "verify", "claim"])
    assert result.exit_code == 130
    payload = json.loads(result.stdout)
    assert payload["status"] == "interrupted"
    assert payload["task_id"] == "t-1"


def test_parse_claim_selection():
    from lenz_io.cli.verify import _parse_claim_selection as p

    assert p(None) is None
    assert p("all") == "all"
    assert p("2") == [1]
    assert p("1, 2, 4, 5") == [0, 1, 3, 4]
    assert p("1,1,2") == [0, 1]  # dedup


def test_verify_multi_select_json_emits_array(monkeypatch):
    """`--claim 1,3` selects two claims → two pipelines → a JSON array of both
    verdicts (the new batch path)."""
    monkeypatch.setenv("LENZ_API_KEY", "k")
    fake = _patch_client(
        monkeypatch,
        FakeClient(
            statuses=[
                TaskStatus(
                    status="needs_input",
                    reason="multi_claim",
                    claims=[CandidateClaim(text="A"), CandidateClaim(text="B"), CandidateClaim(text="C")],
                ),
                TaskStatus(status="completed", result=_verification()),  # for sel-1
                TaskStatus(status="completed", result=_verification()),  # for sel-2
            ]
        ),
    )
    result = runner.invoke(app, ["--json", "verify", "blob", "--claim", "1,3"])
    assert result.exit_code == 0
    assert fake.select_calls == [("t-1", ["A", "C"])]  # picked options 1 and 3
    payload = json.loads(result.stdout)
    assert isinstance(payload, list) and len(payload) == 2
    assert all(item["status"] == "completed" for item in payload)


def test_verify_select_all(monkeypatch):
    monkeypatch.setenv("LENZ_API_KEY", "k")
    fake = _patch_client(
        monkeypatch,
        FakeClient(
            statuses=[
                TaskStatus(
                    status="needs_input",
                    reason="multi_claim",
                    claims=[CandidateClaim(text="A"), CandidateClaim(text="B")],
                ),
                TaskStatus(status="completed", result=_verification()),
                TaskStatus(status="completed", result=_verification()),
            ]
        ),
    )
    result = runner.invoke(app, ["--json", "verify", "blob", "--claim", "all"])
    assert result.exit_code == 0
    assert fake.select_calls == [("t-1", ["A", "B"])]
    assert len(json.loads(result.stdout)) == 2


def test_verify_multi_select_detach(monkeypatch):
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
            ]
        ),
    )
    result = runner.invoke(app, ["--json", "verify", "blob", "--claim", "1,2", "--detach"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert len(payload) == 2 and all(p["status"] == "submitted" for p in payload)


def test_verify_claim_out_of_range_multi(monkeypatch):
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
            ]
        ),
    )
    result = runner.invoke(app, ["--json", "verify", "blob", "--claim", "1,9"])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["code"] == "invalid_selection"


def test_verify_deadline_resets_after_pick(monkeypatch):
    """Time spent at the picker must not be charged against the second pipeline
    run's timeout. A fake clock jumps past the original deadline during the pick;
    with the reset the second round still has a full budget."""
    monkeypatch.setenv("LENZ_API_KEY", "k")
    seq = iter([0.0, 1.0, 50.0, 51.0])  # start, round1 check, reset, round2 check
    monkeypatch.setattr(verify_mod.time, "monotonic", lambda: next(seq, 100.0))
    _patch_client(
        monkeypatch,
        FakeClient(
            statuses=[
                TaskStatus(
                    status="needs_input",
                    reason="multi_claim",
                    claims=[CandidateClaim(text="A"), CandidateClaim(text="B")],
                ),
                TaskStatus(status="completed", result=_verification()),
            ]
        ),
    )
    result = runner.invoke(app, ["--json", "verify", "blob", "--claim", "1", "--timeout", "10"])
    assert result.exit_code == 0  # no spurious timeout despite the clock jump
    assert json.loads(result.stdout)["verdict"] == "False"


def test_resume_honors_claim_preselect(monkeypatch):
    """`--resume <id> --claim N` must auto-select on a resumed task that re-enters
    needs_input, not drop the preselection (regression: _resume ignored --claim)."""
    monkeypatch.setenv("LENZ_API_KEY", "k")
    fake = _patch_client(
        monkeypatch,
        FakeClient(
            statuses=[
                TaskStatus(status="processing"),  # consumed by _resume's first poll
                TaskStatus(
                    status="needs_input",
                    reason="multi_claim",
                    claims=[CandidateClaim(text="claim A"), CandidateClaim(text="claim B")],
                ),
                TaskStatus(status="completed", result=_verification()),
            ]
        ),
    )
    result = runner.invoke(app, ["--json", "verify", "--resume", "t-1", "--claim", "2"])
    assert result.exit_code == 0  # not exit 3 (needs_input emitted)
    assert fake.select_calls == [("t-1", ["claim B"])]
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
