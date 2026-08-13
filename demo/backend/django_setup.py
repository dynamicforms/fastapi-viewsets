"""
A minimal Django ORM setup for the demo, backed by SQLite.

There is no Django project here and no manage.py - `settings.configure()` plus `django.setup()` is
the whole of it. That is deliberate: the point is that the ORM backend needs a database, not a
Django application, and a FastAPI service that wants one should not have to become a Django
project to get it.

Import this module before anything that touches the models.
"""

import os

from pathlib import Path

from django.conf import settings

DB_PATH = Path(os.environ.get("DEMO_DB_PATH", Path(__file__).parent / "demo.sqlite3"))

if not settings.configured:
    settings.configure(
        INSTALLED_APPS=["demo.backend"],
        DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": str(DB_PATH)}},
        USE_TZ=True,
        DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
    )
    import django

    django.setup()


from django.db import models  # noqa: E402


class MusicTrackRow(models.Model):
    """
    The same shape as the in-memory MusicTrack, so the two viewsets are directly comparable.

    Genres and moods are lists in the API model and TEXT here - the demo keeps them denormalised
    because normalising them would add joins that say nothing about the list pipeline.
    """

    title = models.CharField(max_length=200)
    artist = models.CharField(max_length=200, db_index=True)
    year = models.IntegerField(db_index=True)
    duration = models.CharField(max_length=16)
    genres = models.TextField()
    rating = models.IntegerField()
    favorite = models.BooleanField()
    play_count = models.IntegerField()
    moods = models.TextField()
    language = models.CharField(max_length=64)

    class Meta:
        app_label = "backend"
        indexes = [models.Index(fields=["year", "id"])]
        """
        Composite, in that order, because that is what a cursor over (year, id) needs to seek
        rather than scan - see the cursor section in TODO.md.
        """


def ensure_database(records: list[dict]) -> None:
    """
    Creates the table if it is missing and fills it once.

    Synchronous on purpose: this runs at startup, before the event loop is serving anything, and
    wrapping it would only obscure that.
    """
    from django.db import connection

    if MusicTrackRow._meta.db_table not in connection.introspection.table_names():
        with connection.schema_editor() as editor:
            editor.create_model(MusicTrackRow)

    if MusicTrackRow.objects.exists():
        return

    MusicTrackRow.objects.bulk_create(
        (
            MusicTrackRow(
                title=record["title"],
                artist=record["artist"],
                year=record["year"],
                duration=record["duration"],
                genres=",".join(record["genres"]),
                rating=record["rating"],
                favorite=record["favorite"],
                play_count=record["play_count"],
                moods=",".join(record["moods"]),
                language=record["language"],
            )
            for record in records
        ),
        batch_size=500,
    )
