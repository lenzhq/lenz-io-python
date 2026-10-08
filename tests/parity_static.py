"""Model schemas and pickles, frozen by ``parity_generate.py`` from the
previous release (``schemas.json``, ``pickles.json``) and checked by
``test_parity.py``: this release must publish the same JSON schemas for every
model, and unpickle and dump what the previous release pickled."""

from __future__ import annotations

import base64
import pickle
import warnings
from typing import Any

from parity_observe import _model_for, load, names
from pydantic import BaseModel

from lenz_io import models


def model_classes() -> dict[str, type[BaseModel]]:
    return {
        name: obj
        for name in models.__all__
        if isinstance(obj := getattr(models, name), type) and issubclass(obj, BaseModel)
    }


def schemas() -> dict[str, Any]:
    out = {}
    for name, cls in model_classes().items():
        for mode in ("validation", "serialization"):
            out[f"{name}:{mode}"] = cls.model_json_schema(mode=mode)
    return out


def _pickle_names() -> list[str]:
    return [n for n in names() if _model_for(n) is not None and load("legacy", n)["status"] < 400]


def pickles() -> dict[str, str]:
    out = {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        for name in _pickle_names():
            model = _model_for(name).model_validate(load("legacy", name)["body"])
            out[name] = base64.b64encode(pickle.dumps(model, protocol=4)).decode()
    return out
