"""``lenz citecheck``: a draft file or a pairs file in, the check's findings
out, and the exit code from its outcome."""

from __future__ import annotations

import io
import json
from pathlib import Path

import httpx
import pytest
import respx
from rich.console import Console
from typer.testing import CliRunner

from lenz_io import Lenz
from lenz_io.cli import _run, config as cfg, normalize_argv
from lenz_io.cli.app import app
from lenz_io.cli.citecheck import render_citecheck
from lenz_io.cli.render import Output
from lenz_io.models import Citecheck

BASE = "https://lenz.io/api/v1"
FIXTURES = Path(__file__).parent / "fixtures" / "contract"
runner = CliRunner()


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


COMPLETED = _load("citecheck_completed.json")
CHECK_ID = COMPLETED["citecheck_id"]


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.delenv("LENZ_BASE_URL", raising=False)
    monkeypatch.setenv("LENZ_API_KEY", "lenz_test_key")
    monkeypatch.setattr(cfg, "config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr("lenz_io.client.time.sleep", lambda s: None)
    monkeypatch.setattr(_run, "build_client", lambda **kw: Lenz(api_key="lenz_test_key"))


@pytest.fixture()
def draft(tmp_path):
    path = tmp_path / "draft.md"
    path.write_text("Water boils at 100 degrees, per [the entry](https://en.wikipedia.org/wiki/Boiling_point).\n")
    return str(path)


@pytest.fixture()
def pairs_file(tmp_path):
    path = tmp_path / "pairs.json"
    path.write_text(json.dumps([{"statement": "Water boils at 100 degrees.", "url": "https://example.org/b"}]))
    return str(path)


def _serve(r, body: dict | None = None):
    body = body or COMPLETED
    post = r.post("/citecheck").respond(202, json={"citecheck_id": body["citecheck_id"], "status": "queued"})
    r.get(f"/citechecks/{body['citecheck_id']}").mock(return_value=httpx.Response(200, json=body))
    return post


def _invoke(*args: str):
    return runner.invoke(app, normalize_argv(list(args)))


def test_a_draft_is_sent_as_text(draft):
    with respx.mock(base_url=BASE) as r:
        post = _serve(r)
        result = _invoke("citecheck", draft, "--max-citations", "4", "--json")
    assert result.exit_code == 1, result.output  # issues_found
    body = json.loads(post.calls.last.request.content)
    assert body["text"].startswith("Water boils") and body["max_citations"] == 4
    assert json.loads(result.stdout)["citecheck_id"] == CHECK_ID


def test_a_pairs_file_is_sent_as_pairs(pairs_file):
    with respx.mock(base_url=BASE) as r:
        post = _serve(r, _load("citecheck_pairs_completed.json"))
        _invoke("citecheck", "--pairs", pairs_file, "--json")
    assert json.loads(post.calls.last.request.content) == {"pairs": json.loads(Path(pairs_file).read_text())}


def test_a_pairs_object_file_is_read_too(tmp_path):
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"pairs": [{"statement": "s is here.", "doi": "10.1038/nature12373"}]}))
    with respx.mock(base_url=BASE) as r:
        post = _serve(r, _load("citecheck_pairs_completed.json"))
        _invoke("citecheck", "--pairs", str(path), "--json")
    assert json.loads(post.calls.last.request.content)["pairs"][0]["doi"] == "10.1038/nature12373"


@pytest.mark.parametrize(("outcome", "code"), [("clean", 0), ("issues_found", 1), ("incomplete", 2), (None, 2)])
def test_exit_code_follows_the_outcome(draft, outcome, code):
    with respx.mock(base_url=BASE) as r:
        _serve(r, dict(COMPLETED, outcome=outcome))
        result = _invoke("citecheck", draft, "--json")
    assert result.exit_code == code, result.output


@pytest.mark.parametrize(
    "args",
    [
        ("citecheck", "https://example.org/page"),
        ("citecheck", "DRAFT", "--pairs", "PAIRS"),
        ("citecheck", "--pairs", "PAIRS", "--max-citations", "3"),
        ("citecheck", "DRAFT", "--max-citations", "0"),
        ("citecheck", "DRAFT", "--max-citations", "21"),
        ("citecheck", "no-such-file.md"),
        ("citecheck", "DRAFT", "--resume", CHECK_ID),
    ],
)
def test_bad_input_is_refused_before_any_request(draft, pairs_file, args):
    argv = [draft if a == "DRAFT" else pairs_file if a == "PAIRS" else a for a in args]
    with respx.mock(base_url=BASE, assert_all_called=False) as r:
        post = _serve(r)
        result = _invoke(*argv, "--json")
    assert result.exit_code == 2, result.output
    assert not post.called


def test_a_pairs_file_that_is_not_a_list_is_refused(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"statement": "one"}))
    with respx.mock(base_url=BASE, assert_all_called=False) as r:
        post = _serve(r)
        result = _invoke("citecheck", "--pairs", str(path), "--json")
    assert result.exit_code == 2 and not post.called


def test_a_file_holding_one_url_is_refused(tmp_path):
    path = tmp_path / "url.md"
    path.write_text("https://example.org/page\n")
    with respx.mock(base_url=BASE, assert_all_called=False) as r:
        post = _serve(r)
        result = _invoke("citecheck", str(path), "--json")
    assert result.exit_code == 2 and not post.called


def test_detach_prints_the_id_and_resume_reads_it(draft):
    with respx.mock(base_url=BASE, assert_all_called=False) as r:
        _serve(r)
        detached = _invoke("citecheck", draft, "--detach", "--json")
    assert json.loads(detached.stdout) == {"status": "submitted", "citecheck_id": CHECK_ID}
    with respx.mock(base_url=BASE, assert_all_called=False) as r:
        post = _serve(r)
        resumed = _invoke("citecheck", "--resume", CHECK_ID, "--json")
    assert resumed.exit_code == 1 and not post.called


def _render(body: dict) -> str:
    buf = io.StringIO()
    out = Output(json_mode=False, no_color=True)
    out.json_mode = False
    out.console = Console(file=buf, no_color=True, width=200, highlight=False)
    render_citecheck(out, Citecheck.model_validate(body))
    return buf.getvalue()


def test_the_render_of_a_recorded_check():
    text = _render(COMPLETED)
    lines = text.splitlines()
    assert lines[0] == f"Citation check {CHECK_ID}: issues found — 3 credits charged"
    i = lines.index("10 sources cited in your draft")
    assert lines[i + 1] == "3 checked, 2 with a problem. 1 could not be checked."
    assert lines[i + 2] == "6 more were not checked: one check covers 4."
    assert "Contradicted" in text and "The source says:" in text
    assert text.rstrip().endswith("6 more citations were found but not checked.")


def test_the_render_of_a_clean_check_of_pairs():
    body = _load("citecheck_pairs_completed.json")
    body.update(outcome="clean", citation_issues=[])
    body["summary"]["citation_issues"] = 0
    text = _render(body)
    assert "2 sources cited in your draft" in text and "No issues." in text
    assert "found but not checked" not in text


def test_citecheck_language_is_sent(draft, pairs_file):
    with respx.mock(base_url=BASE) as r:
        post = _serve(r)
        _invoke("citecheck", draft, "--language", "de", "--json")
    assert json.loads(post.calls.last.request.content)["language"] == "de"
    with respx.mock(base_url=BASE) as r:
        post = _serve(r, _load("citecheck_pairs_completed.json"))
        _invoke("citecheck", "--pairs", pairs_file, "--language", "fr", "--json")
    assert json.loads(post.calls.last.request.content)["language"] == "fr"
