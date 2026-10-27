"""Tests for supplied_sources across models, core payload serialization,
client and async client methods, and CLI --source-file parsing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import respx
from typer.testing import CliRunner

from lenz_io import (
    AsyncLenz,
    BatchClaimInput,
    CitationPair,
    CitationPairInput,
    Evidence,
    Lenz,
    Source,
    SuppliedSource,
    VerifyBatchItem,
)
from lenz_io._core import (
    _batch_payload,
    _citecheck_payload,
    _review_payload,
    _verify_payload,
)
from lenz_io.cli import _run, config as cfg
from lenz_io.cli._run import parse_source_file_arg, parse_source_file_args
from lenz_io.cli.app import app
from lenz_io.cli.errors import CLIError
from lenz_io.models import TaskAccepted, TaskStatus

runner = CliRunner()


# ── Model & Type Tests ────────────────────────────────────────────────────────


def test_supplied_source_dict_structure() -> None:
    source: SuppliedSource = {
        "text": "The boiling point of water is 100 degrees Celsius at 1 atm.",
        "url": "https://example.com/water",
        "doi": "10.1000/182",
        "cite": "Water Handbook",
        "title": "Water Properties",
        "published_date": "2024-01-01",
    }
    assert source["text"].startswith("The boiling point")
    assert source["title"] == "Water Properties"


def test_source_origin_defaults_to_web() -> None:
    src = Source(url="https://example.com")
    assert src.origin == "web"


def test_source_origin_coerces_none_and_empty_to_web() -> None:
    src1 = Source.model_validate({"url": "https://example.com", "origin": None})
    assert src1.origin == "web"
    src2 = Source.model_validate({"url": "https://example.com", "origin": ""})
    assert src2.origin == "web"


def test_source_origin_preserves_supplied() -> None:
    src = Source.model_validate({"url": "https://example.com", "origin": "supplied"})
    assert src.origin == "supplied"


def test_evidence_is_source_alias() -> None:
    assert Evidence is Source
    ev = Evidence(url="https://example.com", origin="supplied")
    assert ev.origin == "supplied"


def test_citation_pair_input_fields() -> None:
    assert CitationPairInput is CitationPair
    pair: CitationPair = {
        "claim": "Water boils at 100C",
        "url": "https://example.com",
        "source_text": "Boils at 100C",
        "published_date": "2024-01-01",
    }
    assert pair["source_text"] == "Boils at 100C"
    assert pair["published_date"] == "2024-01-01"


def test_verify_batch_item_with_supplied_sources() -> None:
    assert BatchClaimInput is VerifyBatchItem
    item: VerifyBatchItem = {
        "claim": "Water boils at 100C",
        "supplied_sources": [{"text": "Sample text", "title": "Doc"}],
    }
    assert item["claim"] == "Water boils at 100C"
    assert item.get("supplied_sources") is not None
    assert len(item["supplied_sources"]) == 1
    assert item["supplied_sources"][0]["title"] == "Doc"


# ── Payload Serialization Tests ───────────────────────────────────────────────


def test_verify_payload_serialization() -> None:
    sources = [{"text": "Doc text", "title": "Source Doc", "url": "https://example.com"}]
    payload = _verify_payload(
        claim="Water boils at 100C",
        text="",
        source_url="",
        webhook_url="",
        language="",
        visibility="",
        depth="",
        supplied_sources=sources,
    )
    assert payload["text"] == "Water boils at 100C"
    assert payload["supplied_sources"] == sources


def test_batch_payload_serialization() -> None:
    item_sources = [{"text": "Item doc", "title": "Doc 1"}]
    batch_sources = [{"text": "Batch doc", "title": "Doc 2"}]
    claims = [
        {"claim": "Claim 1", "supplied_sources": item_sources},
        {"claim": "Claim 2"},
    ]
    payload = _batch_payload(
        claims=claims,
        webhook_url="",
        language="",
        visibility="",
        depth="",
        supplied_sources=batch_sources,
    )
    assert payload["supplied_sources"] == batch_sources
    assert payload["claims"][0]["supplied_sources"] == item_sources
    assert "supplied_sources" not in payload["claims"][1]


def test_review_payload_serialization() -> None:
    sources = [{"text": "Ref text", "title": "Manual"}]
    payload = _review_payload(
        text="Draft content",
        verdicts=None,
        confidence=None,
        max_assessments=None,
        max_verifications=None,
        depth=None,
        max_citations=None,
        suggest_edits=False,
        language="",
        webhook_url=None,
        visibility="private",
        supplied_sources=sources,
    )
    assert payload["text"] == "Draft content"
    assert payload["supplied_sources"] == sources


def test_citecheck_payload_serialization() -> None:
    sources = [{"text": "Context", "title": "Paper"}]
    payload = _citecheck_payload(
        text="Draft body",
        pairs=None,
        max_citations=None,
        language="",
        webhook_url=None,
        supplied_sources=sources,
    )
    assert payload["text"] == "Draft body"
    assert payload["supplied_sources"] == sources


# ── Client Network Wiring Tests ───────────────────────────────────────────────


def test_sync_client_methods_forward_supplied_sources() -> None:
    sources = [{"text": "Source text", "title": "Doc"}]
    client = Lenz(api_key="lenz_test_key")

    with respx.mock(assert_all_called=True) as router:
        v_route = router.post("https://lenz.io/api/v1/verify").respond(200, json={"task_id": "t-1", "status": "queued"})
        vb_route = router.post("https://lenz.io/api/v1/verify/batch").respond(
            200, json={"batch_id": "b-1", "items": [{"task_id": "t-2", "claim_text": "C"}]}
        )
        r_route = router.post("https://lenz.io/api/v1/review").respond(
            200, json={"review_id": "r-1", "status": "queued"}
        )
        c_route = router.post("https://lenz.io/api/v1/citecheck").respond(
            200, json={"citecheck_id": "cc-1", "status": "queued"}
        )

        client.verify(claim="Test claim", supplied_sources=sources)
        assert json.loads(v_route.calls.last.request.content)["supplied_sources"] == sources

        client.verify_batch(claims=[{"claim": "C"}], supplied_sources=sources)
        assert json.loads(vb_route.calls.last.request.content)["supplied_sources"] == sources

        client.review(text="Test draft", supplied_sources=sources)
        assert json.loads(r_route.calls.last.request.content)["supplied_sources"] == sources

        client.citecheck(text="Test citecheck", supplied_sources=sources)
        assert json.loads(c_route.calls.last.request.content)["supplied_sources"] == sources


@pytest.mark.asyncio
async def test_async_client_methods_forward_supplied_sources() -> None:
    sources = [{"text": "Source text", "title": "Doc"}]
    async with AsyncLenz(api_key="lenz_test_key") as client:
        with respx.mock(assert_all_called=True) as router:
            v_route = router.post("https://lenz.io/api/v1/verify").respond(
                200, json={"task_id": "t-1", "status": "queued"}
            )
            vb_route = router.post("https://lenz.io/api/v1/verify/batch").respond(
                200, json={"batch_id": "b-1", "items": [{"task_id": "t-2", "claim_text": "C"}]}
            )
            r_route = router.post("https://lenz.io/api/v1/review").respond(
                200, json={"review_id": "r-1", "status": "queued"}
            )
            c_route = router.post("https://lenz.io/api/v1/citecheck").respond(
                200, json={"citecheck_id": "cc-1", "status": "queued"}
            )

            await client.verify(claim="Test claim", supplied_sources=sources)
            assert json.loads(v_route.calls.last.request.content)["supplied_sources"] == sources

            await client.verify_batch(claims=[{"claim": "C"}], supplied_sources=sources)
            assert json.loads(vb_route.calls.last.request.content)["supplied_sources"] == sources

            await client.review(text="Test draft", supplied_sources=sources)
            assert json.loads(r_route.calls.last.request.content)["supplied_sources"] == sources

            await client.citecheck(text="Test citecheck", supplied_sources=sources)
            assert json.loads(c_route.calls.last.request.content)["supplied_sources"] == sources


# ── CLI Source File Parsing Tests ─────────────────────────────────────────────


def test_parse_source_file_plain(tmp_path: Path) -> None:
    file = tmp_path / "article.txt"
    file.write_text("Detailed research content.", encoding="utf-8")

    parsed = parse_source_file_arg(str(file))
    assert parsed["text"] == "Detailed research content."
    assert parsed["title"] == "article.txt"
    assert "url" not in parsed
    assert "doi" not in parsed


def test_parse_source_file_bracket_metadata(tmp_path: Path) -> None:
    file = tmp_path / "paper.txt"
    file.write_text("Scientific study content.", encoding="utf-8")

    spec = f"[url=https://example.com/paper,title=Study A,doi=10.1234/567,date=2024-03-15]:{file}"
    parsed = parse_source_file_arg(spec)
    assert parsed["text"] == "Scientific study content."
    assert parsed["title"] == "Study A"
    assert parsed["url"] == "https://example.com/paper"
    assert parsed["doi"] == "10.1234/567"
    assert parsed["published_date"] == "2024-03-15"


def test_parse_source_file_bracket_trailing_colon(tmp_path: Path) -> None:
    file = tmp_path / "notes.txt"
    file.write_text("Field notes.", encoding="utf-8")

    spec = f"[title=Field Notes:]: {file}"
    parsed = parse_source_file_arg(spec)
    assert parsed["title"] == "Field Notes"
    assert parsed["text"] == "Field notes."


def test_parse_source_file_prefix_metadata(tmp_path: Path) -> None:
    file = tmp_path / "source.txt"
    file.write_text("Evidence text.", encoding="utf-8")

    spec = f"url=https://example.com/evidence:title=Evidence 1:cite=Smith 2024:{file}"
    parsed = parse_source_file_arg(spec)
    assert parsed["text"] == "Evidence text."
    assert parsed["title"] == "Evidence 1"
    assert parsed["url"] == "https://example.com/evidence"
    assert parsed["cite"] == "Smith 2024"


def test_parse_source_file_url_with_colons_and_query_params(tmp_path: Path) -> None:
    file = tmp_path / "source.txt"
    file.write_text("Evidence text.", encoding="utf-8")

    spec = f"[url=https://example.com/a:b?x=1,ref=2,title=Evidence 2]:{file}"
    parsed = parse_source_file_arg(spec)
    assert parsed["url"] == "https://example.com/a:b?x=1,ref=2"
    assert parsed["title"] == "Evidence 2"

    with pytest.raises(CLIError, match="Unknown source file metadata key 'author'"):
        parse_source_file_arg(f"author=Smith:{file}")


def test_invalid_supplied_sources_raises_usage_error() -> None:
    from lenz_io.errors import LenzUsageError

    with pytest.raises(LenzUsageError) as exc_info:
        _verify_payload(
            claim="C",
            text="",
            source_url="",
            webhook_url="",
            language="",
            visibility="",
            depth="",
            supplied_sources=["not a dict"],  # type: ignore[list-item]
        )
    assert exc_info.value.code == "invalid_argument"
    assert exc_info.value.param == "supplied_sources[0]"

    with pytest.raises(LenzUsageError) as batch_exc:
        _batch_payload(
            claims=[{"claim": "C", "supplied_sources": [123]}],  # type: ignore[list-item]
            webhook_url="",
            language="",
            visibility="",
            depth="",
        )
    assert batch_exc.value.code == "invalid_argument"
    assert batch_exc.value.param == "claims[0].supplied_sources[0]"


def test_parse_source_file_bracket_nested_brackets(tmp_path: Path) -> None:
    file = tmp_path / "case.txt"
    file.write_text("Judgment text.", encoding="utf-8")

    spec = f"[cite=[2024] UKSC 12,title= Judgment [Final]]:{file}"
    parsed = parse_source_file_arg(spec)
    assert parsed["text"] == "Judgment text."
    assert parsed["cite"] == "[2024] UKSC 12"
    assert parsed["title"] == "Judgment [Final]"


def test_parse_source_file_prefix_windows_drive_letter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    win_file = tmp_path / r"C:\paper.txt"
    win_file.write_text("Windows path content.", encoding="utf-8")

    spec = r"title=Paper:C:\paper.txt"
    parsed = parse_source_file_arg(spec)
    assert parsed["text"] == "Windows path content."
    assert parsed["title"] == "Paper"


def test_parse_source_file_missing_file() -> None:
    with pytest.raises(CLIError) as exc_info:
        parse_source_file_arg("non_existent_file_12345.txt")
    assert exc_info.value.code == "source_file_not_found"
    assert exc_info.value.exit_code == 2


def test_parse_source_file_empty_file(tmp_path: Path) -> None:
    empty = tmp_path / "empty.txt"
    empty.write_text("   \n\t  ", encoding="utf-8")
    with pytest.raises(CLIError) as exc_info:
        parse_source_file_arg(str(empty))
    assert exc_info.value.code == "empty_source_file"
    assert exc_info.value.exit_code == 2


def test_parse_source_file_empty_spec() -> None:
    with pytest.raises(CLIError) as exc_info:
        parse_source_file_arg("   ")
    assert exc_info.value.code == "invalid_source_file_arg"
    assert exc_info.value.exit_code == 2


def test_parse_source_file_unclosed_bracket(tmp_path: Path) -> None:
    file = tmp_path / "doc.txt"
    file.write_text("Data", encoding="utf-8")
    with pytest.raises(CLIError) as exc_info:
        parse_source_file_arg(f"[url=https://example.com:{file}")
    assert exc_info.value.code == "invalid_source_file_arg"
    assert exc_info.value.exit_code == 2


def test_parse_source_file_unknown_meta_key(tmp_path: Path) -> None:
    file = tmp_path / "doc.txt"
    file.write_text("Data", encoding="utf-8")
    with pytest.raises(CLIError) as exc_info:
        parse_source_file_arg(f"[invalid_key=foo]:{file}")
    assert exc_info.value.code == "invalid_source_file_arg"
    assert exc_info.value.exit_code == 2


def test_parse_source_file_args_empty_or_none() -> None:
    assert parse_source_file_args(None) is None
    assert parse_source_file_args([]) is None


# ── CLI End-to-End Invocations ────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("LENZ_BASE_URL", raising=False)
    monkeypatch.setenv("LENZ_API_KEY", "lenz_test_key")
    monkeypatch.setattr(cfg, "config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr("lenz_io.client.time.sleep", lambda s: None)


def test_cli_verify_with_source_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source_file = tmp_path / "ref.txt"
    source_file.write_text("Standard atmospheric pressure water boils at 100C.", encoding="utf-8")

    captured_kwargs: dict[str, Any] = {}

    class _MockClient:
        def verify(self, claim: str, **kwargs: Any) -> TaskAccepted:
            captured_kwargs.update(kwargs)
            captured_kwargs["claim"] = claim
            return TaskAccepted(task_id="t-100", status="completed")

        def get_status(self, task_id: str) -> TaskStatus:
            return TaskStatus(task_id=task_id, status="completed")

        def close(self) -> None:
            pass

    monkeypatch.setattr(_run, "build_client", lambda **kw: _MockClient())
    monkeypatch.setattr("lenz_io.cli.verify._poll", lambda client, out, task_id, timeout, **kw: None)

    res = runner.invoke(
        app,
        [
            "verify",
            "Water boils at 100C",
            "--source-file",
            f"[title=Ref Doc,url=https://nist.gov]:{source_file}",
        ],
    )
    assert res.exit_code == 0
    assert captured_kwargs["claim"] == "Water boils at 100C"
    assert captured_kwargs["supplied_sources"] == [
        {
            "text": "Standard atmospheric pressure water boils at 100C.",
            "title": "Ref Doc",
            "url": "https://nist.gov",
        }
    ]


def test_cli_verify_resume_with_source_file_errors(tmp_path: Path) -> None:
    file = tmp_path / "doc.txt"
    file.write_text("Doc text", encoding="utf-8")
    res = runner.invoke(app, ["verify", "--resume", "t-1", "--source-file", str(file)])
    assert res.exit_code == 2
    assert "Cannot pass --source-file with --resume" in res.output or "invalid_usage" in res.output


def test_cli_review_with_source_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    draft = tmp_path / "draft.txt"
    draft.write_text("Review draft text with factual claims.", encoding="utf-8")
    source = tmp_path / "source.txt"
    source.write_text("Verified source documentation.", encoding="utf-8")

    captured_kwargs: dict[str, Any] = {}

    class _MockClient:
        def review(self, text: str, **kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            captured_kwargs["text"] = text
            from lenz_io.models import ReviewStarted

            return ReviewStarted(review_id="r-100", status="queued")

        def close(self) -> None:
            pass

    monkeypatch.setattr(_run, "build_client", lambda **kw: _MockClient())

    res = runner.invoke(
        app,
        [
            "review",
            str(draft),
            "--detach",
            "--source-file",
            f"title=Primary Source:{source}",
        ],
    )
    assert res.exit_code == 0
    assert captured_kwargs["text"] == "Review draft text with factual claims."
    assert captured_kwargs["supplied_sources"] == [
        {
            "text": "Verified source documentation.",
            "title": "Primary Source",
        }
    ]


def test_cli_citecheck_with_source_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    draft = tmp_path / "draft.txt"
    draft.write_text("A statement citing evidence [1].", encoding="utf-8")
    source = tmp_path / "source.txt"
    source.write_text("Evidence supporting statement.", encoding="utf-8")

    captured_kwargs: dict[str, Any] = {}

    class _MockClient:
        def citecheck(self, text: str | None = None, **kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            captured_kwargs["text"] = text
            from lenz_io.models import CitecheckStarted

            return CitecheckStarted(citecheck_id="cc-100", status="queued")

        def close(self) -> None:
            pass

    monkeypatch.setattr(_run, "build_client", lambda **kw: _MockClient())

    res = runner.invoke(
        app,
        [
            "citecheck",
            str(draft),
            "--detach",
            "--source-file",
            str(source),
        ],
    )
    assert res.exit_code == 0
    assert captured_kwargs["text"] == "A statement citing evidence [1]."
    assert captured_kwargs["supplied_sources"] == [
        {
            "text": "Evidence supporting statement.",
            "title": "source.txt",
        }
    ]
