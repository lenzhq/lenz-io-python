"""Pydantic models mirroring the public Lenz API response surface.

Kept hand-written and small so customers can audit them. The shapes
mirror ``lenz/api/schemas/public_api.py`` server-side; contract tests
in ``tests/test_contract.py`` pin the cross-language invariant against
frozen JSON fixtures.

These models are the public, semver-stable surface. Renames here are
breaking changes that require a SDK major bump.

Vocabulary (applies across every claim-shaped response):

- ``claim``       : str           — the framed claim text
- ``verdict``     : str           — "True" | "Mostly True" | "Mixed" | "Mostly False" | "False" | "Error"
- ``confidence``  : str           — "high" | "medium" | "low" (categorical)
- ``lenz_score``  : int | None    - 1-10 integer (deep / list; /assess omits)
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Lax(BaseModel):
    """Base model that tolerates extra fields.

    The Lenz API may add fields in minor versions; we don't want to break
    customers' deserialisation when that happens. Strict validation runs
    in ``tests/test_contract.py`` via a per-test ``extra="forbid"``
    override so rename misses don't slip through silently.
    """

    model_config = ConfigDict(extra="allow")


#: Closed set of failure causes on a ``failed`` verification, mirroring the
#: Node SDK's ``FailureClass`` union. Exported for callers who want exhaustive
#: matching:
#:
#:     from lenz_io import FailureClass
#:
#: The model fields themselves stay ``str`` — the SDK must not reject a class
#: the server adds after this release was cut.
FailureClass = Literal[
    "upstream_unavailable",
    "insufficient_evidence",
    "invalid_input",
    "cancelled",
    "internal",
]


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

    ``score`` is a panelist-level 1-10 sub-score, distinct from the
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

    Later: ``visibility`` returns — 'private' | 'unlisted' | 'public'. It
    echoes what you set on submit ('private'/'unlisted' are settable;
    'public' can only be read, for listed claims). ``url`` stays dropped.
    """

    verification_id: str = ""
    claim: str = ""
    # 'private' | 'unlisted' | 'public'. Read-back of the claim's visibility.
    visibility: str = ""
    domain: str = ""
    entities: list[EntityRef] = Field(default_factory=list)
    presumed_intent: str = ""
    # Verdict block (flat)
    verdict: str = ""  # "True" | "Mostly True" | "Mixed" | "Mostly False" | "False" | "Error"
    confidence: str = "low"  # "high" | "medium" | "low"
    lenz_score: int | None = None  # 1-10 integer
    # The analysis's key finding: one declarative sentence stating the
    # most important fact it established (2.6.0). "" on legacy claims
    # that were never backfilled.
    key_finding: str = ""
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
    # The analysis's key finding (2.6.0). See ``Verification.key_finding``.
    key_finding: str = ""
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
    verdict: str = ""  # "True" | "Mostly True" | "Mixed" | "Mostly False" | "False" | "Error"
    confidence: str = "low"  # "high" | "medium" | "low"
    verification_url: str | None = None


class AssessResponse(_Lax):
    """Output of ``POST /assess``.

    ``claims`` is one entry per atomic_claim that framing identified in
    the input. Multiclaim inputs return N entries. ``error`` is set when
    framing returns zero claims.

    When ``claims`` is empty, ``error_code`` disambiguates why:
    ``'ambiguous'`` → the input was vague but framing produced specific
    readings in ``candidate_claims`` (assess one of them); ``'no_claim'``
    → genuinely not a checkable claim. Both fields default empty, so older
    servers that don't send them degrade to the plain ``error`` message.
    """

    claims: list[AssessClaim] = Field(default_factory=list)
    error: str | None = None
    error_code: str = ""  # '' | 'ambiguous' | 'no_claim'
    candidate_claims: list[str] = Field(default_factory=list)


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
    # WHY it failed — the closed set is ``FailureClass`` (import it for
    # exhaustive matching). The annotation stays ``str`` on purpose: a
    # ``Literal`` here would make an unknown class the server adds later a
    # hard ValidationError, and every other field on this model is lax.
    # Rows predating 2026-08 omit this and ``retryable`` (the derived retry
    # signal — true iff ``upstream_unavailable``).
    failure_class: str = ""
    retryable: bool | None = None


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


