"""
The music library on PostgreSQL through adorm: `AdormViewSet` at `/music-adorm`. On when adorm and the libraries of
its driver are installed and `DEMO_ADORM_DATABASE_URL` names a PostgreSQL server (`postgres://user:password@host:port/`);
`UNAVAILABLE` says why it is off. The database `adorm_fastapi_viewsets_demo` on that server is created and filled when
missing. `DEMO_ADORM_DRIVER` selects the adorm driver: `asyncpg` (default) or `django`.

Imports nothing of Django or adorm: `django_setup` reads it before Django is configured.
"""

import importlib.util
import os

from urllib.parse import unquote, urlsplit

DATABASE_URL = os.environ.get("DEMO_ADORM_DATABASE_URL", "")
DATABASE_NAME = "adorm_fastapi_viewsets_demo"
DATABASE_ALIAS = "adorm"
"""The Django alias of the database; the adorm connection profile `default` names it."""

DRIVERS = {"asyncpg": "adorm.drivers.asyncpg.AsyncpgDriver", "django": "adorm.drivers.django.DjangoDriver"}
DRIVER = os.environ.get("DEMO_ADORM_DRIVER", "asyncpg")

_REQUIRED = {"asyncpg": ("adorm", "psycopg", "asyncpg"), "django": ("adorm", "psycopg")}


def _unavailable_reason() -> str | None:
    if DRIVER not in DRIVERS:
        return f"DEMO_ADORM_DRIVER is {DRIVER!r}, not one of {', '.join(sorted(DRIVERS))}"
    missing = [name for name in _REQUIRED[DRIVER] if importlib.util.find_spec(name) is None]
    if missing:
        return f"not installed: {', '.join(missing)}"
    if not DATABASE_URL:
        return "DEMO_ADORM_DATABASE_URL names no PostgreSQL server"
    return None


UNAVAILABLE = _unavailable_reason()
"""Why the adorm demo is off; None when it is on."""

ENABLED = UNAVAILABLE is None


def disable(reason: str) -> None:
    """Turns the adorm demo off at run time: `/backends` reports `reason`, and `/music-adorm` answers 503."""
    global ENABLED, UNAVAILABLE  # noqa: PLW0603 - the module's state, read per request
    ENABLED, UNAVAILABLE = False, reason


def django_database() -> dict:
    """The Django `DATABASES` entry of the demo database on the server of `DATABASE_URL`."""
    parts = urlsplit(DATABASE_URL)
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": DATABASE_NAME,
        "USER": unquote(parts.username or ""),
        "PASSWORD": unquote(parts.password or ""),
        "HOST": parts.hostname or "",
        "PORT": str(parts.port or ""),
    }


class TablesRouter:
    """Sends Django's queries of the PostgreSQL table to the `adorm` alias."""

    def db_for_read(self, model, **_hints):
        return DATABASE_ALIAS if model._meta.label == "backend.MusicTrackRecord" else None

    db_for_write = db_for_read


def ensure_database(records: list[dict]) -> None:
    """
    Creates the database and the table when missing and fills the table when it does not hold `records`. Synchronous
    (Django's schema editor and psycopg): call it in a thread.
    """
    import psycopg

    from django.core.management.color import no_style
    from django.db import connections
    from psycopg import sql

    from demo.backend.adorm_tables import MusicTrackRecord

    entry = django_database()
    server = {
        key: value
        for key, value in {
            "user": entry["USER"],
            "password": entry["PASSWORD"],
            "host": entry["HOST"],
            "port": entry["PORT"],
        }.items()
        if value
    }
    with psycopg.connect(dbname="postgres", autocommit=True, **server) as admin:
        if not admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DATABASE_NAME,)).fetchone():
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DATABASE_NAME)))

    connection = connections[DATABASE_ALIAS]
    try:
        if MusicTrackRecord._meta.db_table not in connection.introspection.table_names():
            with connection.schema_editor() as editor:
                editor.create_model(MusicTrackRecord)
        rows = MusicTrackRecord.objects.using(DATABASE_ALIAS)
        highest = rows.order_by("-id").values_list("id", flat=True).first()
        if rows.count() == len(records) and highest == len(records):
            return
        rows.all().delete()
        rows.bulk_create((MusicTrackRecord(**record) for record in records), batch_size=500)
        # The generator's ids are written explicitly: the sequence continues after the highest.
        with connection.cursor() as cursor:
            for statement in connection.ops.sequence_reset_sql(no_style(), [MusicTrackRecord]):
                cursor.execute(statement)
    finally:
        connection.close()


def configure_adorm() -> None:
    """adorm's connection profile `default`: the selected driver on the demo database."""
    import adorm

    adorm.configure(
        profiles={"default": {"driver": DRIVERS[DRIVER], "alias": DATABASE_ALIAS, "pool": {"max_connections": 8}}}
    )
