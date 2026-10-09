"""Every field whose 2.x name is deprecated says so in the JSON schema."""

from __future__ import annotations

import pytest

from lenz_io import models

DEPRECATED_FIELDS = {
    "CandidateClaim": ["text"],
    "ExtractedClaims": ["claim", "identified_claims", "locations", "candidate_claims"],
    "AssessClaim": ["error_code", "hint", "identified_claims", "candidate_claims"],
    "ReviewAssessment": ["error_code", "hint", "identified_claims"],
    "AssessResponse": ["error", "error_code", "candidate_claims"],
    "TaskAccepted": ["claim_text"],
    "BatchItemResult": ["claim_text"],
    "TaskStatus": [
        "error",
        "failure_reason",
        "failure_detail",
        "failure_class",
        "retryable",
        "docs_url",
        "candidates",
        "similar_claims",
    ],
    "FailureBlock": ["failure_reason"],
    "Verification": ["modified_at"],
    "VerificationListItem": ["modified_at"],
    "ReviewVerification": ["modified_at"],
    "ReviewSummary": ["claim_limit_reached", "citation_limit_reached"],
    "CitecheckSummary": ["citation_limit_reached"],
    "Usage": ["quota_resets_at", "verify", "ask", "assess"],
    "UsageCredits": ["bonus"],
    "UsageCapacity": ["credits"],
}

#: Fields that stay current: a marker here would warn about a name that has no replacement.
CURRENT_FIELDS = {"TaskStatus": ["hint", "result", "claims"], "Usage": ["credits", "extract"]}


def _properties(cls_name: str, mode: str) -> dict[str, dict[str, object]]:
    return getattr(models, cls_name).model_json_schema(mode=mode)["properties"]


@pytest.mark.parametrize("mode", ["validation", "serialization"])
@pytest.mark.parametrize(("cls_name", "fields"), sorted(DEPRECATED_FIELDS.items()))
def test_deprecated_fields_are_marked(cls_name: str, fields: list[str], mode: str) -> None:
    props = _properties(cls_name, mode)
    assert [f for f in fields if props[f].get("deprecated") is not True] == []


@pytest.mark.parametrize(("cls_name", "fields"), sorted(CURRENT_FIELDS.items()))
def test_current_fields_are_not_marked(cls_name: str, fields: list[str]) -> None:
    props = _properties(cls_name, "validation")
    assert [f for f in fields if props[f].get("deprecated")] == []
