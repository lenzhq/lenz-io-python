"""Tests for client.extract_file and async_client.extract_file."""

from __future__ import annotations

import io
from pathlib import Path

import httpx
import pytest
import respx

from lenz_io import AsyncLenz, Lenz
from lenz_io._core import EXTRACT_TIMEOUT
from lenz_io.errors import LenzRateLimitError, LenzUsageError, LenzValidationError
from lenz_io.models import ExtractedClaims

BASE = "https://lenz.io/api/v1"

_MOCK_EXTRACT = {
    "status": "ready",
    "domain": "finance",
    "claims": [
        {
            "claim": "Revenue reached $10M in Q3.",
            "status": "verifiable",
            "positions": [{"start": None, "end": None, "text": "Revenue reached $10M in Q3."}],
        },
        {
            "claim": "Operating margins expanded by 200 bps.",
            "status": "verifiable",
            "positions": [{"start": None, "end": None, "text": "Operating margins expanded by 200 bps."}],
        },
    ],
    "identified_claims": [
        "Revenue reached $10M in Q3.",
        "Operating margins expanded by 200 bps.",
    ],
    "language": "en",
}


@pytest.fixture
def client():
    return Lenz(api_key="lenz_test_key123")


@pytest.fixture
def async_client():
    return AsyncLenz(api_key="lenz_test_key123")


class TestExtractFileSuccess:
    def test_extract_file_path_docx(self, client, tmp_path: Path):
        file_path = tmp_path / "report.docx"
        file_path.write_bytes(b"PK\x03\x04fake docx content")

        with respx.mock(base_url=BASE) as respx_mock:
            route = respx_mock.post("/extract/file").respond(200, json=_MOCK_EXTRACT)
            res = client.extract_file(file_path)

            assert isinstance(res, ExtractedClaims)
            assert res.status == "ready"
            assert len(res.claims) == 2
            assert res.claims[0].claim == "Revenue reached $10M in Q3."
            assert route.called
            req = route.calls.last.request
            assert b"filename=\"report.docx\"" in req.content
            docx_mime = b"Content-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            assert docx_mime in req.content
            assert req.extensions["timeout"]["read"] == EXTRACT_TIMEOUT

    def test_extract_file_str_path_pdf(self, client, tmp_path: Path):
        file_path = tmp_path / "paper.pdf"
        file_path.write_bytes(b"%PDF-1.4 fake pdf")

        with respx.mock(base_url=BASE) as respx_mock:
            route = respx_mock.post("/extract/file").respond(200, json=_MOCK_EXTRACT)
            res = client.extract_file(str(file_path))

            assert isinstance(res, ExtractedClaims)
            assert route.called
            req = route.calls.last.request
            assert b"filename=\"paper.pdf\"" in req.content
            assert b"Content-Type: application/pdf" in req.content

    def test_extract_file_image_formats_and_mime_types(self, client):
        cases = [
            ("slide.png", b"\x89PNG fake", "image/png"),
            ("photo.jpg", b"\xff\xd8fake", "image/jpeg"),
            ("photo.jpeg", b"\xff\xd8fake", "image/jpeg"),
            ("diagram.webp", b"RIFFfakeWEBP", "image/webp"),
        ]
        for fname, data, expected_mime in cases:
            with respx.mock(base_url=BASE) as respx_mock:
                route = respx_mock.post("/extract/file").respond(200, json=_MOCK_EXTRACT)
                res = client.extract_file(data, filename=fname)
                assert isinstance(res, ExtractedClaims)
                assert route.called
                req = route.calls.last.request
                assert f"filename=\"{fname}\"".encode() in req.content
                assert f"Content-Type: {expected_mime}".encode() in req.content

    def test_extract_file_bytes_and_stream(self, client):
        data = b"PK\x03\x04fake docx"
        stream = io.BytesIO(data)
        stream.name = "streamed.docx"

        with respx.mock(base_url=BASE) as respx_mock:
            route = respx_mock.post("/extract/file").respond(200, json=_MOCK_EXTRACT)
            res = client.extract_file(stream)
            assert isinstance(res, ExtractedClaims)
            assert route.called
            assert b"filename=\"streamed.docx\"" in route.calls.last.request.content

    def test_extract_file_override_filename_and_content_type(self, client):
        data = b"PK\x03\x04fake"
        with respx.mock(base_url=BASE) as respx_mock:
            route = respx_mock.post("/extract/file").respond(200, json=_MOCK_EXTRACT)
            client.extract_file(data, filename="custom.pdf", content_type="application/x-custom-pdf")
            req = route.calls.last.request
            assert b"filename=\"custom.pdf\"" in req.content
            assert b"Content-Type: application/x-custom-pdf" in req.content

    def test_extract_file_form_data_fields(self, client, tmp_path: Path):
        file_path = tmp_path / "doc.pdf"
        file_path.write_bytes(b"%PDF-1.4 test")

        with respx.mock(base_url=BASE) as respx_mock:
            route = respx_mock.post("/extract/file").respond(200, json=_MOCK_EXTRACT)
            client.extract_file(file_path, language="de", focus="market share", locate=True)
            content = route.calls.last.request.content
            assert b"name=\"language\"\r\n\r\nde" in content
            assert b"name=\"focus\"\r\n\r\nmarket share" in content
            assert b"name=\"locate\"\r\n\r\ntrue" in content

    def test_extract_file_locate_false_and_none(self, client, tmp_path: Path):
        file_path = tmp_path / "doc.pdf"
        file_path.write_bytes(b"%PDF-1.4 test")

        with respx.mock(base_url=BASE) as respx_mock:
            route = respx_mock.post("/extract/file").respond(200, json=_MOCK_EXTRACT)
            client.extract_file(file_path, locate=False)
            content = route.calls.last.request.content
            assert b"name=\"locate\"\r\n\r\nfalse" in content

        with respx.mock(base_url=BASE) as respx_mock:
            route = respx_mock.post("/extract/file").respond(200, json=_MOCK_EXTRACT)
            client.extract_file(file_path, locate=None)
            content = route.calls.last.request.content
            assert b"name=\"locate\"" not in content

    def test_extract_file_idempotency_and_retry(self, client, tmp_path: Path):
        file_path = tmp_path / "doc.pdf"
        file_path.write_bytes(b"%PDF-1.4 test")

        with respx.mock(base_url=BASE) as respx_mock:
            route = respx_mock.post("/extract/file")
            route.side_effect = [
                httpx.Response(500, json={"detail": "transient error"}),
                httpx.Response(200, json=_MOCK_EXTRACT),
            ]
            res = client.extract_file(file_path)
            assert isinstance(res, ExtractedClaims)
            assert len(route.calls) == 2
            k1 = route.calls[0].request.headers["Idempotency-Key"]
            k2 = route.calls[1].request.headers["Idempotency-Key"]
            assert k1 == k2

    def test_extract_file_pinned_idempotency_key(self, client, tmp_path: Path):
        file_path = tmp_path / "doc.pdf"
        file_path.write_bytes(b"%PDF-1.4 test")

        with respx.mock(base_url=BASE) as respx_mock:
            route = respx_mock.post("/extract/file").respond(200, json=_MOCK_EXTRACT)
            client.extract_file(file_path, idempotency_key="custom-key-999")
            assert route.calls.last.request.headers["Idempotency-Key"] == "custom-key-999"

        with respx.mock(base_url=BASE) as respx_mock:
            route = respx_mock.post("/extract/file").respond(200, json=_MOCK_EXTRACT)
            client.extract_file(file_path, idempotency=False)
            assert "Idempotency-Key" not in route.calls.last.request.headers

    def test_extract_file_timeout_override(self, client, tmp_path: Path):
        file_path = tmp_path / "doc.pdf"
        file_path.write_bytes(b"%PDF-1.4 test")

        with respx.mock(base_url=BASE) as respx_mock:
            route = respx_mock.post("/extract/file").respond(200, json=_MOCK_EXTRACT)
            client.extract_file(file_path, timeout=45.0)
            assert route.calls.last.request.extensions["timeout"]["read"] == 45.0


