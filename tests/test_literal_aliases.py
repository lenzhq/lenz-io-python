"""``Verdict``, ``VerdictLabel``, ``VerificationVerdict``, ``Confidence`` and ``Depth``: the accepted
values as exported ``Literal`` aliases, for comparisons, exhaustive matching
and docs. The model fields and method arguments stay ``str``, so a value a
later API adds still reads and still sends."""

from __future__ import annotations

import inspect
from typing import get_args

import lenz_io
from lenz_io import (
    AssessClaim,
    Confidence,
    Depth,
    FailureClass,
    Lenz,
    Verdict,
    VerdictLabel,
    Verification,
    VerificationVerdict,
)


def test_the_values() -> None:
    assert get_args(VerdictLabel) == ("True", "Mostly True", "Mixed", "Mostly False", "False")
    assert get_args(Verdict) == ("True", "Mostly True", "Mixed", "Mostly False", "False", "Error")
    # /verify's alias adds the sixth outcome; the five labels and /assess's
    # alias are untouched.
    assert get_args(VerificationVerdict) == (*get_args(VerdictLabel), "No Public Record")
    assert get_args(FailureClass) == ("upstream_unavailable", "invalid_input", "cancelled", "internal")
    assert get_args(Confidence) == ("low", "medium", "high")
    assert get_args(Depth) == ("standard", "low")


def test_exported() -> None:
    for name in ("Verdict", "VerdictLabel", "VerificationVerdict", "FailureClass", "Confidence", "Depth"):
        assert name in lenz_io.__all__


def test_fields_and_arguments_stay_open_strings() -> None:
    for model in (Verification, AssessClaim):
        assert model.model_fields["verdict"].annotation is str
        assert model.model_fields["confidence"].annotation is str
    assert Verification.model_validate({"verdict": "Unproven", "confidence": "unknown"}).verdict == "Unproven"
    assert inspect.signature(Lenz.verify).parameters["depth"].annotation == "str"
