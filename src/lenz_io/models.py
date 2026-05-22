"""Pydantic models mirroring the public Lenz API response surface.

Kept hand-written and small so customers can audit them. The shapes
mirror ``lenz/api/schemas/public_api.py`` server-side; contract tests
in ``tests/test_contract.py`` pin the cross-language invariant against
frozen JSON fixtures.

These models are the public, semver-stable surface. Renames here are
breaking changes that require a SDK major bump.

Vocabulary (applies across every claim-shaped response):

- ``claim``       : str           — the framed claim text
- ``verdict``     : str           — "True" | "Mostly True" | "Misleading" | "False" | "Error"
- ``confidence``  : str           — "high" | "medium" | "low" (categorical)
- ``lenz_score``  : float | None  — 0–10 (deep / list; /assess omits)
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Lax(BaseModel):
    """Base model that tolerates extra fields.

    The Lenz API may add fields in minor versions; we don't want to break
    customers' deserialisation when that happens. Strict validation runs
    in ``tests/test_contract.py`` via a per-test ``extra="forbid"``
    override so rename misses don't slip through silently.
    """

    model_config = ConfigDict(extra="allow")


class Source(_Lax):
    """A single citation backing a verification."""

    source_name: str = ""
    title: str = ""
    url: str = ""
    snippet: str = ""
    stance: str = ""
    date: str = ""


class DebateSide(_Lax):
    """One side of the adversarial debate transcript."""

    role: str = ""
    argument: str = ""
    rebuttal: str = ""


class Assessment(_Lax):
    """One panelist's structured assessment.

    Each panelist emits exactly one category of warnings (logical fallacies
    for the Logic Examiner, missing context for the Context Analyst, weakest
    sources for the Source Auditor). The kind is implicit in ``focus_area``;
    all of them surface under a single ``warnings`` list.

    ``score`` is a panelist-level 0-10 sub-score, distinct from the
    top-level ``lenz_score`` on a ``Verification``.
    """

    panelist_name: str = ""
    focus_area: str = ""
    score: float | None = None
    reasoning: str = ""
    warnings: list[str] = Field(default_factory=list)


class Audit(_Lax):
    """Nested explainability block — for callers who want the panel's work."""

    adjudication_summary: str = ""
    assessments: list[Assessment] = Field(default_factory=list)
    debate_pro: DebateSide | None = None
    debate_con: DebateSide | None = None
    panel_agreement: str = ""


class CandidateClaim(_Lax):
    """One of multiple distinct claims framing found in the submitted text."""

    text: str = ""
    domain: str = ""


class EntityRef(_Lax):
    """An entity referenced in the claim.

    ``qid`` is the Wikidata Q identifier (e.g. ``Q42``) when the entity
    was resolved against Lenz's internal catalog; ``None`` otherwise.
    """

    name: str = ""
    qid: str | None = None


class SimilarVerification(_Lax):
    """An existing public verification that semantically resembles the submitted text.

    Same vocabulary as ``Verification`` — flat ``verdict`` / ``confidence`` /
    ``lenz_score`` at top level, no nested ``Verdict`` object.
    """

    verification_id: str = ""
    claim: str = ""
    verdict: str = ""
    confidence: str = "low"
    lenz_score: float | None = None
    url: str = ""
    distance: float = 0.0


class Verification(_Lax):
    """Full verification report — returned by ``verify_and_wait``,
    ``verifications.get``, the ``/verify/status/{task_id}`` polling
    endpoint, and the webhook payload.

    The verdict block is FLAT at top level (was nested ``Verdict`` object
    pre-unify). ``created_at`` + ``modified_at`` are the only timestamp
    fields on the API surface — editorial ``published_at`` is internal-only.
    """

    verification_id: str = ""
    url: str = ""
    claim: str = ""
    domain: str = ""
    entities: list[EntityRef] = Field(default_factory=list)
    presumed_intent: str = ""
    # Verdict block (flat)
    verdict: str = ""  # "True" | "Mostly True" | "Misleading" | "False" | "Error"
    confidence: str = "low"  # "high" | "medium" | "low"
    lenz_score: float | None = None  # 0–10
    executive_summary: str = ""
    warnings: list[str] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    audit: Audit = Field(default_factory=Audit)
    created_at: str | None = None
    modified_at: str | None = None
    visibility: str | None = None


