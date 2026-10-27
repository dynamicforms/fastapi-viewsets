"""
A minimal Django ORM setup for the demo, backed by SQLite: `settings.configure()` plus
`django.setup()`, with no Django project. Import this module before anything that touches the models.
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
    """The in-memory MusicTrack's shape; genres and moods are comma-separated TEXT."""

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
        """A cursor over (year, id) seeks on this index instead of scanning."""


def ensure_database(records: list[dict]) -> None:
    """
    Creates the table if it is missing and fills it. Synchronous, as Django refuses a synchronous query
    in a running event loop: call it via `sync_to_async`.
    """
    from django.db import connection

    if MusicTrackRow._meta.db_table not in connection.introspection.table_names():
        with connection.schema_editor() as editor:
            editor.create_model(MusicTrackRow)

    # Refill when the table does not match `records` (e.g. another DEMO_LIBRARY_SIZE). The highest id
    # is checked too, as AUTOINCREMENT never reuses ids.
    highest = MusicTrackRow.objects.order_by("-id").values_list("id", flat=True).first()
    if MusicTrackRow.objects.count() == len(records) and highest == len(records):
        return
    MusicTrackRow.objects.all().delete()

    MusicTrackRow.objects.bulk_create(
        (
            MusicTrackRow(
                # The generator's id, so both backends serve the same records.
                id=record["id"],
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
