"""Official Python SDK for the Lenz Claim Verification API for AI Product Teams.

    pip install lenz-io

Quickstart:

    from lenz_io import Lenz

    client = Lenz(api_key="lenz_...")
    v = client.verify_and_wait(claim="Sharks don't get cancer")
    print(v.verdict.label, v.verdict.score)
    # false 2.0
    for source in v.sources[:3]:
        print(" -", source.title, source.url)

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
    Assessment,
    Audit,
    BatchAccepted,
    CandidateClaim,
    DebateSide,
    EntityRef,
    ExtractedClaims,
    ExtractedEntity,
    FollowupHistory,
    FollowupReply,
    LibraryItem,
    LibraryList,
    RelatedVerifications,
    SimilarVerification,
    Source,
    TaskAccepted,
    TaskStatus,
    Usage,
    Verdict,
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
    "Assessment",
    "Audit",
    "BatchAccepted",
    "CandidateClaim",
    "DebateSide",
    "EntityRef",
    "ExtractedClaims",
    "ExtractedEntity",
    "FollowupHistory",
    "FollowupReply",
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
    "Verdict",
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
