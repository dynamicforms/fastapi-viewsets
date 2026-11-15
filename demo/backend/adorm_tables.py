"""
The table of the music library on PostgreSQL, which the adorm demo serves. Its list columns are arrays, so the
generated adorm model (`demo/backend/adorm_models/`) carries them as `list[str]`, the shape of the API's records.
"""

from django.contrib.postgres.fields import ArrayField
from django.db import models


class MusicTrackRecord(models.Model):
    title = models.CharField(max_length=200)
    artist = models.CharField(max_length=200, db_index=True)
    year = models.IntegerField(db_index=True)
    duration = models.CharField(max_length=16)
    genres = ArrayField(models.CharField(max_length=64), default=list, blank=True)
    rating = models.IntegerField()
    favorite = models.BooleanField(default=False)
    play_count = models.IntegerField(default=0)
    moods = ArrayField(models.CharField(max_length=64), default=list, blank=True)
    language = models.CharField(max_length=64)

    class Meta:
        app_label = "backend"
        indexes = [models.Index(fields=["year", "id"])]
