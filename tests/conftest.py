"""Test fixtures for lenz-io.

Reuses one ``respx`` mock router per test via the ``respx_mock`` fixture
shipped with the respx package. No real network in any test.
"""

from __future__ import annotations

import pytest

from lenz_io import Lenz


@pytest.fixture()
def client():
    """Authed client pointed at the default base URL."""
    with Lenz(api_key="lenz_test_abc123") as c:
        yield c


@pytest.fixture()
def unauth_client():
    """Un-keyed client — only library methods should work."""
    with Lenz() as c:
        yield c


@pytest.fixture()
def custom_base_client():
    with Lenz(api_key="lenz_test", base_url="http://localhost:8001/api/v1") as c:
        yield c
