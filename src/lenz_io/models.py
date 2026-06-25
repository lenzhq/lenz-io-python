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
- ``lenz_score``  : int | None    - 0-10 integer (deep / list; /assess omits)
"""

from __future__ import annotations

from typing import Any, Literal

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
    date: str = ""


class DebateSide(_Lax):
    """One side of the adversarial debate transcript."""

    role: str = ""
    argument: str = ""
    rebuttal: str = ""


class Assessment(_Lax):
    """One panelist's structured assessment.

    Each panelist emits exactly one category of warnings (logical fallacies
    for the Logic Examiner, precision issues for the Precision Analyst,
    weakest sources for the Source Auditor; verifications from before
    2026-06 carry missing context for the retired Context Analyst). The
    kind is implicit in ``focus_area``; all of them surface under a single
    ``warnings`` list.

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
    lenz_score: int | None = None
    url: str = ""
    distance: float = 0.0


class Verification(_Lax):
    """Full verification report — returned by ``verify_and_wait``,
    ``verifications.get``, the ``/verify/status/{task_id}`` polling
    endpoint, and the webhook payload.

    The verdict block is FLAT at top level (was nested ``Verdict`` object
    pre-unify). ``created_at`` + ``modified_at`` are the only timestamp
    fields on the API surface — editorial ``published_at`` is internal-only.

    1.1.0: dropped ``url`` and ``visibility``. API claims are private by
    default and referenced by ``verification_id`` only. Cache-hit on
    another customer's claim is transparent — the customer always sees
    their own ``verification_id``, never another customer's.
    """

    verification_id: str = ""
    claim: str = ""
    domain: str = ""
    entities: list[EntityRef] = Field(default_factory=list)
    presumed_intent: str = ""
    # Verdict block (flat)
    verdict: str = ""  # "True" | "Mostly True" | "Misleading" | "False" | "Error"
    confidence: str = "low"  # "high" | "medium" | "low"
    lenz_score: int | None = None  # 0-10 integer
    executive_summary: str = ""
    warnings: list[str] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    audit: Audit = Field(default_factory=Audit)
    created_at: str | None = None
    modified_at: str | None = None
    # Output language (ISO 639-1). Always populated by the server when
    # the SDK is fresh; defaulted to ``'en'`` for resilience against
    # older cached payloads that lack the field.
    language: str = "en"


class VerificationListItem(_Lax):
    """Compact item for the verifications list endpoint and the public
    library list. Slim shape — no ``url`` (reference by
    ``verification_id``), no ``visibility`` (1.1.0).
    """

    verification_id: str = ""
    claim: str = ""
    domain: str = ""
    entities: list[EntityRef] = Field(default_factory=list)
    verdict: str = ""
    confidence: str = "low"
    lenz_score: int | None = None
    executive_summary: str = ""
    created_at: str | None = None
    modified_at: str | None = None
    # Output language (ISO 639-1). See ``Verification.language``.
    language: str = "en"


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
    # Output language (ISO 639-1). Echoes the language requested on the
    # call, or ``'en'`` when unspecified. Verdict enums always English.
    language: str = "en"
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
    # failure branches. The server's failed response is
    # ``{"status": "failed", "error": "..."}`` — ``error`` is the live wire
    # field. ``failure_reason`` / ``failure_detail`` are kept for forward/back
    # compatibility and other channels; read precedence is
    # ``error or failure_detail or failure_reason``.
    error: str = ""
    failure_reason: str = ""
    failure_detail: str = ""


class BatchItemResult(_Lax):
    """Per-item outcome from :meth:`Lenz.verify_batch_and_wait`.

    A client-side composition type — NOT a wire shape (the server never emits
    it, so it has no contract fixture). One entry per task that
    ``POST /verify/batch`` returned, in input order.

    ``status`` is a client-side rollup:

    - ``completed``    — ``verification`` is set (and ``status_detail`` carries the raw poll).
    - ``needs_input``  — paused for caller input; inspect ``status_detail`` (reason / claims / candidates).
    - ``failed``       — terminal failure (or completed-without-result); ``status_detail`` carries the diagnostic.
    - ``timeout``      — the deadline elapsed before this task reached a terminal state; ``status_detail`` is ``None``.
    """

    task_id: str = ""
    claim_text: str = ""
    status: Literal["completed", "needs_input", "failed", "timeout"]
    verification: Verification | None = None
    status_detail: TaskStatus | None = None


class UsageCapacity(_Lax):
    """Per-capability remaining capacity (``verify`` / ``ask``).

    Two buckets, kept separate on purpose:

    - ``quota_*``  — the recurring monthly allowance for the current plan;
      resets every period (see :attr:`Usage.quota_resets_at`).
      ``quota_remaining`` is ``quota_total - quota_used`` (never negative).
    - ``credits``  — one-off top-up credits that do NOT reset monthly; spent
      only after the monthly quota is exhausted.

    ``remaining`` is the true usable capacity: ``quota_remaining + credits``.
    """

    quota_used: int = 0
    quota_total: int = 0
    quota_remaining: int = 0
    credits: int = 0
    remaining: int = 0


class UsageExtract(_Lax):
    """Daily ``/extract`` usage — a per-day rate limit, not credit-based."""

    calls_today: int = 0
    daily_limit: int = 0
    unlimited: bool = False


class Usage(_Lax):
    """Returned by ``GET /me/usage`` — usage + remaining capacity for the key.

    Monthly quota (resets at ``quota_resets_at``) and one-off top-up credits are
    reported separately per capability so callers can tell a recurring allowance
    apart from a purchased balance. ``assess`` is quota-only — there is no
    one-off assess credit pool, so its ``credits`` is always 0 and
    ``remaining == quota_remaining`` (not a bug).
    """

    plan: str = ""
    quota_resets_at: str | None = None
    verify: UsageCapacity = Field(default_factory=UsageCapacity)
    ask: UsageCapacity = Field(default_factory=UsageCapacity)
    assess: UsageCapacity = Field(default_factory=UsageCapacity)
    extract: UsageExtract = Field(default_factory=UsageExtract)


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
    """Returned by ``POST /ask/{verification_id}``.

    ``content`` is the assistant's reply text in a small markdown
    subset:

    - ``**bold**`` and ``*italic*``
    - ``- `` or ``* `` bullet lists
    - Blank-line paragraph breaks; single newlines inside a paragraph
      mean line break

    The model only produces these — no headings, no tables, no code
    blocks. Pass it through any markdown library or display it
    verbatim. See https://lenz.io/docs/quickstart#ask-reply-format.

    Pre-1.0.2 the SDK declared a single ``reply`` field that never
    matched the wire — the server has always returned
    ``{role, content, created_at}``. 1.0.2 aligned the typed surface.
    """

    role: str = ""  # 'expert' on every reply (the assistant turn)
    content: str = ""  # markdown-subset prose (see class docstring)
    created_at: str = ""


__all__ = [
    "AskHistory",
    "AskMessage",
    "AskReply",
    "AssessClaim",
    "AssessResponse",
    "Assessment",
    "Audit",
    "BatchAccepted",
    "BatchItemResult",
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
