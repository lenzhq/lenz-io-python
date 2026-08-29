"""Usage model semantics — the credit pool, its projections, and the alias.

Shape drift is covered by ``test_contract.py`` against the frozen server
fixture. This file covers behaviour the fixture can't state: that ``bonus``
and its deprecated ``credits`` alias track each other whichever side of the
API deploy we're talking to, and that reading the alias warns.
"""

from __future__ import annotations

import warnings

import pytest

from lenz_io.models import Usage, UsageCapacity, UsageCredits

# The live shape (2026-08-29 onward), trimmed to the fields under test.
POOL_PAYLOAD = {
    "plan": "developer",
    "quota_resets_at": "2026-09-01T00:00:00+00:00",
    "credits": {
        "total": 5200,
        "used": 130,
        "remaining": 5070,
        "bonus": 200,
        "resets_at": "2026-09-01T00:00:00+00:00",
    },
    "costs": {"verify": 10, "verify_low": 5, "assess": 1, "ask": 1, "extract": 0},
    "verify": {
        "quota_used": 13,
        "quota_total": 520,
        "quota_remaining": 507,
        "bonus": 20,
        "credits": 20,
        "remaining": 507,
    },
    "assess": {
        "quota_used": 130,
        "quota_total": 5200,
        "quota_remaining": 5070,
        "bonus": 200,
        "credits": 200,
        "remaining": 5070,
    },
    "extract": {"calls_today": 4, "daily_limit": 1000, "unlimited": False},
    "has_webhook_secret": False,
}


def test_credit_pool_and_price_list_parse():
    u = Usage.model_validate(POOL_PAYLOAD)
    assert isinstance(u.credits, UsageCredits)
    assert (u.credits.total, u.credits.used, u.credits.remaining) == (5200, 130, 5070)
    assert u.credits.bonus == 200
    assert u.credits.resets_at == "2026-09-01T00:00:00+00:00"
    assert u.costs == {"verify": 10, "verify_low": 5, "assess": 1, "ask": 1, "extract": 0}


def test_costs_carries_the_low_depth_verify_price():
    """``verify_low`` is half a standard verify and rides in the same map.

    ``costs`` is an open ``dict[str, int]``, so a new price key needs no model
    change — this test is the tripwire that it actually survives parsing
    rather than being dropped into ``__pydantic_extra__``."""
    u = Usage.model_validate(POOL_PAYLOAD)
    assert u.costs["verify_low"] == 5
    assert u.costs["verify_low"] * 2 == u.costs["verify"]


def test_verify_low_is_a_price_not_a_capability():
    """There is deliberately no ``verify_low`` projection block.

    A block would report the same balance in a second unit. Clients that want
    the low-depth count divide the balance by the price themselves. If a
    ``verify_low`` attribute ever appears on ``Usage``, someone modelled a
    price as a capability — that is the bug this guards."""
    u = Usage.model_validate(POOL_PAYLOAD)
    assert "verify_low" not in Usage.model_fields
    assert not hasattr(u, "verify_low")
    # The count is a division the caller does, and it is not verify x 2:
    # flooring is against the balance, not against the standard projection.
    assert u.credits.remaining // u.costs["verify_low"] == 1014


def test_capability_blocks_are_projections_of_the_one_pool():
    """Each block is the pool divided by that capability's weight, floored."""
    u = Usage.model_validate(POOL_PAYLOAD)
    assert u.verify.remaining == u.credits.remaining // u.costs["verify"]
    assert u.assess.remaining == u.credits.remaining // u.costs["assess"]
    # 200 bonus credits buy 20 verifications but 200 assessments.
    assert (u.verify.bonus, u.assess.bonus) == (20, 200)
    # used + remaining == total holds in every block (the server derives used).
    assert u.verify.quota_used + u.verify.quota_remaining == u.verify.quota_total


def test_reading_the_credits_alias_warns_and_returns_bonus():
    cap = UsageCapacity.model_validate(POOL_PAYLOAD["verify"])
    assert cap.bonus == 20
    with pytest.deprecated_call(match="2026-11-29"):
        assert cap.credits == 20


def test_dumping_keeps_the_alias_and_does_not_warn():
    """Serialization must stay warning-free — a `--json` dump of usage is not
    a deprecated read, and the key stays on the wire until 2026-11-29."""
    cap = UsageCapacity.model_validate(POOL_PAYLOAD["verify"])
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        dumped = cap.model_dump()
    assert dumped["bonus"] == 20
    assert dumped["credits"] == 20


def test_old_server_sending_only_credits_still_fills_bonus():
    """Pre-pool server (or a mid-deploy revision): `credits` only."""
    cap = UsageCapacity.model_validate(
        {"quota_used": 120, "quota_total": 500, "quota_remaining": 380, "credits": 25, "remaining": 405}
    )
    assert cap.bonus == 25
    with pytest.deprecated_call():
        assert cap.credits == 25


def test_server_after_the_alias_removal_still_fills_credits():
    """After 2026-11-29 the server drops `credits`; the alias keeps reading."""
    cap = UsageCapacity.model_validate(
        {"quota_used": 13, "quota_total": 520, "quota_remaining": 507, "bonus": 20, "remaining": 507}
    )
    assert cap.bonus == 20
    with pytest.deprecated_call():
        assert cap.credits == 20


def test_pre_pool_server_leaves_the_balance_empty_not_wrong():
    """No `credits` block at all → an all-zero pool the caller can detect,
    rather than a ValidationError or a fabricated balance."""
    u = Usage.model_validate(
        {
            "plan": "plus",
            "quota_resets_at": "2026-09-01T00:00:00+00:00",
            "verify": {"quota_used": 5, "quota_total": 100, "quota_remaining": 95, "credits": 0, "remaining": 95},
            "extract": {"calls_today": 0, "daily_limit": 1000, "unlimited": False},
        }
    )
    assert u.credits.total == 0
    assert u.credits.remaining == 0
    assert u.costs == {}
    assert u.verify.remaining == 95