class UsageCredits(_Lax):
    """The account's credit balance — the one pool every capability spends.

    Every billable call debits this pool at the weight in :attr:`Usage.costs`
    (``/verify`` is 10 credits — 5 at ``depth="low"``, published as
    ``cost_options["verify"]["depth"]["low"]`` — ``/assess`` and ``/ask`` are 1, ``/extract`` is
    free). Two buckets: the monthly allowance for the current plan, which
    resets at ``resets_at``, and non-expiring ``bonus`` credits from grants and
    top-ups, spent only once the allowance is gone. ``remaining`` covers both
    and is what a call is checked against.

    Servers predating the credit pool (before 2026-08-29) don't send this block
    at all, and it then reads as all-zero — check ``usage.credits.total``
    before trusting the balance.
    """

    total: int = 0
    used: int = 0
    remaining: int = 0
    bonus: int = 0
    resets_at: str | None = None


class UsageCapacity(_Lax):
    """One capability's share of the credit pool, in that capability's own unit.

    These are **projections, not allowances**. Every billable capability draws
    on the single balance in :attr:`Usage.credits`, at the weight in
    :attr:`Usage.costs`; this block answers "how many ``/verify`` calls could I
    still make if I spent everything on them". Spending on any capability moves
    every block, and the division floors — a ``verify`` block (weight 10) ticks
    once per 10 credits spent anywhere.

    - ``quota_*``  — the monthly allowance projected into this unit; resets
      every period (see :attr:`Usage.quota_resets_at`). ``quota_used`` is
      derived as ``quota_total - quota_remaining``, so ``quota_used +
      quota_remaining == quota_total`` always holds.
    - ``bonus``    — the non-expiring top-up bucket, in this unit. A user
      holding 5 bonus credits sees ``assess.bonus == 5`` and
      ``verify.bonus == 0``: 5 credits does not buy a verification.

    ``remaining`` is the usable capacity across both buckets.
    """

    quota_used: int = 0
    quota_total: int = 0
    quota_remaining: int = 0
    bonus: int = 0
    #: **Deprecated** alias of :attr:`bonus`, removed on 2026-11-29. It never
    #: meant the credit pool — before the pool existed it meant this
    #: capability's one-off top-up balance, which is exactly what ``bonus``
    #: reports. Reading it emits a ``DeprecationWarning``; it stays in
    #: ``model_dump()`` output (unwarned) for as long as the server sends it.
    credits: int = Field(
        default=0,
        deprecated=(
            "UsageCapacity.credits is deprecated and will be removed on 2026-11-29; "
            "use `bonus` — the same number, this capability's non-expiring top-up "
            "balance. The credit pool itself is `Usage.credits`."
        ),
    )
    remaining: int = 0

    @model_validator(mode="before")
    @classmethod
    def _mirror_bonus_and_credits(cls, data: Any) -> Any:
        """Keep ``bonus`` and its deprecated alias in step, in both directions.

        A server predating the credit pool sends only ``credits``; a server
        after the 2026-11-29 removal sends only ``bonus``. Mirroring here means
        both attributes read correctly either way, so the SDK never depends on
        which side of an API deploy it is talking to.
        """
        if not isinstance(data, dict):
            return data
        has_bonus, has_credits = data.get("bonus") is not None, data.get("credits") is not None
        if has_bonus and not has_credits:
            return {**data, "credits": data["bonus"]}
        if has_credits and not has_bonus:
            return {**data, "bonus": data["credits"]}
        return data


class UsageExtract(_Lax):
    """Daily ``/extract`` usage — a per-day rate limit, not credit-based."""

    calls_today: int = 0
    daily_limit: int = 0
    unlimited: bool = False


