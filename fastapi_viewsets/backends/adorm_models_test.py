"""
The Django models of the adorm backend's tests. Their adorm models are in `adorm_test_models/`, written by
`adorm generate` (`test_the_generated_models_are_up_to_date` in adorm_test.py checks and rewrites them). The tables
live in the PostgreSQL database of the `adorm` Django alias (see the repo-root conftest.py).
"""

import pytest

pytest.importorskip("adorm")

from django.db import models  # noqa: E402


class AdormClub(models.Model):
    name = models.CharField(max_length=100, unique=True)
    touched = models.DateTimeField(auto_now=True, null=True)

    class Meta:
        app_label = "backends"


class AdormMember(models.Model):
    name = models.CharField(max_length=100)
    email = models.CharField(max_length=100, unique=True)
    rating = models.IntegerField(null=True, blank=True)
    club = models.ForeignKey(AdormClub, null=True, blank=True, on_delete=models.PROTECT, related_name="members")
    copy = models.CharField(max_length=20, default="", blank=True)

    class Meta:
        app_label = "backends"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(rating__gte=0) | models.Q(rating__isnull=True),
                name="adorm_member_rating_not_negative",
            )
        ]


DJANGO_MODELS = (AdormClub, AdormMember)
"""In the order their tables are created; the models `adorm_test_models/` holds."""
