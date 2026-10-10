"""Official Python SDK for the Lenz Fact Checking API for AI Product Teams.

    pip install lenz-io

The fact-check API for AI products, in six calls: four form a research-depth
ladder (find claims, judge them fast, prove them deep, follow up), ``review``
runs the ladder on a whole draft, and ``citecheck`` checks a draft's sources
on its own:

    from lenz_io import Lenz
    client = Lenz()  # reads LENZ_API_KEY

    row = client.assess(claim="The Great Wall of China is visible from space.").claims[0]
    print(row.verdict, row.confidence)

    # /review — every claim quick-checked, the doubtful ones deep-checked (2-4 min)
    review = client.review_and_wait(text=draft)
    for issue in review.issues:
        print(issue.verdict, issue.claim, issue.suggested_rewrite)

The primitives, call by call:

    from lenz_io import Lenz
    client = Lenz(api_key="lenz_...")

    # 1. /extract — pull verifiable claims out of text (free, 1000/day)
    out = client.extract(text=llm_output)
    claims = [c.claim for c in out.claims]

    # 2. /assess — 20 claims a call (extract finds up to 100), one row per claim, same order (paid)
    quick = [row for i in range(0, len(claims), 20) for row in client.assess(claims=claims[i : i + 20]).claims]
    # a row with status == "failed" got no verdict: see its failure.code and failure.hint;
    # a compound item lists the claims it did not assess in more_claims

    # 3. /verify — escalate the low-confidence rows to the full pipeline (~90s, paid)
    # verify_batch_and_wait takes up to 20 claims a call: the first 20 here
    doubtful = [{"claim": c.claim} for c in quick if c.status == "completed" and c.confidence == "low"][:20]
    results = client.verify_batch_and_wait(claims=doubtful) if doubtful else []

    # 4. /ask — follow-up questions grounded on a verification (when one was escalated)
    deep = next((r.verification for r in results if r.verification is not None), None)
    if deep is not None:
        reply = client.ask.send(deep.verification_id, message="Which source is strongest?")

The same calls from asyncio, as coroutines:

    from lenz_io import AsyncLenz

    async with AsyncLenz() as client:
        row = (await client.assess(claim="...")).claims[0]

See https://lenz.io/api/v1/docs/ for the full API reference.
"""

from typing import TYPE_CHECKING, Any

# Version is generated at build time by hatch-vcs from the git tag.
# `_version.py` is gitignored; falls back to "0.0.0+local" for editable
# dev installs where the file hasn't been written yet.
try:
    from ._version import __version__
except ImportError:
    __version__ = "0.0.0+local"

if TYPE_CHECKING:
    from .async_client import AsyncLenz

# Public surface
from .client import API_VERSION, DEFAULT_BASE_URL, NOT_GIVEN, CitationPair, Lenz, NotGiven, VerifyBatchItem
from .errors import (
    MAX_RETRY_AFTER_SLEEP,
    USAGE_ERROR_CODES,
    CitecheckFailed,
    CitecheckFailedError,
    CitecheckTimeout,
    CitecheckTimeoutError,
    LenzAPIError,
    LenzApiVersionError,
    LenzAuthError,
    LenzConnectionError,
    LenzError,
    LenzGoneError,
    LenzInvalidKeyError,
    LenzInvalidResponseError,
    LenzNeedsInputError,
    LenzNotFoundError,
    LenzPipelineError,
    LenzQuotaExceededError,
    LenzRateLimitError,
    LenzRequestTimeoutError,
    LenzTimeoutError,
    LenzUpstreamUnavailableError,
    LenzUsageError,
    LenzValidationError,
    LenzVerificationNotReadyError,
    LenzWebhookSignatureError,
    ReviewFailed,
    ReviewFailedError,
    ReviewTimeout,
    ReviewTimeoutError,
    UsageErrorCode,
)
from .models import (
    AskHistory,
    AskMessage,
    AskReply,
    AssessClaim,
    Assessment,
    AssessResponse,
    AssessStatus,
    Audit,
    BatchAccepted,
    BatchItemResult,
    CancelResult,
    CandidateClaim,
    Certificate,
    Citecheck,
    CitecheckStarted,
    ClaimLocation,
    Confidence,
    Coverage,
    CoverageReason,
    CoverageStatus,
    DebateSide,
    Depth,
    EntityRef,
    Escalation,
    EscalationPolicy,
    ExtractedClaim,
    ExtractedClaims,
    ExtractedEntity,
    ExtractStatus,
    FailureBlock,
    FailureClass,
    LibraryItem,
    LibraryList,
    Position,
    Progress,
    RelatedVerifications,
    ReviewCitation,
    ReviewCitationFailure,
    ReviewCitationIssue,
    ReviewClaim,
    ReviewFailure,
    ReviewFull,
    ReviewIssue,
    ReviewIssues,
    ReviewStarted,
    SimilarVerification,
    Source,
    SuggestedEdit,
    SuggestedEdits,
    TaskAccepted,
    TaskStatus,
    Usage,
    UsageCapacity,
    UsageCredits,
    UsageExtract,
    Verdict,
    VerdictLabel,
    Verification,
    VerificationList,
    VerificationListItem,
)
from .webhooks import (
    CertificateTimestamped,
    CitecheckEvent,
    LenzWebhooks,
    ReviewEvent,
    VerificationCancelled,
    VerificationCompleted,
    VerificationFailed,
    VerificationNeedsInput,
    WebhookEvent,
    parse_webhook,
    verify_signature,
)