class TestExtractFileValidationErrors:
    def test_missing_file_path(self, client, tmp_path: Path):
        with pytest.raises(LenzUsageError) as exc_info:
            client.extract_file(tmp_path / "nonexistent.docx")
        assert "No such file" in str(exc_info.value)
        assert exc_info.value.code == "invalid_argument"
        assert exc_info.value.param == "file"

    def test_directory_as_file(self, client, tmp_path: Path):
        with pytest.raises(LenzUsageError) as exc_info:
            client.extract_file(tmp_path)
        assert "Expected a file, but got a directory" in str(exc_info.value)
        assert exc_info.value.param == "file"

    def test_empty_string_file(self, client):
        with pytest.raises(LenzUsageError) as exc_info:
            client.extract_file("")
        assert "file is required." in str(exc_info.value)
        assert exc_info.value.param == "file"

    def test_empty_payload_0_bytes(self, client, tmp_path: Path):
        empty_file = tmp_path / "empty.pdf"
        empty_file.write_bytes(b"")
        with pytest.raises(LenzUsageError) as exc_info:
            client.extract_file(empty_file)
        assert "file payload is empty (0 bytes)." in str(exc_info.value)
        assert exc_info.value.param == "file"

    def test_missing_filename_for_raw_bytes(self, client):
        with pytest.raises(LenzUsageError) as exc_info:
            client.extract_file(b"some bytes")
        assert "filename is required" in str(exc_info.value)
        assert exc_info.value.param == "filename"

    def test_missing_filename_for_stream_without_name(self, client):
        stream = io.BytesIO(b"some bytes")
        with pytest.raises(LenzUsageError) as exc_info:
            client.extract_file(stream)
        assert "filename is required" in str(exc_info.value)
        assert exc_info.value.param == "filename"

    def test_unsupported_file_format(self, client):
        with pytest.raises(LenzUsageError) as exc_info:
            client.extract_file(b"some bytes", filename="notes.txt")
        assert (
            "Unsupported file format for extraction: notes.txt. Only Word (.docx), PDF (.pdf), "
            "and image (.png, .jpg, .webp) files are supported." == str(exc_info.value)
        )
        assert exc_info.value.param == "file"


