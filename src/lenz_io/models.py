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
                                    ("Error": an ``assess`` row with ``status == "failed"``)
- ``confidence``  : str           — "high" | "medium" | "low" (categorical)
- ``lenz_score``  : int | None    - 1-10 integer (deep / list; /assess omits)
"""

from __future__ import annotations

import functools
from collections.abc import Callable, ItemsView, KeysView, ValuesView
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

#: Where a validator lists the attributes it filled in that the response did
#: not carry. ``_Lax`` moves it to a private attribute; ``model_dump()`` leaves
#: those attributes out, so a dump is always the shape the server sent.
_DERIVED_KEY = "__lenz_derived__"


def _reads_both(*new_fields: str, always_hide: bool = False) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Mark what a ``_read_both_shapes`` validator derived, so ``model_dump()``
    shows the shape the server sent.

    ``new_fields`` are the attributes this release added for the newer
    response shape. Hidden from a dump: each of them the input did not carry
    (so an original-shape response dumps exactly as it did before this
    release), and, on a newer-shape input (one carrying any of them, or any
    input with ``always_hide``), every attribute the input did not carry. A
    value the validator rewrote (a failed row's ``verdict``) dumps as sent.
    """

    def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def inner(cls: Any, data: Any) -> Any:
            out = fn(cls, data)
            if not isinstance(data, dict) or not isinstance(out, dict):
                return out
            hidden = {k for k in new_fields if k not in data}
            if always_hide or any(k in data for k in new_fields):
                hidden |= {k for k in out if k not in data}
                hidden |= {k for k in cls.model_fields if k not in data}
            rewritten = {
                k: v
                for k, v in data.items()
                if k in out and not isinstance(v, (dict, list)) and out[k] is not v and out[k] != v
            }
            if not hidden and not rewritten:
                return out
            return {**out, _DERIVED_KEY: {"hidden": sorted(hidden), "sent": rewritten}}

        return inner

    return wrap


class _Lax(BaseModel):
    """Base model that tolerates extra fields.

    The Lenz API may add fields in minor versions; we don't want to break
    customers' deserialisation when that happens. Strict validation runs
    in ``tests/test_contract.py`` via a per-test ``extra="forbid"``
    override so rename misses don't slip through silently.
    """

    model_config = ConfigDict(extra="allow")

    # The attributes filled in from the other response shape (see
    # ``_reads_both``): readable, never dumped.
    _lenz_derived: frozenset[str] = PrivateAttr(default_factory=frozenset)
    _lenz_sent: dict[str, Any] = PrivateAttr(default_factory=dict)

    @model_validator(mode="after")
    def _collect_derived(self) -> Any:
        extra = self.__pydantic_extra__
        if extra and _DERIVED_KEY in extra:
            marks = extra.pop(_DERIVED_KEY)
            self._lenz_derived = frozenset(marks["hidden"])
            self._lenz_sent = dict(marks["sent"])
        return self

    @model_serializer(mode="wrap")
    def _dump_as_sent(self, handler: SerializerFunctionWrapHandler) -> Any:
        data = handler(self)
        if isinstance(data, dict):
            for key in self._lenz_derived:
                data.pop(key, None)
            for key, value in self._lenz_sent.items():
                if key in data:
                    data[key] = value
        return data


# ── Reading both response shapes ─────────────────────────────────────────
#
# The API is gaining a second, dated response shape that renames a handful of
# fields (``claim_text`` -> ``claim``, ``modified_at`` -> ``completed_at``, a
# failed item's ``error`` / ``error_code`` / ``failure_reason`` -> one
# ``failure`` block, ...). This release still asks for the original shape, but
# every model reads either one: the new name is a first-class attribute, and
# every old attribute keeps its old meaning, computed from whichever shape
# arrived. The helpers below are the shared rules.

#: The one code for "the input holds nothing that can be checked", in the
#: newer response shape. The original shape spells it per endpoint:
#: ``not_a_claim`` (/extract, /verify) and ``no_claim`` (/assess, /review).
NO_CHECKABLE_CLAIM = "no_checkable_claim"
_OLD_NO_CLAIM_CODES = ("not_a_claim", "no_claim")


def _new_code(code: Any) -> Any:
    """An old per-endpoint spelling of "nothing checkable" -> the one new code."""
    return NO_CHECKABLE_CLAIM if code in _OLD_NO_CLAIM_CODES else code


def _old_code(code: Any, old: str) -> Any:
    """The new code -> the spelling ``old`` this field always carried."""
    return old if code in (NO_CHECKABLE_CLAIM, *_OLD_NO_CLAIM_CODES) else code


def _utc_day(iso: Any) -> Any:
    """The UTC calendar day of an ISO-8601 string, or ``None``."""
    if not isinstance(iso, str) or not iso:
        return None
    try:
        when = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).date()


def _modified_at_from(created_at: Any, completed_at: Any) -> str | None:
    """The original ``modified_at`` rule: the completion time when it falls on a
    later UTC calendar day than the creation time, else ``None``."""
    created, completed = _utc_day(created_at), _utc_day(completed_at)
    if created is None or completed is None or completed <= created:
        return None
    return str(completed_at)


def _with_completion_times(data: Any) -> Any:
    """Fill ``completed_at`` and ``modified_at`` from whichever one arrived.

    ``modified_at`` from ``completed_at``: the original later-calendar-day
    rule. ``completed_at`` from ``modified_at``: exact when ``modified_at`` is
    set; when it is null (a same-day completion) the original shape does not
    say when the verification completed, so ``completed_at`` stays ``None``.
    """
    if not isinstance(data, dict):
        return data
    if "completed_at" in data and "modified_at" not in data:
        return {**data, "modified_at": _modified_at_from(data.get("created_at"), data.get("completed_at"))}
    if "modified_at" in data and "completed_at" not in data:
        return {**data, "completed_at": data.get("modified_at")}
    return data


def _mirror(data: dict[str, Any], new: str, old: str) -> dict[str, Any]:
    """Copy a field across a plain rename, in whichever direction is missing."""
    if new in data and old not in data:
        return {**data, old: data[new]}
    if old in data and new not in data:
        return {**data, new: data[old]}
    return data


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
    # The passage around the quoted sentence(s) on the source page, in the
    # page's own language: up to ~2,000 characters, and it may contain line
    # breaks. ``…`` marks a cut paragraph, `` … `` separates two passages.
    snippet: str = ""
    date: str = ""


class DebateSide(_Lax):
    """One side of the adversarial debate transcript."""

    role: str = ""
    argument: str = ""
    rebuttal: str = ""


