"""Shared helper functions and types for file reviews and binary document handling."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from ._core import _supplied_sources_body
from .errors import LenzUsageError
from .models import (
    FileInput as FileInput,
    ReviewEscalateInput as ReviewEscalateInput,
    SuppliedSource,
)


def _serialize_escalate(
    escalate: ReviewEscalateInput | dict[str, Any] | None,
    *,
    verdicts: list[str] | None = None,
    confidence: list[str] | None = None,
    max_verifications: int | None = None,
    depth: str | None = None,
    max_assessments: int | None = None,
    max_citations: int | None = None,
    suggest_edits: bool | None = None,
) -> str | None:
    """Build JSON string for escalate parameter in multipart review request."""
    data: dict[str, Any] = {}
    if escalate is not None:
        if not isinstance(escalate, dict):
            raise LenzUsageError(
                f"escalate must be a dict (got {type(escalate).__name__}).",
                code="invalid_argument",
                param="escalate",
            )
        data.update(escalate)

    if verdicts is not None:
        data["verdicts"] = verdicts
    if confidence is not None:
        data["confidence"] = confidence
    if max_verifications is not None:
        data["max_verifications"] = max_verifications
    if depth is not None:
        data["depth"] = depth
    if max_assessments is not None:
        data["max_assessments"] = max_assessments
    if max_citations is not None:
        data["max_citations"] = max_citations
    if suggest_edits is not None:
        data["suggest_edits"] = bool(suggest_edits)

    if not data:
        return None
    return json.dumps(data)


def _serialize_supplied_sources(
    sources: Sequence[SuppliedSource | dict[str, Any]] | None,
) -> str | None:
    """Serialize supplied_sources to a JSON string for multipart form data."""
    if sources is None:
        return None
    raw_list = _supplied_sources_body(sources, param="supplied_sources")
    return json.dumps(raw_list)


def _prepare_review_file_multipart(
    file: FileInput,
    *,
    filename: str | None = None,
    content_type: str | None = None,
    max_assessments: int | None = None,
    max_citations: int | None = None,
    suggest_edits: bool | None = None,
    language: str = "",
    webhook_url: str | None = None,
    visibility: str = "private",
    supplied_sources: Sequence[SuppliedSource | dict[str, Any]] | None = None,
    escalate: ReviewEscalateInput | dict[str, Any] | None = None,
    verdicts: list[str] | None = None,
    confidence: list[str] | None = None,
    max_verifications: int | None = None,
    depth: str | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Validate file input and construct httpx files and data dictionaries.

    Accepts Word (.docx), PDF (.pdf), and image (.png, .jpg, .webp) files.
    """
    # 1. Resolve file content, filename, content_type
    file_bytes: bytes
    resolved_filename: str

    if isinstance(file, (str, os.PathLike)):
        p = Path(file)
        if not p.is_file():
            raise LenzUsageError(f"No such file: {file}", code="invalid_argument", param="file")
        try:
            file_bytes = p.read_bytes()
        except OSError as exc:
            raise LenzUsageError(f"Cannot read file {file}: {exc}", code="invalid_argument", param="file") from exc
        resolved_filename = filename if filename is not None else p.name
    elif isinstance(file, bytes):
        file_bytes = file
        if filename is None:
            raise LenzUsageError(
                "filename is required when file is passed as raw bytes or file-like object.",
                code="invalid_argument",
                param="filename",
            )
        resolved_filename = filename
    elif hasattr(file, "read"):
        try:
            content = file.read()
            if isinstance(content, str):
                file_bytes = content.encode("utf-8")
            else:
                file_bytes = bytes(content)
        except Exception as exc:
            raise LenzUsageError(f"Cannot read file-like object: {exc}", code="invalid_argument", param="file") from exc
        if filename is None:
            name_attr = getattr(file, "name", None)
            if isinstance(name_attr, str) and name_attr:
                resolved_filename = Path(name_attr).name
            else:
                raise LenzUsageError(
                    "filename is required when file is passed as raw bytes or file-like object.",
                    code="invalid_argument",
                    param="filename",
                )
        else:
            resolved_filename = filename
    else:
        raise LenzUsageError(
            f"file must be a path, bytes, or binary stream (got {type(file).__name__}).",
            code="invalid_argument",
            param="file",
        )

    # Validate filename extension: .docx, .pdf, .png, .jpg, .jpeg, and .webp are supported by POST /review/file
    lower_fn = resolved_filename.lower()
    if lower_fn.endswith(".docx"):
        resolved_content_type = (
            content_type or "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
    elif lower_fn.endswith(".pdf"):
        resolved_content_type = content_type or "application/pdf"
    elif lower_fn.endswith(".png"):
        resolved_content_type = content_type or "image/png"
    elif lower_fn.endswith(".jpg") or lower_fn.endswith(".jpeg"):
        resolved_content_type = content_type or "image/jpeg"
    elif lower_fn.endswith(".webp"):
        resolved_content_type = content_type or "image/webp"
    else:
        raise LenzUsageError(
            f"Unsupported file format for review: {resolved_filename}. Only Word (.docx), PDF (.pdf), "
            "and image (.png, .jpg, .webp) files are supported.",
            code="invalid_argument",
            param="file",
        )

    files = {
        "file": (resolved_filename, file_bytes, resolved_content_type),
    }

    # Allowed top-level form fields on the server: language, visibility, webhook_url, escalate, supplied_sources.
    # max_assessments, max_citations, and suggest_edits belong inside the escalate JSON object.
    data: dict[str, str] = {}
    if language:
        data["language"] = language
    if webhook_url is not None:
        data["webhook_url"] = webhook_url
    if visibility:
        data["visibility"] = visibility

    escalate_json = _serialize_escalate(
        escalate,
        verdicts=verdicts,
        confidence=confidence,
        max_verifications=max_verifications,
        depth=depth,
        max_assessments=max_assessments,
        max_citations=max_citations,
        suggest_edits=suggest_edits,
    )
    if escalate_json is not None:
        data["escalate"] = escalate_json

    sources_json = _serialize_supplied_sources(supplied_sources)
    if sources_json is not None:
        data["supplied_sources"] = sources_json

    return files, data


def _sanitize_filename(name: str | None, default_filename: str) -> str:
    """Sanitize extracted filename to a safe basename, rejecting path separators and NUL bytes."""
    if not name or "\x00" in name:
        return default_filename
    cleaned = re.sub(r"[/\\]+", "/", name).strip()
    base = Path(cleaned).name.strip()
    if not base or base in (".", ".."):
        return default_filename
    return base


def _parse_content_disposition_filename(header_value: str | None, default_filename: str) -> str:
    """Extract filename from Content-Disposition header, handling RFC 5987 filename*."""
    if not header_value:
        return default_filename

    # Check for RFC 5987 filename*=charset'lang'encoded_value
    match_rfc = re.search(r"filename\*\s*=\s*([^;]+)", header_value, re.IGNORECASE)
    if match_rfc:
        val = match_rfc.group(1).strip().strip("\"'")
        parts = val.split("'", 2)
        if len(parts) == 3:
            encoding, _, encoded_text = parts
            try:
                raw_fn = unquote(encoded_text, encoding=encoding or "utf-8")
                return _sanitize_filename(raw_fn, default_filename)
            except Exception:
                pass
        else:
            try:
                raw_fn = unquote(val)
                return _sanitize_filename(raw_fn, default_filename)
            except Exception:
                pass

    # Standard filename="..." or filename=...
    match_fn = re.search(r'filename\s*=\s*(?:"([^"]+)"|([^;\s]+))', header_value, re.IGNORECASE)
    if match_fn:
        raw_fn = match_fn.group(1) or match_fn.group(2)
        return _sanitize_filename(raw_fn, default_filename)

    msg = EmailMessage()
    msg["content-disposition"] = header_value
    fn = msg.get_filename()
    if fn:
        return _sanitize_filename(fn, default_filename)

    return default_filename