class TestExtractFileAPIErrors:
    def test_validation_error_422(self, client, tmp_path: Path):
        file_path = tmp_path / "doc.pdf"
        file_path.write_bytes(b"%PDF-1.4 test")

        with respx.mock(base_url=BASE) as respx_mock:
            respx_mock.post("/extract/file").respond(
                422,
                json={"detail": "Document text could not be extracted.", "code": "unreadable_document"},
            )
            with pytest.raises(LenzValidationError) as exc_info:
                client.extract_file(file_path)
            assert exc_info.value.status_code == 422
            assert exc_info.value.code == "unreadable_document"

    def test_validation_error_preserves_review_family_codes(self, client, tmp_path: Path):
        file_path = tmp_path / "doc.pdf"
        file_path.write_bytes(b"%PDF-1.4 test")

        with respx.mock(base_url=BASE) as respx_mock:
            respx_mock.post("/extract/file").respond(
                422,
                json={
                    "detail": "focus is at most 300 characters.",
                    "code": "validation_error",
                    "errors": [{"loc": ["focus"], "msg": "focus is at most 300 characters.", "type": "value_error"}],
                },
            )
            with pytest.raises(LenzValidationError) as exc_info:
                client.extract_file(file_path, focus="x" * 301)
            assert exc_info.value.status_code == 422
            assert exc_info.value.code == "validation_error"

        with respx.mock(base_url=BASE) as respx_mock:
            respx_mock.post("/extract/file").respond(
                422,
                json={
                    "detail": "Unsupported language 'xx'.",
                    "code": "unsupported_language",
                    "errors": [
                        {"loc": ["language"], "msg": "Unsupported language 'xx'.", "type": "unsupported_language"}
                    ],
                },
            )
            with pytest.raises(LenzValidationError) as exc_info:
                client.extract_file(file_path, language="xx")
            assert exc_info.value.status_code == 422
            assert exc_info.value.code == "unsupported_language"

    def test_rate_limit_429(self, client, tmp_path: Path):
        file_path = tmp_path / "doc.pdf"
        file_path.write_bytes(b"%PDF-1.4 test")

        with respx.mock(base_url=BASE) as respx_mock:
            respx_mock.post("/extract/file").respond(
                429,
                json={"detail": "Daily limit reached.", "code": "extract_daily_limit", "retry_after": 3600},
            )
            with pytest.raises(LenzRateLimitError) as exc_info:
                client.extract_file(file_path)
            assert exc_info.value.status_code == 429
            assert exc_info.value.code == "extract_daily_limit"


class TestAsyncExtractFile:
    @pytest.mark.asyncio
    async def test_async_extract_file_path(self, async_client, tmp_path: Path):
        file_path = tmp_path / "async_report.docx"
        file_path.write_bytes(b"PK\x03\x04fake docx")

        with respx.mock(base_url=BASE) as respx_mock:
            route = respx_mock.post("/extract/file").respond(200, json=_MOCK_EXTRACT)
            res = await async_client.extract_file(file_path, language="fr", focus="earnings", locate=True)
            assert isinstance(res, ExtractedClaims)
            assert res.status == "ready"
            assert route.called
            req = route.calls.last.request
            assert b"filename=\"async_report.docx\"" in req.content
            assert b"name=\"language\"\r\n\r\nfr" in req.content
            assert b"name=\"focus\"\r\n\r\nearnings" in req.content
            assert b"name=\"locate\"\r\n\r\ntrue" in req.content

    @pytest.mark.asyncio
    async def test_async_extract_file_bytes(self, async_client):
        with respx.mock(base_url=BASE) as respx_mock:
            route = respx_mock.post("/extract/file").respond(200, json=_MOCK_EXTRACT)
            res = await async_client.extract_file(b"\x89PNG fake image", filename="img.png")
            assert isinstance(res, ExtractedClaims)
            assert route.called
            assert b"filename=\"img.png\"" in route.calls.last.request.content

    @pytest.mark.asyncio
    async def test_async_extract_file_validation_error(self, async_client, tmp_path: Path):
        with pytest.raises(LenzUsageError) as exc_info:
            await async_client.extract_file(tmp_path / "nonexistent.pdf")
        assert "No such file" in str(exc_info.value)
