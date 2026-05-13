"""Pydantic models mirroring the public Lenz API response surface.

Kept hand-written and small so customers can audit them. The shapes
mirror ``lenz/api/schemas/public_api.py`` server-side; contract tests
pin the cross-language invariant.

These models are the public, semver-stable surface. Renames here are
breaking changes that require a SDK major bump.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Lax(BaseModel):
    """Base model that tolerates extra fields.

    The Lenz API may add fields in minor versions; we don't want to break
    customers' deserialisation when that happens. Strict validation only
    runs on the contract-test path.
    """

    model_config = ConfigDict(extra="allow")


class Verdict(_Lax):
    """The marquee block on a verification — what 80% of callers read."""

    label: str = ""
    score: float | None = None
    confidence: float | None = None


class Source(_Lax):
    """A single citation backing a verification."""

    title: str = ""
    url: str = ""
    snippet: str = ""
    stance: str = ""


class DebateSide(_Lax):
    """One side of the adversarial debate transcript."""

    role: str = ""
    arguments: list[str] = Field(default_factory=list)


class Assessment(_Lax):
    """One panelist's structured assessment.

    Each panelist emits exactly one category of warnings (logical fallacies
    for the Logic Examiner, missing context for the Context Analyst, weakest
    sources for the Source Auditor). The kind is implicit in ``focus_area``;
    all of them surface under a single ``warnings`` list.
    """

    panelist_name: str = ""
    focus_area: str = ""
    score: float | None = None
    confidence: float | None = None
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
    """An existing public verification that semantically resembles the submitted text."""

    verification_id: str = ""
    claim: str = ""
    verdict_label: str = ""
    score: float | None = None
    url: str = ""
    distance: float = 0.0


class Verification(_Lax):
    """Full verification report — returned by `verify`, `get_verification`, etc."""

    verification_id: str = ""
    url: str = ""
    claim: str = ""
    domain: str = ""
    entities: list[EntityRef] = Field(default_factory=list)
    presumed_intent: str = ""
    verdict: Verdict = Field(default_factory=Verdict)
    executive_summary: str = ""
    warnings: list[str] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    audit: Audit = Field(default_factory=Audit)
    created_at: str | None = None
    published_at: str | None = None
    modified_at: str | None = None
    visibility: str | None = None


class VerificationListItem(_Lax):
    """Compact item for the verifications list endpoint."""

    verification_id: str = ""
    url: str = ""
    claim: str = ""
    domain: str = ""
    verdict: Verdict = Field(default_factory=Verdict)
    executive_summary: str = ""
    created_at: str | None = None
    visibility: str = ""


class VerificationList(_Lax):
    items: list[VerificationListItem] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20


class LibraryItem(VerificationListItem):
    """Same shape as VerificationListItem on the public Library list."""


class LibraryList(_Lax):
    items: list[LibraryItem] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20


class ExtractedClaims(_Lax):
    """Output of ``POST /extract``."""

    status: str = ""
    atomic_claim: str = ""
    identified_claims: list[str] = Field(default_factory=list)
    candidate_claims: list[str] = Field(default_factory=list)
    domain: str = ""
    key_entities: list[str] = Field(default_factory=list)
    presumed_intent: str = ""
    original_input: str = ""


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
    candidate_claims: list[str] = Field(default_factory=list)
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


class FollowupHistory(_Lax):
    messages: list[dict[str, Any]] = Field(default_factory=list)
    exchanges_used: int = 0
    exchange_limit: int = 0
    can_send: bool = False


class FollowupReply(_Lax):
    reply: str = ""


__all__ = [
    "Assessment",
    "Audit",
    "BatchAccepted",
    "CandidateClaim",
    "DebateSide",
    "ExtractedClaims",
    "FollowupHistory",
    "FollowupReply",
    "LibraryItem",
    "LibraryList",
    "SimilarVerification",
    "Source",
    "TaskAccepted",
    "TaskStatus",
    "Usage",
    "Verdict",
    "Verification",
    "VerificationList",
    "VerificationListItem",
]