class Assessment(_Lax):
    """One reviewer's structured assessment.

    Current verifications carry three reviewers, ``Reviewer A`` to
    ``Reviewer C``, all running the same checks over the evidence, plus
    ``Reviewer D`` and ``Reviewer E`` when those three disagree. Their
    ``focus_area`` reads ``"Sources, evidence fit and wording"``, and
    ``warnings`` holds the source issues, evidence gaps and precision issues
    that reviewer found, in one list.

    Older verifications carry specialist panelists instead, one warning
    category each: logical fallacies (Logic Examiner), precision issues
    (Precision Analyst), weakest sources (Source Auditor) and, before
    2026-06, missing context (Context Analyst).

    ``panelist_name`` is a display value, not a stable key — don't branch
    on it. ``score`` is a reviewer-level 1-10 sub-score, distinct from the
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
    """One of multiple distinct claims framing found in the submitted text.

    ``claim`` is the claim's text. ``text`` is its older name, the same
    string, kept for existing code (deprecated; either shape fills both).
    """

    claim: str = ""
    #: Deprecated: the same string as :attr:`claim`.
    text: str = Field(default="", json_schema_extra={"deprecated": True})
    domain: str = ""

    @model_validator(mode="before")
    @classmethod
    @_reads_both("claim")
    def _read_both_shapes(cls, data: Any) -> Any:
        return _mirror(data, "claim", "text") if isinstance(data, dict) else data


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


#: Closed set of ``coverage.status`` values. Exported for exhaustive matching;
#: the model field stays ``str`` so the SDK never rejects a status the server
#: adds after this release was cut.
CoverageStatus = Literal["covered", "uncovered", "pending_timestamp"]

#: Closed set of ``coverage.reasons`` values — why a verdict is NOT covered.
#: Deliberately smaller than the internal gate's vocabulary: ``plan`` and
#: ``depth`` are actionable, ``account`` means the account turned warranty
#: certificates off (a verification that already carries a certificate keeps
#: it), ``verdict`` is a product rule you design around, ``quality`` covers
#: everything you can neither act on nor define, and ``withdrawn`` /
#: ``issue_failed`` are statements about Lenz rather than about your claim.
CoverageReason = Literal[
    "plan",
    "account",
    "depth",
    "verdict",
    "quality",
    "withdrawn",
    "issue_failed",
]


class Coverage(_Lax):
    """Whether this verdict carries Lenz's warranty, for YOUR account.

    Present only when covered verification is enabled on the account's plan;
    ``None`` otherwise. It describes the pair (your account, this analysis),
    not the analysis alone — two accounts can hold two certificates over one
    cached verdict, so the block you see is never another customer's.

    ``reasons`` is empty exactly when ``status`` is not ``"uncovered"``.

    The money fields are three, not two: ``currency`` is ISO 4217 and the
    amounts are integers in **major units** — ``cap=10000`` means ten
    thousand, not a hundred. They are contract figures, not amounts a payment
    processor charges. Read ``currency``; do not assume EUR.
    """

    status: str = "uncovered"
    reasons: list[str] = Field(default_factory=list)
    certificate_id: str | None = None
    certificate_url: str | None = None
    #: The date the verdict is warranted AS OF — the analysis time, not the
    #: issue time. ``None`` when there is no certificate.
    as_of: str | None = None
    currency: str = ""
    cap: int = 0
    aggregate: int = 0
    terms_version: str = ""


class Certificate(_Lax):
    """The signed warranty certificate, byte-identical to the public document.

    Everything needed to verify the record **without Lenz**: ``leaf`` is the
    digest the ``signature`` is over, ``anchors`` carries the qualified
    (RFC 3161 / eIDAS) timestamp and the OpenTimestamps receipt, and
    ``verifier_url`` / ``keys_url`` point at the open-source checker and the
    published keys.

    A withdrawn certificate is still served — it is the record of what was
    warranted, and ``withdrawn_at`` is on it.
    """

    #: An INT on the wire, unlike ``record_version`` which is a string. Not a
    #: tidy asymmetry, but it is what the server sends: `record_version` is
    #: part of the signed leaf and has always been a string, while
    #: `document_version` versions the envelope around it.
    document_version: int = 0
    certificate_id: str = ""
    record_version: str = ""
    #: The signed payload: the exact statement, verdict, warnings, sources and
    #: caps. This is what the leaf is computed over — treat it as opaque and
    #: pass it to the verifier rather than reconstructing it.
    payload: dict[str, Any] = Field(default_factory=dict)
    leaf: str = ""
    signature: str | None = None
    key_id: str | None = None
    anchors: dict[str, Any] = Field(default_factory=dict)
    withdrawn_at: str | None = None
    keys_url: str = ""
    verifier_url: str = ""


class Verification(_Lax):
    """Full verification report — returned by ``verify_and_wait``,
    ``verifications.get``, the ``/verify/status/{task_id}`` polling
    endpoint, and the webhook payload.

    The verdict block is FLAT at top level (was nested ``Verdict`` object
    pre-unify). ``created_at`` and ``completed_at`` are its timestamps
    (``modified_at`` is the deprecated older one) — editorial
    ``published_at`` is internal-only.

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
    # 'standard' | 'low'. Read-back of the depth the verdict was actually
    # produced with — a 'low' request served from cache reads 'standard'.
    # "" on servers that predate the field (``_Lax`` tolerates its absence).
    depth: str = ""
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
    #: A suggested rewrite of ``claim`` that this verification's findings
    #: support, to use in place of the original sentence. It has not been
    #: verified itself: before using it, review it or run it through
    #: ``client.verify(...)``. ``None`` for a True verdict, when the findings
    #: establish no correction, and on verifications that predate the field
    #: (or a server that does not send it). On every verification, single or
    #: listed (``VerificationListItem`` carries it too). It answers the same
    #: question the claim answers: it may replace the claim's subject when the
    #: subject is the wrong part, negates the claim when the findings establish
    #: it is false but name no right answer, and is ``None`` when the findings
    #: only find no support. ``assess`` rows carry their own
    #: (``AssessClaim.suggested_rewrite``, with ``suggest_rewrite=True``).
    suggested_rewrite: str | None = None
    sources: list[Source] = Field(default_factory=list)
    audit: Audit = Field(default_factory=Audit)
    created_at: str | None = None
    #: When the verification completed. In the original response shape it is
    #: known only through ``modified_at``: set when that is, ``None`` on a
    #: same-day completion (and on servers that send neither).
    completed_at: str | None = None
    #: Deprecated: the completion time when it falls on a later UTC calendar
    #: day than ``created_at``, else ``None``. Read :attr:`completed_at`.
    modified_at: str | None = Field(default=None, json_schema_extra={"deprecated": True})
    # Output language (ISO 639-1). Always populated by the server when
    # the SDK is fresh; defaulted to ``'en'`` for resilience against
    # older cached payloads that lack the field.
    language: str = "en"
    #: Warranty state for YOUR account over this analysis.
    #:
    #: ``None`` for two reasons that are NOT "this verdict does not qualify":
    #: Lenz is not operating the warranty, or the call was unauthenticated
    #: (``verifications.get`` accepts anonymous callers, and an anonymous
    #: caller has no account for a warranty to attach to). A verdict that does
    #: not qualify carries the block with ``status="uncovered"`` and the
    #: reasons why.
    coverage: Coverage | None = None

    @model_validator(mode="before")
    @classmethod
    @_reads_both("completed_at")
    def _read_both_shapes(cls, data: Any) -> Any:
        return _with_completion_times(data)


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
    # A suggested rewrite of ``claim``. See ``Verification.suggested_rewrite``.
    suggested_rewrite: str | None = None
    created_at: str | None = None
    #: See ``Verification.completed_at``.
    completed_at: str | None = None
    #: Deprecated. See ``Verification.modified_at``.
    modified_at: str | None = Field(default=None, json_schema_extra={"deprecated": True})
    # Output language (ISO 639-1). See ``Verification.language``.
    language: str = "en"

    @model_validator(mode="before")
    @classmethod
    @_reads_both("completed_at")
    def _read_both_shapes(cls, data: Any) -> Any:
        return _with_completion_times(data)


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


class Position(_Lax):
    """One place in the text you sent: a claim's passage, or a citation's
    statement.

    ``start`` and ``end`` index the ``text`` you sent, in Unicode code
    points (not UTF-16 units or bytes), half-open: ``text[start:end]`` is
    the passage in Python, ``end`` exclusive. Both are ``None`` when the
    input was a URL: the page is not returned, so there is nothing to index,
    and ``text`` carries the passage instead.

    ``text`` is the passage as it appears in the text. It is ``None`` on a
    citation's ``position``, whose row already carries the ``statement``.
    """

    start: int | None = None
    end: int | None = None
    text: str | None = None


class ClaimLocation(_Lax):
    """Where the submitted text makes one claim.

    ``claim`` is exactly as in the claim list it belongs to
    (``ExtractedClaims.claim`` / ``identified_claims``, or a review's
    ``more_claims``). ``positions`` lists every place the text makes it, in
    text order, at most 10. It is ``None`` only when that claim could not be
    placed; on ``/extract`` every returned claim is placed.
    """

    claim: str = ""
    positions: list[Position] | None = None


#: The ``ExtractedClaims.status`` values this release knows about:
#:
#:     from lenz_io import ExtractStatus
#:
#: - ``ready`` — claims were found (and, with a ``focus``, at least one matched).
#: - ``not_a_claim`` — the text holds no verifiable factual claim at all (the
#:   newer response shape spells it ``no_checkable_claim``; this release
#:   reads both as ``not_a_claim``).
#: - ``no_match`` — claims were found, but none fall within the ``focus``.
#:
#: A DOCUMENTATION constant, not a guarantee. The model field stays ``str``,
#: because the SDK must not reject a status the server adds after this release
#: was cut — so ``result.status`` will NOT type-check where an
#: ``ExtractStatus`` is expected, and ``result.status == "no_matchh"`` is a
#: perfectly legal comparison. Use it to annotate your own handlers and to
#: enumerate the cases; do not expect it to catch a typo.
#:
#: The Node SDK's ``ExtractStatus`` is a union with an open ``string`` arm, so
#: it behaves the same way at the usage site for the same reason.
ExtractStatus = Literal[
    "ready",
    "not_a_claim",
    "no_match",
]


class ExtractedClaim(ClaimLocation):
    """One claim ``/extract`` found: ``claim``, and with ``locate=True`` its
    ``positions`` in the text (``None`` when the call did not locate, or the
    claims could not be located)."""


