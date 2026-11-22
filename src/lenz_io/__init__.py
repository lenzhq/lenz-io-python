"""Official Python SDK for the Lenz Accuracy Verification API.

    pip install lenz-io

Check the facts, figures, and citations in your work, in six calls: four form
a research-depth ladder (find claims, judge them fast, prove them deep, follow
up), ``review`` runs the ladder on a whole draft, and ``citecheck`` checks a
draft's sources on its own:

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
from ._core import BatchClaimInput, CitationPairInput
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
    LenzMissingKeyError,
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
    Escalation,
    EscalationPolicy,
    Evidence,
    ExtractedClaim,
    ExtractedClaims,
    ExtractStatus,
    FailureBlock,
    FailureClass,
    FileInput,
    Position,
    Progress,
    Result,
    ReviewCitation,
    ReviewCitationFailure,
    ReviewCitationIssue,
    ReviewClaim,
    ReviewEscalateInput,
    ReviewExport,
    ReviewExportFormat,
    ReviewFailure,
    ReviewFull,
    ReviewIssue,
    ReviewIssues,
    ReviewList,
    ReviewListItem,
    ReviewStarted,
    SearchedPage,
    Source,
    SuggestedEdit,
    SuggestedEdits,
    SuppliedSource,
    TaskAccepted,
    TaskAcceptedResult,
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
    VerificationResult,
    VerificationVerdict,
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
    "AssessClaim",
    "AssessResponse",
    "AssessStatus",
    "Assessment",
    "AsyncLenz",
    "Audit",
    "BatchAccepted",
    "BatchClaimInput",
    "BatchItemResult",
    "CancelResult",
    "CandidateClaim",
    "Certificate",
    "CertificateTimestamped",
    "CitationPair",
    "CitationPairInput",
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
    "Escalation",
    "EscalationPolicy",
    "Evidence",
    "ExtractStatus",
    "ExtractedClaim",
    "ExtractedClaims",
    "FailureBlock",
    "FailureClass",
    "FileInput",
    "Lenz",
    "LenzAPIError",
    "LenzApiVersionError",
    "LenzAuthError",
    "LenzConnectionError",
    "LenzError",
    "LenzGoneError",
    "LenzInvalidKeyError",
    "LenzInvalidResponseError",
    "LenzMissingKeyError",
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
    "NotGiven",
    "Position",
    "Progress",
    "Result",
    "ReviewCitation",
    "ReviewCitationFailure",
    "ReviewCitationIssue",
    "ReviewClaim",
    "ReviewEscalateInput",
    "ReviewEvent",
    "ReviewExport",
    "ReviewExportFormat",
    "ReviewFailed",
    "ReviewFailedError",
    "ReviewFailure",
    "ReviewFull",
    "ReviewIssue",
    "ReviewIssues",
    "ReviewList",
    "ReviewListItem",
    "ReviewStarted",
    "ReviewTimeout",
    "ReviewTimeoutError",
    "SearchedPage",
    "Source",
    "SuggestedEdit",
    "SuggestedEdits",
    "SuppliedSource",
    "TaskAccepted",
    "TaskAcceptedResult",
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
    "VerificationResult",
    "VerificationVerdict",
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
