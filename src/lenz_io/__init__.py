"""Official Python SDK for the Lenz Fact Checking API for AI Product Teams.

    pip install lenz-io

The fact-check API for AI products. Four primitives form a research-depth
ladder — find claims, judge them fast, prove them deep, follow up:

    from concurrent.futures import ThreadPoolExecutor
    from lenz_io import Lenz
    client = Lenz(api_key="lenz_...")

    # 1. /extract — pull verifiable claims out of text (free, 1000/day)
    out = client.extract(text=llm_output)
    claims = out.identified_claims or [out.claim]

    # 2. /assess — fast 3-model verdict on each (~10s, one credit per claim);
    #    independent calls, so run them side by side
    with ThreadPoolExecutor() as pool:
        quick = list(pool.map(lambda c: client.assess(claim=c).claims[0], claims))

    # 3. /verify — escalate the low-confidence ones to the full pipeline (~90s), in one batch
    doubtful = [{"claim": c.claim} for c in quick if c.confidence == "low"]
    results = client.verify_batch_and_wait(claims=doubtful)
    for r in results:
        if r.verification:
            print(r.verification.verdict, r.verification.lenz_score)

    # 4. /ask — follow-up questions grounded on a verification
    deep = results[0].verification
    reply = client.ask.send(deep.verification_id, message="Which source is strongest?")

See https://lenz.io/api/v1/docs/ for the full API reference.
"""

# Version is generated at build time by hatch-vcs from the git tag.
# `_version.py` is gitignored; falls back to "0.0.0+local" for editable
# dev installs where the file hasn't been written yet.
try:
    from ._version import __version__
except ImportError:
    __version__ = "0.0.0+local"

# Public surface
from .client import API_VERSION, DEFAULT_BASE_URL, Lenz, VerifyBatchItem
from .errors import (
    MAX_RETRY_AFTER_SLEEP,
    LenzAPIError,
    LenzAuthError,
    LenzError,
    LenzNeedsInputError,
    LenzPipelineError,
    LenzQuotaExceededError,
    LenzRateLimitError,
    LenzTimeoutError,
    LenzUpstreamUnavailableError,
    LenzValidationError,
    LenzWebhookSignatureError,
)
from .models import (
    AskHistory,
    AskMessage,
    AskReply,
    AssessClaim,
    Assessment,
    AssessResponse,
    Audit,
    BatchAccepted,
    BatchItemResult,
    CandidateClaim,
    DebateSide,
    EntityRef,
    ExtractedClaims,
    ExtractedEntity,
    ExtractStatus,
    FailureClass,
    LibraryItem,
    LibraryList,
    RelatedVerifications,
    SimilarVerification,
    Source,
    TaskAccepted,
    TaskStatus,
    Usage,
    UsageCapacity,
    UsageCredits,
    UsageExtract,
    Verification,
    VerificationList,
    VerificationListItem,
)
from .webhooks import (
    LenzWebhooks,
    VerificationCompleted,
    VerificationFailed,
    VerificationNeedsInput,
    WebhookEvent,
    verify_signature,
)

__all__ = [
    "API_VERSION",
    "DEFAULT_BASE_URL",
    "MAX_RETRY_AFTER_SLEEP",
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
    "ExtractStatus",
    "ExtractedClaims",
    "ExtractedEntity",
    "FailureClass",
    "Lenz",
    "LenzAPIError",
    "LenzAuthError",
    "LenzError",
    "LenzNeedsInputError",
    "LenzPipelineError",
    "LenzQuotaExceededError",
    "LenzRateLimitError",
    "LenzTimeoutError",
    "LenzUpstreamUnavailableError",
    "LenzValidationError",
    "LenzWebhookSignatureError",
    "LenzWebhooks",
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
    "VerificationCompleted",
    "VerificationFailed",
    "VerificationList",
    "VerificationListItem",
    "VerificationNeedsInput",
    "VerifyBatchItem",
    "WebhookEvent",
    "__version__",
    "verify_signature",
]
