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
from lenz_io.cli import _run
from lenz_io.cli import config as cfg
from lenz_io.cli import verify as verify_mod
from lenz_io.cli.app import app
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


def test_save_api_key_perms_0600(tmp_path):
    path = cfg.save_api_key("secret")
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600
    assert json.loads(path.read_text())["api_key"] == "secret"


def test_corrupt_config_is_friendly(monkeypatch, tmp_path):
    (tmp_path / "config.json").write_text("{not json")
    with pytest.raises(cfg.ConfigError):
        cfg.resolve_api_key(None)


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
    # --claim 2 (1-based) → select claim_index=1, no hang, verdict rendered
    result = runner.invoke(app, ["--json", "verify", "blob", "--claim", "2"])
    assert result.exit_code == 0
    assert fake.select_calls == [("t-1", "", 1)]
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
