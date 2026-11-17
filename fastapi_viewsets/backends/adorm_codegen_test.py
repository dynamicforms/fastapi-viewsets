"""
`adorm_test_models/` is what `adorm generate` writes for the Django models of `adorm_models_test.py`. With
`FASTAPI_VIEWSETS_WRITE_ADORM_MODELS=1` the test rewrites the package before checking it.
"""

import os

from pathlib import Path

import pytest

pytest.importorskip("adorm")

from adorm.codegen import AppSpec, check, plan, write  # noqa: E402
from adorm.codegen.django import introspect  # noqa: E402

from .adorm_models_test import DJANGO_MODELS  # noqa: E402

OUTPUT = Path(__file__).parent / "adorm_test_models"


def generated_files() -> dict[str, str]:
    """`adorm generate --line-length 120 backends`, restricted to `DJANGO_MODELS`."""
    labels = {model._meta.label for model in DJANGO_MODELS}
    apps = tuple(
        AppSpec(app.label, tuple(model for model in app.models if model.label in labels))
        for app in introspect(["backends"])
    )
    return plan(apps, line_length=120)


def test_the_generated_models_are_up_to_date():
    files = generated_files()
    if os.environ.get("FASTAPI_VIEWSETS_WRITE_ADORM_MODELS"):
        write(OUTPUT, files)
    assert [str(difference) for difference in check(OUTPUT, files)] == []