class ExtractedClaims(_Lax):
    """Output of ``POST /extract``.

    ``claims`` is every claim found, most check-worthy first, always a list
    (one entry for one claim, ``[]`` for none). With ``locate=True`` each
    entry's ``positions`` says where the text makes it.

    ``status`` is one of ``ExtractStatus``: ``ready``, ``not_a_claim``, or —
    when a ``focus`` was given and no claim fell within it — ``no_match``.
    ``no_match`` is a successful answer, not an error: ``claims`` is empty
    and the unfocused list is never substituted for it. In this release
    ``status`` keeps the ``not_a_claim`` spelling whichever response shape
    arrived (the newer shape spells it ``no_checkable_claim``).

    Deprecated, kept with their original meaning: ``claim`` is the first
    claim (``""`` for none), ``identified_claims`` the whole list when there
    are two or more (``[]`` for one), and ``locations`` the ``ClaimLocation``
    list of a located call (``None`` when ``locate`` was not set, when the
    extraction found no claims, or when the claims could not be located).
    Read ``claims`` instead: it is filled from either response shape.
    """

    status: str = ""
    claims: list[ExtractedClaim] = Field(default_factory=list)
    #: Deprecated: the first of :attr:`claims`.
    claim: str = Field(default="", json_schema_extra={"deprecated": True})
    #: Deprecated: :attr:`claims` when there are two or more, else ``[]``.
    identified_claims: list[str] = Field(default_factory=list, json_schema_extra={"deprecated": True})
    # Deprecated: always empty since 2026-09-12. Kept because the server
    # still sends the key.
    candidate_claims: list[str] = Field(default_factory=list, json_schema_extra={"deprecated": True})
    domain: str = ""
    key_entities: list[ExtractedEntity] = Field(default_factory=list)
    presumed_intent: str = ""
    original_input: str = ""
    #: Deprecated: the ``positions`` of :attr:`claims`, as one
    #: ``ClaimLocation`` per claim.
    locations: list[ClaimLocation] | None = Field(default=None, json_schema_extra={"deprecated": True})

    @model_validator(mode="before")
    @classmethod
    @_reads_both("claims")
    def _read_both_shapes(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        if out.get("status") == NO_CHECKABLE_CLAIM:
            out["status"] = "not_a_claim"
        claims = out.get("claims")
        if isinstance(claims, list):
            # The newer shape: derive the three original fields.
            items = [c for c in claims if isinstance(c, dict)]
            names = [str(c.get("claim") or "") for c in items]
            out.setdefault("claim", names[0] if names else "")
            out.setdefault("identified_claims", names if len(names) > 1 else [])
            if "locations" not in out:
                located = bool(items) and all(c.get("positions") is not None for c in items)
                out["locations"] = (
                    [{"claim": c.get("claim") or "", "positions": c.get("positions")} for c in items]
                    if located
                    else None
                )
            return out
        # The original shape: build ``claims`` from ``claim`` + ``identified_claims``.
        identified = [c for c in out.get("identified_claims") or [] if isinstance(c, str)]
        primary = out.get("claim") if isinstance(out.get("claim"), str) else ""
        # ``identified_claims`` normally starts with ``claim``; an older server
        # sent the extras only, so take the union, ``claim`` first.
        names = ([primary] if primary else []) + [c for c in identified if c != primary]
        positions: dict[str, Any] = {}
        for loc in out.get("locations") or []:
            if isinstance(loc, dict) and isinstance(loc.get("claim"), str):
                positions.setdefault(loc["claim"], loc.get("positions"))
        out["claims"] = [{"claim": n, "positions": positions.get(n)} for n in names]
        return out


class FailureBlock(_Lax):
    """Why something failed: a review, one claim's work inside it, a citation
    check, an ``assess`` row or a verification.

    ``code`` is the specific cause (an open set, e.g. ``no_checkable_claim``,
    ``insufficient_credits``, ``timeout``), ``detail`` one sentence on what
    happened (``None`` from the original response shape, which has no such
    sentence here), ``failure_class`` the closed :data:`FailureClass`,
    ``retryable`` whether resending the same request can help, ``hint`` one
    sentence on what to do next and ``docs_url`` where the class is explained.

    ``failure_reason`` is the older name of ``code`` (deprecated), with its
    original spelling: ``no_claim`` where ``code`` reads
    ``no_checkable_claim``. Either response shape fills both.
    """

    code: str | None = ""
    detail: str | None = None
    #: Deprecated: :attr:`code`, spelling "nothing checkable" ``no_claim``.
    failure_reason: str | None = Field(default="", json_schema_extra={"deprecated": True})
    failure_class: str | None = ""
    retryable: bool | None = None
    hint: str | None = None
    docs_url: str | None = ""

    @model_validator(mode="before")
    @classmethod
    @_reads_both("code", "detail")
    def _read_both_shapes(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if "code" in data and "failure_reason" not in data:
            return {**data, "failure_reason": _old_code(data["code"], "no_claim")}
        if "failure_reason" in data and "code" not in data:
            return {**data, "code": _new_code(data["failure_reason"])}
        return data


class AssessClaim(_Lax):
    """Per-claim entry in an ``AssessResponse.claims`` list.

    Lean shape by design — no model_votes, no panel identity. The
    ``verification_url`` (when present) points at the full
    ``ClaimDetailOut`` payload at ``GET /api/v1/verifications/{id}`` for
    callers that want citations and the full audit trail.

    ``status`` is ``"completed"`` for a row with a verdict and ``"failed"``
    for one that could not be given a verdict. A failed row stays in
    position (one row per item sent on a list call), is not charged, and
    says why in ``failure``: ``failure.code`` names the cause and
    ``failure.hint`` is one sentence on what to send next — the field to
    surface to a human, written per cause. A vague input is assessed on its
    most likely reading, which ``claim`` carries. A compound input is
    assessed on its main claim; the other claims found in it are listed in
    ``more_claims``.

    The original fields keep their meaning whichever response shape
    arrived: a failed row reads ``verdict == "Error"`` and
    ``confidence == "low"``, its cause is in ``error_code`` (``no_claim``
    where ``failure.code`` reads ``no_checkable_claim``) and its next step in
    ``hint``; ``identified_claims`` is ``more_claims``. These fields default
    empty so older servers that don't send them still parse.
    """

    claim: str = ""
    # Output language (ISO 639-1). Echoes the language requested on the
    # call, or ``'en'`` when unspecified. Verdict enums always English.
    language: str = "en"
    #: ``"completed"`` (a verdict) or ``"failed"`` (none; see ``failure``).
    #: ``""`` only from a server that sent neither a status nor a verdict.
    status: str = ""
    verdict: str = ""  # "True" | "Mostly True" | "Mixed" | "Mostly False" | "False" | "Error"
    confidence: str = "low"  # "high" | "medium" | "low"
    verification_url: str | None = None
    # ``rationale`` is the reasoning of a reviewer who agrees with the panel's
    # verdict; ``dissent``, when set, is the reasoning of the reviewer farthest
    # from it. Both are reviewers' notes, not checked sources; for sourced
    # evidence, call ``verify``. Read both as optional: a failed row has
    # neither, and a response replayed from before the API added them carries
    # neither key.
    rationale: str | None = None
    dissent: str | None = None
    # The claim with its wrong part corrected, when the request set
    # ``suggest_rewrite=True`` and the check found the claim "False" or
    # "Mostly False" with high confidence. ``None`` when not requested,
    # outside that, when there was no correction to write, and on a response
    # replayed from before the API added the key. Written from the quick
    # check's reasoning and not itself verified: review it, or run it through
    # ``verify``, before using it.
    suggested_rewrite: str | None = None
    #: Why a failed row has no verdict; ``None`` on a completed row.
    #: ``failure.code`` is an OPEN set (``no_checkable_claim`` |
    #: ``framing_failed`` | ``upstream_unavailable`` | ``timeout`` today):
    #: branch on the ones you know and fall through on the rest.
    #:
    #: Which are worth resending as-is: ``upstream_unavailable`` (a provider
    #: was down) and ``timeout`` (the call ran out of its time budget before
    #: this item was done — send fewer items per call to make it less likely).
    #: ``framing_failed`` is deterministic, so retrying the same text will not
    #: help. ``no_checkable_claim`` wants a different input; read
    #: ``failure.hint``.
    failure: FailureBlock | None = None
    #: Other claims found in the input that were NOT assessed; else empty.
    more_claims: list[str] = Field(default_factory=list)
    #: Deprecated: ``failure.code`` on a failed row, with "nothing
    #: checkable" spelled ``no_claim``; ``None`` on a completed row.
    error_code: str | None = Field(default=None, json_schema_extra={"deprecated": True})
    # Deprecated: always empty since 2026-09-12, when the ``ambiguous`` cause
    # that filled it was retired. Kept because the server still sends the key.
    candidate_claims: list[str] = Field(default_factory=list, json_schema_extra={"deprecated": True})
    #: Deprecated: :attr:`more_claims`.
    identified_claims: list[str] = Field(default_factory=list, json_schema_extra={"deprecated": True})
    #: Deprecated: one sentence on what to send next. On a failed row it is
    #: ``failure.hint``. The original response shape also set it on a row
    #: with other claims found; the newer shape does not.
    hint: str | None = Field(default=None, json_schema_extra={"deprecated": True})

    @model_validator(mode="before")
    @classmethod
    @_reads_both("status", "failure", "more_claims")
    def _read_both_shapes(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = _mirror(dict(data), "more_claims", "identified_claims")
        status = out.get("status")
        verdict = out.get("verdict")
        if not status:
            status = "failed" if verdict == "Error" else ("completed" if verdict else "")
            out["status"] = status
        if status == "failed":
            # The original meaning of a row with no verdict.
            if not verdict:
                out["verdict"] = "Error"
            if out.get("confidence") is None:
                out["confidence"] = "low"
        else:
            if verdict is None:
                out["verdict"] = ""
            if out.get("confidence") is None:
                out["confidence"] = "low"
        failure = out.get("failure")
        if isinstance(failure, dict):
            code = failure.get("code", failure.get("failure_reason"))
            out.setdefault("error_code", _old_code(code, "no_claim"))
            out.setdefault("hint", failure.get("hint"))
        elif failure is None and status == "failed" and "failure" not in data:
            # The original shape: build the block from the row's own fields.
            out["failure"] = {"code": _new_code(out.get("error_code") or ""), "hint": out.get("hint")}
        return out


#: The ``AssessResponse.status`` values this release knows about. A
#: documentation constant, like ``ExtractStatus``: the field stays ``str``.
#:
#: - ``ok`` — at least one row has a verdict (a list may mix in failed rows).
#: - ``no_checkable_claim`` — the input, or every item, holds nothing that
#:   can be checked.
#: - ``error`` — no row has a verdict, for another reason (see the rows).
AssessStatus = Literal["ok", "no_checkable_claim", "error"]


class AssessResponse(_Lax):
    """Output of ``POST /assess``.

    Single form (``assess(claim=...)``): ``claims`` is one entry per claim
    found in the input — up to 20, at 1 credit each. A text that makes more
    claims than one call checks gets its most check-worthy 20 checked and the
    rest listed in ``more_claims``, unchecked and free: send them back with
    ``assess(claims=...)``, 20 a call, to check them. When the input holds no
    checkable claim, ``claims`` is empty, ``status`` is
    ``"no_checkable_claim"`` and ``failure`` says why.

    List form (``assess(claims=[...])``): exactly one entry per item sent,
    in the order sent. An item that could not be given a verdict is a
    ``"failed"`` row in position (see ``AssessClaim``), never a missing one;
    the top-level ``failure`` stays ``None``.

    ``status`` (see ``AssessStatus``) sums the call up: ``ok``,
    ``no_checkable_claim`` or ``error``. Computed from the rows when a server
    does not send it.

    Deprecated, kept with their original meaning: ``error`` is the sentence
    (``failure.detail``) and ``error_code`` is ``'no_claim'`` when the
    single form found no checkable claim; both are empty otherwise.
    """

    status: str = ""
    claims: list[AssessClaim] = Field(default_factory=list)
    #: Why the single form has no rows; ``None`` otherwise.
    failure: FailureBlock | None = None
    #: Deprecated: ``failure.detail``.
    error: str | None = Field(default=None, json_schema_extra={"deprecated": True})
    #: Deprecated: ``'no_claim'`` when ``failure`` says nothing was checkable.
    error_code: str = Field(default="", json_schema_extra={"deprecated": True})  # '' | 'no_claim'
    # Deprecated: always empty since 2026-09-12. Kept because the server
    # still sends the key.
    candidate_claims: list[str] = Field(default_factory=list, json_schema_extra={"deprecated": True})
    # Single form: the claims found past the ones checked, most check-worthy
    # first; ``[]`` otherwise, on the list form, and from older servers.
    more_claims: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    @_reads_both("status", "failure")
    def _read_both_shapes(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        failure = out.get("failure")
        if isinstance(failure, dict):
            code = failure.get("code", failure.get("failure_reason"))
            out.setdefault("error", failure.get("detail"))
            out.setdefault("error_code", _old_code(code, "no_claim") or "")
        elif failure is None and "failure" not in data and (out.get("error") or out.get("error_code")):
            out["failure"] = {"code": _new_code(out.get("error_code") or ""), "detail": out.get("error")}
        if out.get("error_code") is None:
            out["error_code"] = ""
        if not out.get("status"):
            out["status"] = _assess_status(out)
        return out


def _assess_status(body: dict[str, Any]) -> str:
    """``AssessResponse.status`` for a body that does not carry it."""
    rows = [r for r in body.get("claims") or [] if isinstance(r, dict)]
    failed = [r for r in rows if r.get("status") == "failed" or r.get("verdict") in ("Error", None, "")]
    if rows and len(failed) < len(rows):
        return "ok"

    def _code(block: Any, fallback: Any) -> Any:
        if isinstance(block, dict):
            return block.get("code", block.get("failure_reason"))
        return fallback

    if not rows:
        code = _code(body.get("failure"), body.get("error_code"))
        return NO_CHECKABLE_CLAIM if _new_code(code) == NO_CHECKABLE_CLAIM else "error"
    codes = {_new_code(_code(r.get("failure"), r.get("error_code"))) for r in rows}
    return NO_CHECKABLE_CLAIM if codes == {NO_CHECKABLE_CLAIM} else "error"


class TaskAccepted(_Lax):
    """Returned by ``POST /verify`` and per item of ``POST /verify/batch``
    and ``POST /verify/{task_id}/select``.

    ``claim`` is the item's claim on a batch or select receipt (``""`` on a
    single ``verify`` receipt). ``claim_text`` is its older name, the same
    string (deprecated; either response shape fills both).
    """

    task_id: str = ""
    claim: str = ""
    #: Deprecated: the same string as :attr:`claim`.
    claim_text: str = Field(default="", json_schema_extra={"deprecated": True})

    @model_validator(mode="before")
    @classmethod
    @_reads_both("claim")
    def _read_both_shapes(cls, data: Any) -> Any:
        return _mirror(data, "claim", "claim_text") if isinstance(data, dict) else data


class BatchAccepted(_Lax):
    batch_id: str = ""
    items: list[TaskAccepted] = Field(default_factory=list)


class Progress(_Lax):
    """Where a running verification has got to. Advisory — never results.

    ``step`` is one of ``starting`` / ``framing`` / ``research`` / ``debate``
    / ``adjudication`` / ``conclusion``. ``index`` is the 1-based stage
    position (0 while ``starting``) out of ``total``; read ``total`` off the
    response rather than hard-coding it. ``poll_after_seconds`` is how long
    to wait before looking again — :meth:`Lenz.verify_and_wait` honours it
    for you. ``elapsed_seconds`` is how long the run has been going, a
    measurement rather than an estimate of what is left.

    ``index`` is stage POSITION, not elapsed work: the stages are uneven, so
    a bar driven by it sits on ``research`` for roughly half the run.

    ``step`` stays a plain ``str``, not a ``Literal`` — an SDK that
    hard-rejects a stage name the server adds later is worse than one that
    passes it through.

    This was a plain ``dict`` up to 2.10.0. The mapping methods below keep
    ``p["step"]``, ``"step" in p``, ``p.keys()`` and ``dict(p)`` working on a
    2.x minor; they are deprecated and go in 3.0.0. Use attributes.
    """

    step: str = ""
    index: int | None = None
    total: int | None = None
    elapsed_seconds: int | None = None
    poll_after_seconds: int | None = None

    # ── dict compatibility shim (deprecated, removed in 3.0.0) ──
    #
    # `.get()` alone would have been the worst option available: the
    # changelog would say "minor", the shim would look like it handled
    # compatibility, and a caller doing `progress["step"]` would find out in
    # production. `dict` has four access patterns; support all of them.

    # Everything routes through model_dump() rather than getattr, so the key
    # set is the FIELDS (plus any extras the server sent) — `"model_dump" in
    # progress` must not answer True.

    def __getitem__(self, key: str) -> Any:
        return self.model_dump()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.model_dump().get(key, default)

    def __contains__(self, key: object) -> bool:
        return key in self.model_dump()

    def keys(self) -> KeysView[str]:
        return self.model_dump().keys()

    def values(self) -> ValuesView[Any]:
        return self.model_dump().values()

    def items(self) -> ItemsView[str, Any]:
        return self.model_dump().items()


class TaskStatus(_Lax):
    """Returned by ``GET /verify/status/{task_id}``."""

    status: str = ""  # processing | needs_input | completed | failed
    # Echoed on every shape since 2026-09, so a caller polling several
    # verifications in one loop can tell the replies apart. ``""`` from
    # older servers.
    task_id: str = ""
    # Populated when status == 'needs_input': 'multi_claim'.
    reason: str = ""
    # One sentence on what was unclear and how to resolve it. Set on a
    # ``multi_claim`` ``needs_input`` and on a ``failed`` with
    # ``failure_reason == "not_a_claim"``; ``""`` otherwise and from older
    # servers.
    hint: str = ""
    # Present on ``processing``; an empty ``Progress`` on every other status
    # (the server omits the key entirely there since 2026-09).
    progress: Progress = Field(default_factory=lambda: Progress())
    result: Verification | None = None
    # needs_input branches
    claims: list[CandidateClaim] = Field(default_factory=list)
    # Deprecated, both always empty: the API no longer sends them, and
    # ``reason`` is only ever ``multi_claim``. Kept so code that reads them
    # keeps working; removal is planned for 2026-11-29. Marked deprecated in
    # the JSON schema only, so reading them does not warn.
    candidates: list[str] = Field(default_factory=list, json_schema_extra={"deprecated": True})
    similar_claims: list[SimilarVerification] = Field(default_factory=list, json_schema_extra={"deprecated": True})
    #: On ``failed``: why. ``failure.code`` is the specific cause (an open
    #: set, e.g. ``research_empty``, ``no_checkable_claim``), ``failure.detail``
    #: one sentence on what happened, ``failure.failure_class`` the closed
    #: :data:`FailureClass`, ``failure.retryable`` whether resending the same
    #: claim can help, ``failure.hint`` what to send instead (when the server
    #: has one) and ``failure.docs_url`` where the class is explained. Filled
    #: from either response shape; ``None`` on every other status.
    failure: FailureBlock | None = None
    # The original failure fields, deprecated and kept with their original
    # meaning whichever response shape arrived: ``error`` is the sentence,
    # ``failure_reason`` the cause (``not_a_claim`` where ``failure.code``
    # reads ``no_checkable_claim``), and ``failure_class`` / ``retryable`` /
    # ``docs_url`` / ``hint`` the same values as on ``failure``.
    # ``failure_detail`` is never sent by the API; it stays for old code.
    error: str = Field(default="", json_schema_extra={"deprecated": True})
    failure_reason: str = Field(default="", json_schema_extra={"deprecated": True})
    failure_detail: str = Field(default="", json_schema_extra={"deprecated": True})
    # WHY it failed — the closed set is ``FailureClass`` (import it for
    # exhaustive matching). The annotation stays ``str`` on purpose: a
    # ``Literal`` here would make an unknown class the server adds later a
    # hard ValidationError, and every other field on this model is lax.
    # Rows predating 2026-08 omit this and ``retryable`` (the derived retry
    # signal — true iff ``upstream_unavailable``).
    failure_class: str = ""
    retryable: bool | None = None
    # Where this ``failure_class`` is explained. ``""`` from older servers.
    docs_url: str = ""

    @model_validator(mode="before")
    @classmethod
    @_reads_both("failure")
    def _read_both_shapes(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        failure = out.get("failure")
        if isinstance(failure, dict):
            # The newer shape: the original top-level fields from the block.
            code = failure.get("code", failure.get("failure_reason"))
            for key, value in (
                ("error", failure.get("detail")),
                ("failure_reason", _old_code(code, "not_a_claim")),
                ("failure_class", failure.get("failure_class")),
                ("retryable", failure.get("retryable")),
                ("docs_url", failure.get("docs_url")),
                ("hint", failure.get("hint")),
            ):
                if out.get(key) is None:
                    out[key] = value
        elif out.get("status") == "failed" and failure is None:
            # The original shape: the block from the top-level fields.
            out["failure"] = {
                "code": _new_code(out.get("failure_reason") or ""),
                "detail": out.get("error") or out.get("failure_detail") or None,
                "failure_class": out.get("failure_class") or "",
                "retryable": out.get("retryable") if isinstance(out.get("retryable"), bool) else None,
                "hint": out.get("hint") or None,
                "docs_url": out.get("docs_url") or "",
            }
        # ``None`` reads as the field's empty value, never as a validation error.
        for key in ("error", "failure_reason", "failure_detail", "failure_class", "docs_url", "hint", "reason"):
            if key in out and out[key] is None:
                out[key] = ""
        return out


class BatchItemResult(_Lax):
    """Per-item outcome from :meth:`Lenz.verify_batch_and_wait`.

    A client-side composition type — NOT a wire shape (the server never emits
    it, so it has no contract fixture). One entry per task that
    ``POST /verify/batch`` returned, in input order.

    ``status`` is a client-side rollup:

    - ``completed``    — ``verification`` is set (and ``status_detail`` carries the raw poll).
    - ``needs_input``  — paused for caller input; inspect ``status_detail`` (reason / claims).
    - ``failed``       — terminal failure (or completed-without-result); ``status_detail`` carries the diagnostic.
      A verification removed by its account's retention period (HTTP 410) is ``failed``, ``status_detail`` ``None``.
    - ``timeout``      — the deadline elapsed before this task reached a terminal state; ``status_detail`` is ``None``.
    """

    task_id: str = ""
    claim: str = ""
    #: Deprecated: the same string as :attr:`claim`.
    claim_text: str = Field(default="", json_schema_extra={"deprecated": True})
    status: Literal["completed", "needs_input", "failed", "timeout"]
    verification: Verification | None = None
    status_detail: TaskStatus | None = None

    @model_validator(mode="before")
    @classmethod
    @_reads_both("claim")
    def _read_both_names(cls, data: Any) -> Any:
        return _mirror(data, "claim", "claim_text") if isinstance(data, dict) else data


class UsageCredits(_Lax):
    """The account's credit balance — the one pool every capability spends.

    Every billable call debits this pool at the weight in :attr:`Usage.costs`
    (``/verify`` is 10 credits — 5 at ``depth="low"``, published as
    ``cost_options["verify"]["depth"]["low"]`` — ``/assess`` and ``/ask`` are 1, ``/extract`` is
    free). Two buckets: the monthly allowance for the current plan, which
    resets at ``resets_at``, and non-expiring ``extra`` credits from grants and
    top-ups, spent only once the allowance is gone. ``remaining`` covers both
    and is what a call is checked against. ``bonus`` is the deprecated old
    name of ``extra``, the same number, kept for existing code.

    Servers predating the credit pool (before 2026-08-29) don't send this block
    at all, and it then reads as all-zero — check ``usage.credits.total``
    before trusting the balance.
    """

    total: int = 0
    used: int = 0
    remaining: int = 0
    #: The non-expiring part of ``remaining``: credits from grants and top-ups,
    #: spent only once the monthly allowance is gone.
    extra: int = 0
    #: **Deprecated** old name of :attr:`extra`, the same number, kept for
    #: existing code. Reading it emits a ``DeprecationWarning``; it stays in
    #: ``model_dump()`` output (unwarned), filled from ``extra`` when the
    #: server does not send it.
    bonus: int = Field(
        default=0,
        deprecated=(
            "UsageCredits.bonus is deprecated; use `extra` — the same number, the non-expiring part of the balance."
        ),
    )
    resets_at: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _mirror_extra_and_bonus(cls, data: Any) -> Any:
        """Keep ``extra`` and its deprecated old name in step, in both directions.

        A server that has not started sending ``extra`` sends only ``bonus``;
        the newer response shape sends only ``extra``. Mirroring here means
        both attributes read correctly either way.
        """
        if not isinstance(data, dict):
            return data
        has_extra, has_bonus = data.get("extra") is not None, data.get("bonus") is not None
        if has_bonus and not has_extra:
            return {**data, "extra": data["bonus"]}
        if has_extra and not has_bonus:
            return {**data, "bonus": data["extra"]}
        return data


class UsageCapacity(_Lax):
    """DEPRECATED. One capability's share of the pool, in that capability's unit.

    Deprecated, together with the per-block ``credits`` alias, and kept for
    existing code: the newer response shape does not send these blocks, and
    the SDK then computes them from :attr:`Usage.credits` and
    :attr:`Usage.costs`. Read those two and do the division yourself — it is
    the same one this block does::

        remaining = u.credits.remaining // u.costs[capability]
        total     = u.credits.total     // u.costs[capability]
        used      = total - remaining

    Two capabilities at the same price emit identical objects — ``ask`` and
    ``assess`` are both 1 credit — because there is one balance behind all of
    them, not a per-capability allowance.

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
    - ``bonus``    — :attr:`UsageCredits.extra`, the non-expiring part of the
      balance, in this unit. A user holding 5 extra credits sees
      ``assess.bonus == 5`` and ``verify.bonus == 0``: 5 credits does not
      buy a verification.

    ``remaining`` is the usable capacity across both buckets.
    """

    quota_used: int = 0
    quota_total: int = 0
    quota_remaining: int = 0
    bonus: int = 0
    #: **Deprecated** alias of :attr:`bonus`, kept for existing code. It never
    #: meant the credit pool — before the pool existed it meant this
    #: capability's one-off top-up balance, which is exactly what ``bonus``
    #: reports. Reading it emits a ``DeprecationWarning``; it stays in
    #: ``model_dump()`` output (unwarned) for as long as the server sends it.
    credits: int = Field(
        default=0,
        deprecated=(
            "UsageCapacity.credits is deprecated; "
            "use `bonus` — the same number, this capability's non-expiring top-up "
            "balance. The credit pool itself is `Usage.credits`."
        ),
    )
    remaining: int = 0

    @model_validator(mode="before")
    @classmethod
    def _mirror_bonus_and_credits(cls, data: Any) -> Any:
        """Keep ``bonus`` and its deprecated alias in step, in both directions.

        A server predating the credit pool sends only ``credits``; a block the
        SDK computed has only ``bonus``. Mirroring here means
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

    #: The tier slug — ``"free"`` | ``"plus"`` | ``"pro"`` | ``"scale"``.
    #: This is the field to branch on; it is stable. The Pro plan's slug was
    #: ``"developer"`` until 2026-09-15.
    plan: str = ""
    #: The same tier as display copy (``"Pro"``). Separate from
    #: :attr:`plan` on purpose: this one is copy and may be reworded, so
    #: comparing against it will break on a rename that ought to be free.
    #: Empty on servers predating this field — fall back to :attr:`plan`.
    plan_label: str = ""
    #: Deprecated: the same time as ``credits.resets_at``, which it is
    #: filled from when the server does not send it.
    quota_resets_at: str | None = Field(default=None, json_schema_extra={"deprecated": True})
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
    #: Nested rather than flat so that a future request parameter adds a key
    #: under its capability instead of a new top-level entry — ``costs`` stays
    #: a list of capability names, safe to iterate.
    #:
    #: You are charged for the depth you **requested**, not the one served; a
    #: verdict served from the last hour's cache is free (except a ``verify``
    #: that issues a new warranty certificate). The ``depth`` echoed on a
    #: completed verification is what the verdict was PRODUCED with, so it can
    #: read ``standard`` on a ``low`` request — the echo describes the
    #: evidence, the charge follows the request.
    cost_options: dict[str, dict[str, dict[str, int]]] = Field(default_factory=dict)
    #: DEPRECATED, kept for existing code. Derive from :attr:`credits` and
    #: :attr:`costs` instead::
    #:
    #:     left = u.credits.remaining // u.costs["verify"]
    #:
    #: Computed that way when the server does not send the block (the newer
    #: response shape does not).
    verify: UsageCapacity = Field(default_factory=UsageCapacity)
    #: DEPRECATED, kept for existing code. See :attr:`verify`.
    ask: UsageCapacity = Field(default_factory=UsageCapacity)
    #: DEPRECATED, kept for existing code. See :attr:`verify`.
    assess: UsageCapacity = Field(default_factory=UsageCapacity)
    extract: UsageExtract = Field(default_factory=UsageExtract)
    # Whether this key has a webhook signing secret provisioned. ``POST /verify``
    # with a ``webhook_url`` is rejected without one, so callers that rely on
    # webhook delivery can check this up front. Reports existence only — the
    # secret value is never exposed here (shown once at rotation, never again).
    # Defaults to ``False`` on servers predating this field.
    has_webhook_secret: bool = False

    @model_validator(mode="before")
    @classmethod
    @_reads_both(always_hide=True)
    def _read_both_shapes(cls, data: Any) -> Any:
        """Compute the deprecated blocks the newer response shape leaves out.

        Each per-capability block is the credit pool divided by that
        capability's price, exactly as the server computed it; it is filled
        only when the server sent the pool and the price but not the block.
        """
        if not isinstance(data, dict):
            return data
        credits, costs = data.get("credits"), data.get("costs")
        if not isinstance(credits, dict):
            return data
        out = dict(data)
        if "quota_resets_at" not in out:
            out["quota_resets_at"] = credits.get("resets_at")
        if not isinstance(costs, dict):
            return out

        def _int(value: Any) -> int:
            return value if isinstance(value, int) and not isinstance(value, bool) else 0

        total, remaining, extra = (
            _int(credits.get("total")),
            _int(credits.get("remaining")),
            _int(credits.get("extra", credits.get("bonus"))),
        )
        for capability in ("verify", "ask", "assess"):
            cost = _int(costs.get(capability))
            if capability in out or cost <= 0:
                continue
            quota_total, left = total // cost, remaining // cost
            out[capability] = {
                "quota_used": quota_total - left,
                "quota_total": quota_total,
                "quota_remaining": left,
                "bonus": extra // cost,
                "remaining": left,
            }
        return out


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


# ── /review ──────────────────────────────────────────────────────────────
#
# ``POST /review`` reads a draft, gives every claim a quick verdict, sends the
# ones that look wrong or uncertain through the deep check, and hands back the
# issues. ``GET /reviews/{review_id}`` answers one of two views over one
# envelope: ``ReviewFull`` (every claim, ``claims``) and ``ReviewIssues``
# (``?view=issues``, without ``claims``).
#
# Every enum-shaped field below is a plain ``str``, never a ``Literal``: the API
# may add a status, a disposition or an error code in a minor version, and a
# released SDK must pass it through rather than reject the whole review.


class ReviewStarted(_Lax):
    """Returned by ``POST /review`` (HTTP 202).

    ``status`` is always ``"queued"``: the acceptance receipt, not the current
    state. Read the review with ``client.get_review(review_id)``.
    """

    review_id: str = ""
    status: str = "queued"


class EscalationPolicy(_Lax):
    """Which quick-checked claims get a deep check, as the review RESOLVED it.

    A claim is deep-checked when its quick verdict is in ``verdicts`` OR its
    confidence band is in ``confidence``, up to ``max_verifications`` of them,
    at ``depth``. ``max_assessments`` is how many of the draft's claims, most
    check-worthy first, got a quick verdict.
    """

    verdicts: list[str] = Field(default_factory=list)
    confidence: list[str] = Field(default_factory=list)
    max_assessments: int = 20
    max_verifications: int = 5
    depth: str = "standard"
    #: How many of the draft's citations the review checks; ``0`` (or
    #: ``None`` from an older server) when it checks none.
    max_citations: int | None = None
    #: Whether the review computes suggested edits (``False`` from an older
    #: server).
    suggest_edits: bool = False


class Escalation(_Lax):
    """Why a claim was, or was not, deep-checked.

    ``matched_rules`` lists the rules the quick check matched (``verdict``,
    ``confidence``, both, or neither). ``disposition`` says what happened:
    ``planned`` (deep-checked), ``not_selected`` (no rule matched), ``cap``
    (matched, but ``max_verifications`` was reached), ``credits`` (matched,
    but the balance ran out) or ``account_cap`` (matched, but the account's
    allowance of concurrent deep checks was reached). To deep-check the
    ``cap`` rows yourself, send their claims to ``verify_batch_and_wait``.
    """

    matched_rules: list[str] = Field(default_factory=list)
    disposition: str = ""


class ReviewAssessmentCounts(_Lax):
    completed: int = 0
    failed: int = 0


class ReviewVerificationCounts(_Lax):
    planned: int = 0
    completed: int = 0
    failed: int = 0


class ReviewCitationCheckCounts(_Lax):
    """Three disjoint counts over the citation rows. ``checked``: a finding
    other than ``unchecked``. ``unchecked``: could not be checked, for a
    reason of the page or the draft. ``failed``: no finding, for a reason of
    ours. A row still running is in none of the three."""

    checked: int = 0
    unchecked: int = 0
    failed: int = 0


class ReviewSummary(_Lax):
    """Counts over the review. A count is ``None`` until what it counts is
    known (``claims_selected`` before the draft has been read).

    The ``citation*`` counts are ``None`` (``citation_issues`` 0) on a review
    that did not ask for the citation check."""

    #: How many claims the draft holds. ``None`` until the draft is read,
    #: and from the original response shape, which does not send it.
    claims_found: int | None = None
    claims_selected: int | None = None
    #: The resolved ``max_assessments``.
    claim_limit: int = 20
    #: Some of the draft's claims were left out (more than ``claim_limit``):
    #: they are in ``more_claims``. From the original response shape it is
    #: read off ``more_claims`` once the draft is read.
    claim_limit_exceeded: bool | None = None
    #: Deprecated: the draft held at least ``claim_limit`` claims, so more
    #: MAY exist. Read :attr:`claim_limit_exceeded`, which says whether they do.
    claim_limit_reached: bool | None = Field(default=None, json_schema_extra={"deprecated": True})
    #: The text was cut at 50,000 characters.
    input_truncated: bool = False
    assessments: ReviewAssessmentCounts | None = None
    verifications: ReviewVerificationCounts | None = None
    issues: int = 0
    #: Citations with a URL or a DOI in the draft (exact; each use counts).
    citations_found: int | None = None
    #: How many of them the review checks: the first, in the draft's order.
    citations_selected: int | None = None
    #: The resolved ``max_citations``.
    citation_limit: int | None = None
    #: ``citations_found`` is over ``citation_limit``: some were left out.
    citation_limit_exceeded: bool | None = None
    #: Deprecated: the same value as :attr:`citation_limit_exceeded`.
    citation_limit_reached: bool | None = Field(default=None, json_schema_extra={"deprecated": True})
    citation_checks: ReviewCitationCheckCounts | None = None
    #: ``len(citation_issues)``.
    citation_issues: int = 0
    #: Why the citation check was asked for and did not run: ``url_input``
    #: (the draft was one URL), ``insufficient_credits`` or ``switched_off``.
    #: An open set.
    citations_skipped: str | None = None

    @model_validator(mode="before")
    @classmethod
    @_reads_both("claims_found", "claim_limit_exceeded", "citation_limit_exceeded")
    def _read_both_shapes(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = _mirror(dict(data), "citation_limit_exceeded", "citation_limit_reached")
        if "claim_limit_reached" not in out and "claims_found" in out:
            found, limit = out.get("claims_found"), out.get("claim_limit", 20)
            out["claim_limit_reached"] = found >= limit if isinstance(found, int) and isinstance(limit, int) else None
        return out


class ReviewCredits(_Lax):
    """``charged`` is the net credits the review cost the account, its
    citation checks included. It is final once no deep check or citation
    check is running: read it at ``completed``."""

    charged: int = 0


class ReviewResult(_Lax):
    """The one answer to read for a claim: the deep check's verdict when it
    completed, else the quick check's. ``source`` says which
    (``assessment`` or ``verification``)."""

    verdict: str = ""
    confidence: str | None = None
    source: str = ""
    is_issue: bool = False


class ReviewAssessment(_Lax):
    """A claim's quick check. ``status``: ``pending`` | ``running`` |
    ``completed`` | ``failed``. On ``completed`` the fields mean what they
    mean on an ``/assess`` row; on ``failed`` see ``failure`` (its ``code``
    an open set, as on ``/assess``). ``more_claims`` lists other claims
    found in the passage that were not checked.

    Deprecated, kept with their original meaning: ``error_code`` is
    ``failure.code`` on a failed row (``no_claim`` where the code reads
    ``no_checkable_claim``), ``identified_claims`` is ``more_claims``, and
    ``hint`` is the row's own hint from the original response shape
    (``None`` from the newer one; read ``failure.hint``)."""

    status: str = ""
    verdict: str | None = None
    confidence: str | None = None
    rationale: str | None = None
    dissent: str | None = None
    verification_url: str | None = None
    more_claims: list[str] = Field(default_factory=list)
    #: Deprecated: ``failure.code`` on a failed row.
    error_code: str | None = Field(default=None, json_schema_extra={"deprecated": True})
    #: Deprecated: :attr:`more_claims`.
    identified_claims: list[str] = Field(default_factory=list, json_schema_extra={"deprecated": True})
    #: Deprecated. See the class docstring.
    hint: str | None = Field(default=None, json_schema_extra={"deprecated": True})
    #: With ``suggest_edits=True``: the claim with its wrong part corrected,
    #: from the quick check's reasoning, when it found the claim "False" or
    #: "Mostly False" with high confidence. ``None`` otherwise, and from
    #: servers that predate the field. Not itself verified: review it, or
    #: run it through ``verify``, before using it.
    suggested_rewrite: str | None = None
    failure: FailureBlock | None = None

    @model_validator(mode="before")
    @classmethod
    @_reads_both("more_claims")
    def _read_both_shapes(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = _mirror(dict(data), "more_claims", "identified_claims")
        failure = out.get("failure")
        if "error_code" not in out and isinstance(failure, dict):
            out["error_code"] = _old_code(failure.get("code", failure.get("failure_reason")), "no_claim")
        return out


class ReviewEntity(_Lax):
    """An entity the deep check found in the claim. ``qid`` is its Wikidata
    id when resolved. Either may be ``None``."""

    name: str | None = None
    qid: str | None = None


class ReviewVerification(_Lax):
    """A claim's deep check. ``status``: ``processing`` | ``completed`` |
    ``failed``; while ``processing`` only ``status``, ``content_status`` and
    the ids are set. ``content_status`` is ``purged`` once the verification
    was deleted or removed by its account's retention period: the verdict and
    ids stay, the text fields and links are ``None``.

    ``visibility`` and ``depth`` are what the verification actually has: a
    verdict served from an earlier check keeps its own visibility and may be
    ``standard`` for a ``low`` request. Sources are on the verification
    itself: ``client.verifications.get(verification_id)``, or ``url``.
    """

    status: str = ""
    content_status: str = "available"
    verification_id: str | None = None
    task_id: str | None = None
    claim: str | None = None
    language: str | None = None
    visibility: str | None = None
    depth: str | None = None
    domain: str | None = None
    entities: list[ReviewEntity] = Field(default_factory=list)
    verdict: str | None = None
    confidence: str | None = None
    lenz_score: int | float | None = None
    key_finding: str | None = None
    executive_summary: str | None = None
    suggested_rewrite: str | None = None
    warnings: list[str] = Field(default_factory=list)
    created_at: str | None = None
    #: See ``Verification.completed_at``.
    completed_at: str | None = None
    #: Deprecated. See ``Verification.modified_at``.
    modified_at: str | None = Field(default=None, json_schema_extra={"deprecated": True})
    verification_url: str | None = None
    url: str | None = None
    failure: FailureBlock | None = None

    @model_validator(mode="before")
    @classmethod
    @_reads_both("completed_at")
    def _read_both_shapes(cls, data: Any) -> Any:
        return _with_completion_times(data)


class SuggestedEdit(_Lax):
    """One replacement in the draft: the span ``start``..``end`` of the
    ``text`` you sent (Unicode code points, ``end`` exclusive, as in
    ``Position``), ``text`` exactly that slice, and ``replacement`` what takes
    its place (``""`` deletes it). ``position`` is the index of the claim
    row's ``positions`` entry the edit sits in, for grouping by passage.

    Compare ``text`` with your draft before you apply an edit, so a draft that
    changed since is never edited in the wrong place."""

    position: int = 0
    start: int = 0
    end: int = 0
    text: str = ""
    replacement: str = ""


class SuggestedEdits(_Lax):
    """The smallest edits to the draft that make it say what the claim's
    ``suggested_rewrite`` says (the deep check's, or the quick check's on a
    claim that stayed on the quick verdict), in the draft's own language
    (``review(..., suggest_edits=True)``).

    ``status`` is ``"pending"`` while they are computed (``edits`` is
    ``None``; keep polling) and ``"completed"`` once settled, which includes
    settling on none (``edits == []``: no edit could be made safely, or it
    could not be computed). They are not themselves verified:
    review them before you publish."""

    status: str = ""
    edits: list[SuggestedEdit] | None = None


class ReviewClaim(_Lax):
    """One claim of the draft, in the order the draft's claims were read.

    ``result`` is ``None`` until the quick check completes, and on a failed
    one. ``escalation`` is ``None`` until then too. ``verification`` is set
    once a deep check was planned for the claim.

    ``positions`` lists every place the draft makes the claim, in text
    order, at most 10 (see ``Position``). ``start`` / ``end`` index the
    ``text`` you sent in Unicode code points (``text[start:end]`` in Python;
    ``end`` exclusive), the same coordinates as a citation's ``position``.
    For a URL draft, ``start`` and ``end`` are ``None`` and ``text`` carries
    the passage. ``positions`` is ``None`` when the claims could not be
    located, or once a zero-retention draft is gone (and from servers that
    predate the field).
    """

    index: int = 0
    claim: str | None = None
    positions: list[Position] | None = None
    result: ReviewResult | None = None
    assessment: ReviewAssessment = Field(default_factory=ReviewAssessment)
    escalation: Escalation | None = None
    verification: ReviewVerification | None = None
    #: The claim's suggested edits to the draft (``review(...,
    #: suggest_edits=True)``). ``None`` when not asked, when the claim got no
    #: suggested rewrite from a completed deep check or its quick check, when its passage is not in a
    #: supported language or could not be placed, once a zero-retention draft
    #: is gone, and from servers that predate the field.
    suggested_edits: SuggestedEdits | None = None


class ReviewIssue(_Lax):
    """A claim whose final verdict is ``False``, ``Mostly False`` or ``Mixed``.

    ``source`` says which check the verdict comes from: ``verification`` (a
    deep check, with ``verification_id``, ``url`` and ``key_finding``) or
    ``assessment`` (the quick check only; ``escalation`` says why it was not
    deep-checked, and ``rationale`` is the reviewers' note).
    ``verified_claim`` is the deep check's wording of the claim when it
    differs from ``claim``. ``failure`` is set when the claim's deep check
    failed.
    """

    claim_index: int = 0
    claim: str | None = None
    verified_claim: str | None = None
    verdict: str = ""
    confidence: str | None = None
    source: str = ""
    verification_id: str | None = None
    verification_status: str | None = None
    verification_url: str | None = None
    url: str | None = None
    escalation: Escalation | None = None
    key_finding: str | None = None
    rationale: str | None = None
    #: The claim rewritten so the findings support it: the deep check's when
    #: the claim has one, otherwise, when the review asked for suggested
    #: edits, the quick check's (for a claim found "False" or "Mostly False"
    #: with high confidence); ``source`` says which check it came from.
    #: ``None`` when neither suggested one. It is a suggestion and has not
    #: been verified itself: review it, or run it through ``verify``, before
    #: you use it.
    suggested_rewrite: str | None = None
    failure: FailureBlock | None = None
    #: A copy of its claim row's ``suggested_edits``.
    suggested_edits: SuggestedEdits | None = None


class ReviewFailure(_Lax):
    """A claim outside the issues whose work failed. ``stage`` is
    ``assessment`` or ``verification``."""

    claim_index: int = 0
    claim: str | None = None
    stage: str = ""
    failure: FailureBlock | None = None


class ReviewCitationResult(_Lax):
    """The one answer to read for a citation, derived from its ``check``.

    ``finding``, most serious first: ``doi_not_found``, ``page_not_found``,
    ``contradicted``, ``quote_not_in_source``, ``not_in_source``,
    ``partly_supported``, ``metadata_mismatch`` (each an issue), then
    ``supported`` and ``unchecked``. ``source`` says which check the finding
    came from: ``doi``, ``page``, ``support``, ``quote`` or ``metadata``;
    ``None`` on ``unchecked``.
    """

    finding: str = ""
    source: str | None = None
    is_issue: bool = False


class ReviewCitationRecord(_Lax):
    """A DOI's record in the registry."""

    title: str | None = None
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    journal: str | None = None


class ReviewCitationDifference(_Lax):
    """One way the reference differs from the registry's record. ``field``
    is ``title``, ``authors``, ``year`` or ``journal``."""

    field: str = ""
    cited: str | None = None
    registered: str | None = None


class ReviewCitationCheck(_Lax):
    """A citation's check. ``status`` is progress, as on ``assessment``:
    ``pending`` | ``running`` | ``completed`` | ``failed``.

    - ``page_read``: ``full``, ``partial``, ``not_found`` or ``none``.
    - ``source_url``: where the text was actually read from (after
      redirects, or a free copy of a paper). ``source_version``, for a DOI:
      ``published``, ``accepted`` or ``submitted``.
    - ``support``: ``supported``, ``partly_supported``, ``contradicted``,
      ``not_in_source`` or ``unchecked``. ``snippet`` is the verified passage
      of the source it rests on; ``rationale`` is a reviewer's note, not a
      checked source.
    - ``quote``: ``matched``, ``not_in_source`` or ``unchecked``; ``None``
      when the draft quoted nothing from this source. ``missing_quote`` is
      the quoted excerpt the quote check did not find; ``None`` unless
      ``quote`` is ``not_in_source``.
    - ``doi_registered``, ``metadata`` (``consistent``, ``mismatch`` or
      ``unchecked``), ``metadata_differences`` and ``registered``: set for a
      DOI only.
    - ``unchecked_reason`` and ``hint``: why the finding is ``unchecked`` and
      what to do next. The reasons are an open set, e.g. ``no_text``,
      ``partial_text``, ``login_required``, ``unsupported_site``,
      ``no_statement``, ``invalid_url``, ``other_version``, ``inconclusive``,
      ``ambiguous_reference``.
    - ``failure``: on a ``failed`` check.
    """

    status: str = ""
    page_read: str | None = None
    page_title: str | None = None
    page_published_date: str | None = None
    page_language: str | None = None
    source_url: str | None = None
    source_version: str | None = None
    support: str | None = None
    snippet: str | None = None
    rationale: str | None = None
    quote: str | None = None
    missing_quote: str | None = None
    doi_registered: bool | None = None
    metadata: str | None = None
    metadata_differences: list[ReviewCitationDifference] = Field(default_factory=list)
    registered: ReviewCitationRecord | None = None
    unchecked_reason: str | None = None
    hint: str | None = None
    failure: FailureBlock | None = None


class ReviewCitation(_Lax):
    """One citation of the draft (a URL or a DOI where the draft uses it), in
    the draft's order. Only on a review that asked for the citation check.

    ``reference`` is the citation as the draft writes it; ``statement`` the
    draft's sentence it is attached to, with link syntax reduced to its
    words; ``quotes`` the words the draft quotes from this source.
    ``position`` locates ``statement`` in the text as sent (its ``text`` is
    ``None``: the row carries the ``statement``). ``result`` is
    ``None`` until the check has ended, and on a failed row with nothing
    established.
    """

    index: int = 0
    reference: str | None = None
    cited_url: str | None = None
    doi: str | None = None
    statement: str | None = None
    quotes: list[str] = Field(default_factory=list)
    position: Position | None = None
    result: ReviewCitationResult | None = None
    check: ReviewCitationCheck = Field(default_factory=ReviewCitationCheck)


class ReviewCitationIssue(_Lax):
    """A citation whose finding is an issue (see ``ReviewCitationResult``);
    most serious first, then in the draft's order.

    ``snippet`` and ``rationale`` are set only when ``source`` is
    ``support``. On ``quote_not_in_source``, ``missing_quote`` is the quoted
    excerpt that was not found. On ``metadata_mismatch``,
    ``metadata_differences`` says what differs. ``failure`` is set when another part of the check failed after
    the finding was established.
    """

    citation_index: int = 0
    reference: str | None = None
    cited_url: str | None = None
    doi: str | None = None
    statement: str | None = None
    quotes: list[str] = Field(default_factory=list)
    position: Position | None = None
    finding: str = ""
    source: str | None = None
    snippet: str | None = None
    rationale: str | None = None
    missing_quote: str | None = None
    metadata_differences: list[ReviewCitationDifference] = Field(default_factory=list)
    page_title: str | None = None
    failure: FailureBlock | None = None


class ReviewMoreCitation(_Lax):
    """A citation found in the draft past the ones this review checked:
    found but not checked. Send it in a later request to check it.
    ``sentence`` is the draft's sentence around it."""

    index: int = 0
    reference: str | None = None
    cited_url: str | None = None
    doi: str | None = None
    sentence: str | None = None
    position: Position | None = None


class ReviewCitationFailure(_Lax):
    """A citation whose check failed with nothing established."""

    citation_index: int = 0
    reference: str | None = None
    cited_url: str | None = None
    doi: str | None = None
    failure: FailureBlock | None = None


# ── /citecheck ──────────────────────────────────────────────────────────
#
# ``POST /citecheck`` checks the citations of a draft, or statement-source
# pairs sent as they are, without the rest of a review. The rows, issues and
# failures are the review's own models.


class CitecheckStarted(_Lax):
    """Returned by ``POST /citecheck`` (HTTP 202). ``status`` is always
    ``"queued"``: the receipt, not the current state."""

    citecheck_id: str = ""
    status: str = "queued"


class CitecheckPolicy(_Lax):
    """``max_citations``: how many citations are checked (for pairs, the
    number of pairs: every pair is checked)."""

    max_citations: int | None = None


class CitecheckSummary(_Lax):
    """Counts over the check, as on a review's ``summary``. ``citations_found``
    is the citations in the text, or the pairs sent; ``None`` until read."""

    citations_found: int | None = None
    citations_selected: int | None = None
    citation_limit: int | None = None
    #: ``citations_found`` is over ``citation_limit``: some were left out.
    citation_limit_exceeded: bool | None = None
    #: Deprecated: the same value as :attr:`citation_limit_exceeded`.
    citation_limit_reached: bool | None = Field(default=None, json_schema_extra={"deprecated": True})
    citation_checks: ReviewCitationCheckCounts | None = None
    citation_issues: int = 0

    @model_validator(mode="before")
    @classmethod
    @_reads_both("citation_limit_exceeded")
    def _read_both_shapes(cls, data: Any) -> Any:
        return _mirror(data, "citation_limit_exceeded", "citation_limit_reached") if isinstance(data, dict) else data


class Citecheck(_Lax):
    """``GET /citechecks/{citecheck_id}``: a citation check as it stands.

    ``status``: ``queued`` → ``checking`` → ``completed``, or ``failed``.
    ``outcome`` is ``None`` until it ends, then ``clean``, ``issues_found``,
    ``incomplete`` (a citation could not be checked for a reason of ours) or
    ``unchecked``. ``citations``, ``citation_issues`` and ``citation_failures``
    are the review's rows. ``more_citations`` lists the citations of the text
    past the ones checked; ``None`` until the text is read, ``[]`` for pairs.
    """

    citecheck_id: str = ""
    status: str = ""
    outcome: str | None = None
    created_at: str = ""
    completed_at: str | None = None
    poll_after_seconds: int | None = None
    policy: CitecheckPolicy = Field(default_factory=CitecheckPolicy)
    summary: CitecheckSummary = Field(default_factory=CitecheckSummary)
    credits: ReviewCredits = Field(default_factory=ReviewCredits)
    citations: list[ReviewCitation] = Field(default_factory=list)
    citation_issues: list[ReviewCitationIssue] = Field(default_factory=list)
    citation_failures: list[ReviewCitationFailure] = Field(default_factory=list)
    more_citations: list[ReviewMoreCitation] | None = None
    failure: FailureBlock | None = None


class ReviewEnvelope(_Lax):
    """What both views of ``GET /reviews/{review_id}`` carry.

    ``status`` is the lifecycle: ``queued`` → ``assessing`` → ``verifying``
    → ``completed``, or ``failed``. ``outcome`` is ``None`` until the review
    ends, then one of ``clean`` (every selected claim checked, no issue),
    ``issues_found``, ``incomplete`` (some work failed) or ``unchecked`` (it
    failed before any claim was checked; ``failure`` says why).

    On a review that asked for the citation check, a citation issue makes
    ``outcome`` ``issues_found`` too (``issues`` may then be empty: the rows
    are in ``citation_issues``), and a failed citation check makes it
    ``incomplete``. ``status`` reads ``verifying`` while deep checks or
    citation checks are running.

    ``issues`` and ``failures`` can change until ``completed``.
    ``poll_after_seconds`` is how long to wait before reading again; ``None``
    once the review has ended.
    """

    review_id: str = ""
    view: str = ""
    status: str = ""
    outcome: str | None = None
    created_at: str = ""
    completed_at: str | None = None
    language: str = "en"
    policy: EscalationPolicy = Field(default_factory=EscalationPolicy)
    summary: ReviewSummary = Field(default_factory=ReviewSummary)
    credits: ReviewCredits = Field(default_factory=ReviewCredits)
    poll_after_seconds: int | None = None
    issues: list[ReviewIssue] = Field(default_factory=list)
    failures: list[ReviewFailure] = Field(default_factory=list)
    citation_issues: list[ReviewCitationIssue] = Field(default_factory=list)
    citation_failures: list[ReviewCitationFailure] = Field(default_factory=list)
    #: Claims found past ``max_assessments``, in the draft's order: found but
    #: not checked. ``None`` until the draft is read, ``[]`` when there are none.
    more_claims: list[str] | None = None
    #: Where the draft makes each ``more_claims`` claim: one ``ClaimLocation``
    #: (``claim``, ``positions``) per string, same order, the shape
    #: ``extract(locate=True)`` returns. ``None`` until the draft is read.
    more_claim_locations: list[ClaimLocation] | None = None
    #: Citations found past the ones checked (up to 100): found but not
    #: checked. ``None`` until the draft is read, ``[]`` when there are none.
    more_citations: list[ReviewMoreCitation] | None = None
    failure: FailureBlock | None = None

    @model_validator(mode="after")
    def _claim_limit_exceeded_from_more_claims(self) -> ReviewEnvelope:
        """The original shape says only that the limit was REACHED; whether
        claims were left out is whether ``more_claims`` lists any."""
        summary = self.summary
        if summary.claim_limit_exceeded is None and self.more_claims is not None:
            summary.claim_limit_exceeded = bool(self.more_claims)
        return self


class ReviewFull(ReviewEnvelope):
    """``GET /reviews/{review_id}``: the envelope plus every claim."""

    view: str = "full"
    claims: list[ReviewClaim] = Field(default_factory=list)
    citations: list[ReviewCitation] = Field(default_factory=list)


class ReviewIssues(ReviewEnvelope):
    """``GET /reviews/{review_id}?view=issues``: the envelope, no ``claims``."""

    view: str = "issues"


__all__ = [
    "NO_CHECKABLE_CLAIM",
    "AskHistory",
    "AskMessage",
    "AskReply",
    "AssessClaim",
    "AssessResponse",
    "AssessStatus",
    "Assessment",
    "Audit",
    "BatchAccepted",
    "BatchItemResult",
    "CandidateClaim",
    "Citecheck",
    "CitecheckPolicy",
    "CitecheckStarted",
    "CitecheckSummary",
    "ClaimLocation",
    "DebateSide",
    "EntityRef",
    "Escalation",
    "EscalationPolicy",
    "ExtractStatus",
    "ExtractedClaim",
    "ExtractedClaims",
    "ExtractedEntity",
    "FailureBlock",
    "FailureClass",
    "LibraryItem",
    "LibraryList",
    "Position",
    "RelatedVerifications",
    "ReviewAssessment",
    "ReviewAssessmentCounts",
    "ReviewCitation",
    "ReviewCitationCheck",
    "ReviewCitationCheckCounts",
    "ReviewCitationDifference",
    "ReviewCitationFailure",
    "ReviewCitationIssue",
    "ReviewCitationRecord",
    "ReviewCitationResult",
    "ReviewClaim",
    "ReviewCredits",
    "ReviewEntity",
    "ReviewEnvelope",
    "ReviewFailure",
    "ReviewFull",
    "ReviewIssue",
    "ReviewIssues",
    "ReviewMoreCitation",
    "ReviewResult",
    "ReviewStarted",
    "ReviewSummary",
    "ReviewVerification",
    "ReviewVerificationCounts",
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