class Usage(_Lax):
    """Returned by ``GET /me/usage`` — the account's balance and what it buys.

    ``credits`` is the balance and ``costs`` is the price list (credits per
    call, keyed by capability). The ``verify`` / ``ask`` / ``assess`` blocks
    are **projections** of that one pool into each capability's unit — read
    whichever is convenient, they all describe the same money, and spending on
    one moves all of them.

    ``extract`` is free at the pool (``costs["extract"] == 0``) and is bounded
    by a per-account daily fair-use cap instead; it rejects with 429, never
    402.

    ``costs`` names capabilities, one entry each, at the default price.
    Prices that depend on a request PARAMETER live in :attr:`cost_options`,
    nested capability → parameter → value. Divide ``credits.remaining`` by one
    of those yourself for the low-depth count — there is deliberately no
    ``verify_low`` block beside ``verify``.
    """

    #: The tier slug — ``"free"`` | ``"plus"`` | ``"developer"`` | ``"scale"``.
    #: This is the field to branch on; it is stable.
    plan: str = ""
    #: The same tier as display copy (``"Developer"``). Separate from
    #: :attr:`plan` on purpose: this one is copy and may be reworded, so
    #: comparing against it will break on a rename that ought to be free.
    #: Empty on servers predating this field — fall back to :attr:`plan`.
    plan_label: str = ""
    quota_resets_at: str | None = None
    #: The credit balance — the authoritative number. Empty on older servers.
    credits: UsageCredits = Field(default_factory=UsageCredits)
    #: Credits per call, keyed by CAPABILITY, at its default price:
    #: ``{"verify": 10, "assess": 1, "ask": 1, "extract": 0}``. Empty on older
    #: servers. Read the weight from here rather than hard-coding it — new
    #: keys appear without an SDK release, and the SDK never rewrites the
    #: server's own key names.
    #:
    #: Contains capability names and nothing else. Prices that depend on a
    #: request parameter are in :attr:`cost_options`.
    costs: dict[str, int] = Field(default_factory=dict)
    #: Prices that depend on a request PARAMETER, nested capability →
    #: parameter → value::
    #:
    #:     {"verify": {"depth": {"standard": 10, "low": 5}}}
    #:
    #: Read as "on ``verify``, the ``depth`` parameter prices like this".
    #: Empty on servers predating this field.
    #:
    #: Every capability here also appears in :attr:`costs` at its default
    #: price, so reading only ``costs`` is imprecise, never wrong. A caller
    #: wanting "how many low-depth verifications can I afford" divides
    #: ``credits.remaining`` by ``cost_options["verify"]["depth"]["low"]``.
    #:
    #: Nested rather than flattened into ``costs`` as ``verify_low``, which is
    #: what this was at first: a flat map grows one sibling per tuning
    #: parameter, and anything iterating ``costs`` would count prices as
    #: capabilities.
    #:
    #: You are charged for the depth you **requested**, not the one served: a
    #: ``low`` request answered from a cached ``standard`` verdict still costs
    #: the ``low`` price. The ``depth`` echoed on a completed verification is
    #: what the verdict was PRODUCED with, so it can read ``standard`` on a
    #: ``low`` request — the echo describes the evidence, the charge follows
    #: the request.
    cost_options: dict[str, dict[str, dict[str, int]]] = Field(default_factory=dict)
    verify: UsageCapacity = Field(default_factory=UsageCapacity)
    ask: UsageCapacity = Field(default_factory=UsageCapacity)
    assess: UsageCapacity = Field(default_factory=UsageCapacity)
    extract: UsageExtract = Field(default_factory=UsageExtract)
    # Whether this key has a webhook signing secret provisioned. ``POST /verify``
    # with a ``webhook_url`` is rejected without one, so callers that rely on
    # webhook delivery can check this up front. Reports existence only — the
    # secret value is never exposed here (shown once at rotation, never again).
    # Defaults to ``False`` on servers predating this field.
    has_webhook_secret: bool = False


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
    "FailureClass",
    "LibraryItem",
    "LibraryList",
    "RelatedVerifications",
    "SimilarVerification",
    "Source",
    "TaskAccepted",
    "TaskStatus",
    "Usage",
    "UsageCapacity",
    "UsageCredits",
    "UsageExtract",
    "Verification",
    "VerificationList",
    "VerificationListItem",
]