__all__ = [
    "API_VERSION",
    "DEFAULT_BASE_URL",
    "MAX_RETRY_AFTER_SLEEP",
    "NOT_GIVEN",
    "USAGE_ERROR_CODES",
    "AskHistory",
    "AskMessage",
    "AskReply",
    "AssessClaim",
    "AssessResponse",
    "AssessStatus",
    "Assessment",
    "AsyncLenz",
    "Audit",
    "BatchAccepted",
    "BatchItemResult",
    "CancelResult",
    "CandidateClaim",
    "Certificate",
    "CertificateTimestamped",
    "CitationPair",
    "Citecheck",
    "CitecheckEvent",
    "CitecheckFailed",
    "CitecheckFailedError",
    "CitecheckStarted",
    "CitecheckTimeout",
    "CitecheckTimeoutError",
    "ClaimLocation",
    "Confidence",
    "Coverage",
    "CoverageReason",
    "CoverageStatus",
    "DebateSide",
    "Depth",
    "EntityRef",
    "Escalation",
    "EscalationPolicy",
    "ExtractStatus",
    "ExtractedClaim",
    "ExtractedClaims",
    "ExtractedEntity",
    "FailureBlock",
    "FailureClass",
    "Lenz",
    "LenzAPIError",
    "LenzApiVersionError",
    "LenzAuthError",
    "LenzConnectionError",
    "LenzError",
    "LenzGoneError",
    "LenzInvalidKeyError",
    "LenzInvalidResponseError",
    "LenzNeedsInputError",
    "LenzNotFoundError",
    "LenzPipelineError",
    "LenzQuotaExceededError",
    "LenzRateLimitError",
    "LenzRequestTimeoutError",
    "LenzTimeoutError",
    "LenzUpstreamUnavailableError",
    "LenzUsageError",
    "LenzValidationError",
    "LenzVerificationNotReadyError",
    "LenzWebhookSignatureError",
    "LenzWebhooks",
    "LibraryItem",
    "LibraryList",
    "NotGiven",
    "Position",
    "Progress",
    "RelatedVerifications",
    "ReviewCitation",
    "ReviewCitationFailure",
    "ReviewCitationIssue",
    "ReviewClaim",
    "ReviewEvent",
    "ReviewFailed",
    "ReviewFailedError",
    "ReviewFailure",
    "ReviewFull",
    "ReviewIssue",
    "ReviewIssues",
    "ReviewStarted",
    "ReviewTimeout",
    "ReviewTimeoutError",
    "SimilarVerification",
    "Source",
    "SuggestedEdit",
    "SuggestedEdits",
    "TaskAccepted",
    "TaskStatus",
    "Usage",
    "UsageCapacity",
    "UsageCredits",
    "UsageErrorCode",
    "UsageExtract",
    "Verdict",
    "VerdictLabel",
    "Verification",
    "VerificationCancelled",
    "VerificationCompleted",
    "VerificationFailed",
    "VerificationList",
    "VerificationListItem",
    "VerificationNeedsInput",
    "VerifyBatchItem",
    "WebhookEvent",
    "__version__",
    "parse_webhook",
    "verify_signature",
]


def __dir__() -> list[str]:
    return sorted({*globals(), "AsyncLenz"})


def __getattr__(name: str) -> Any:
    # ``AsyncLenz`` is imported on first use, so ``import lenz_io`` (and the
    # ``lenz`` CLI) does not load asyncio and the async client for sync users.
    if name == "AsyncLenz":
        from .async_client import AsyncLenz

        return AsyncLenz
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