class VerificationListItem(_Lax):
    """Compact item for the verifications list endpoint and the public library list.

    Both ``GET /api/v1/library`` and ``GET /api/v1/verifications`` return
    the same per-item shape. ``visibility`` is the literal string
    ``'public'`` on /library (the only visibility surfaced there);
    /verifications carries the owner's actual visibility.
    """

    verification_id: str = ""
    url: str = ""
    claim: str = ""
    domain: str = ""
    entities: list[EntityRef] = Field(default_factory=list)
    verdict: str = ""
    confidence: str = "low"
    lenz_score: float | None = None
    executive_summary: str = ""
    created_at: str | None = None
    modified_at: str | None = None
    visibility: str = ""


class VerificationList(_Lax):
    items: list[VerificationListItem] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20


class RelatedVerifications(_Lax):
    """Wrapper for ``GET /verifications/{id}/related``."""

    items: list[SimilarVerification] = Field(default_factory=list)


class LibraryItem(VerificationListItem):
    """Same shape as VerificationListItem on the public Library list."""


class LibraryList(_Lax):
    items: list[LibraryItem] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20


class ExtractedEntity(_Lax):
    """An entity surfaced by ``/extract``. ``type`` is the framing category
    (``person`` | ``org`` | ``place`` | ``topic``)."""

    name: str = ""
    type: str = ""


class ExtractedClaims(_Lax):
    """Output of ``POST /extract``."""

    status: str = ""
    claim: str = ""
    identified_claims: list[str] = Field(default_factory=list)
    candidate_claims: list[str] = Field(default_factory=list)
    domain: str = ""
    key_entities: list[ExtractedEntity] = Field(default_factory=list)
    presumed_intent: str = ""
    original_input: str = ""


class AssessClaim(_Lax):
    """Per-claim entry in an ``AssessResponse.claims`` list.

    Lean shape by design — no model_votes, no panel identity. The
    ``verification_url`` (when present) points at the full
    ``ClaimDetailOut`` payload at ``GET /api/v1/verifications/{id}`` for
    callers that want citations and the full audit trail.
    """

    claim: str = ""
    verdict: str = ""  # "True" | "Mostly True" | "Misleading" | "False" | "Error"
    confidence: str = "low"  # "high" | "medium" | "low"
    verification_url: str | None = None


class AssessResponse(_Lax):
    """Output of ``POST /assess``.

    ``claims`` is one entry per atomic_claim that framing identified in
    the input. Multiclaim inputs return N entries. ``error`` is set when
    framing returns zero claims.
    """

    claims: list[AssessClaim] = Field(default_factory=list)
    error: str | None = None


class TaskAccepted(_Lax):
    """Returned by ``POST /verify`` and per item of ``POST /verify/batch``."""

    task_id: str = ""
    claim_text: str = ""


class BatchAccepted(_Lax):
    batch_id: str = ""
    items: list[TaskAccepted] = Field(default_factory=list)


class TaskStatus(_Lax):
    """Returned by ``GET /verify/status/{task_id}``."""

    status: str = ""  # processing | needs_input | completed | failed
    reason: str = ""  # populated when status == 'needs_input'
    progress: dict[str, Any] = Field(default_factory=dict)
    result: Verification | None = None
    # needs_input branches
    claims: list[CandidateClaim] = Field(default_factory=list)
    candidates: list[str] = Field(default_factory=list)
    similar_claims: list[SimilarVerification] = Field(default_factory=list)
    # failure branches
    failure_reason: str = ""
    failure_detail: str = ""


class Usage(_Lax):
    """Returned by ``GET /me/usage``."""

    plan: str = ""
    credits_used: int = 0
    credits_total: int = 0
    credits_resets_at: str | None = None
    extract_calls_today: int = 0
    extract_daily_limit: int = 0


class AskMessage(_Lax):
    """One message in an ``/ask`` conversation thread."""

    role: str = ""  # "user" | "expert"
    content: str = ""
    created_at: str = ""


class AskHistory(_Lax):
    """Returned by ``GET /ask/{verification_id}``."""

    messages: list[AskMessage] = Field(default_factory=list)
    exchanges_used: int = 0
    exchange_limit: int = 0
    can_send: bool = False


class AskReply(_Lax):
    """Returned by ``POST /ask/{verification_id}``."""

    reply: str = ""


__all__ = [
    "AskHistory",
    "AskMessage",
    "AskReply",
    "AssessClaim",
    "AssessResponse",
    "Assessment",
    "Audit",
    "BatchAccepted",
    "CandidateClaim",
    "DebateSide",
    "EntityRef",
    "ExtractedClaims",
    "ExtractedEntity",
    "LibraryItem",
    "LibraryList",
    "RelatedVerifications",
    "SimilarVerification",
    "Source",
    "TaskAccepted",
    "TaskStatus",
    "Usage",
    "Verification",
    "VerificationList",
    "VerificationListItem",
]
