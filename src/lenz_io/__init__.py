"""Official Python SDK for the Lenz Claim Verification API for AI Product Teams.

    pip install lenz-io

The fact-check API for AI products. Four primitives form a research-depth
ladder — find claims, judge them fast, prove them deep, follow up:

    from lenz_io import Lenz
    client = Lenz(api_key="lenz_...")

    # 1. /extract — pull verifiable claims out of text (free, 1000/day)
    claims = client.extract(text=llm_output).identified_claims

    # 2. /assess — fast 3-model verdict on each (~5-10s, paid)
    quick = client.assess(text=llm_output)

    # 3. /verify — escalate low-confidence to the full pipeline (~90s, paid)
    for c in quick.claims:
        if c.confidence == "low":
            deep = client.verify_and_wait(claim=c.claim)
            print(deep.verdict, deep.lenz_score)

    # 4. /ask — follow-up questions grounded on a verification
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
from .client import API_VERSION, DEFAULT_BASE_URL, Lenz
from .errors import (
    LenzAPIError,
    LenzAuthError,
    LenzError,
    LenzNeedsInputError,
    LenzPipelineError,
    LenzQuotaExceededError,
    LenzRateLimitError,
    LenzTimeoutError,
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
    CandidateClaim,
    DebateSide,
    EntityRef,
    ExtractedClaims,
    ExtractedEntity,
    LibraryItem,
    LibraryList,
    RelatedVerifications,
    SimilarVerification,
    Source,
    TaskAccepted,
    TaskStatus,
    Usage,
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
    "Lenz",
    "LenzAPIError",
    "LenzAuthError",
    "LenzError",
    "LenzNeedsInputError",
    "LenzPipelineError",
    "LenzQuotaExceededError",
    "LenzRateLimitError",
    "LenzTimeoutError",
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
    "Verification",
    "VerificationCompleted",
    "VerificationFailed",
    "VerificationList",
    "VerificationListItem",
    "VerificationNeedsInput",
    "WebhookEvent",
    "__version__",
    "verify_signature",
]
