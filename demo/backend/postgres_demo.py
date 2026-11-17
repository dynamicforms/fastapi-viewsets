"""
The music library on PostgreSQL. On when psycopg is installed and `DEMO_DATABASE_URL` names a reachable PostgreSQL
server (`postgres://user:password@host:port/`); `UNAVAILABLE` says why it is off. Then the Django ORM data source
(`/music-db`) and the adorm one (`/music-adorm`) both serve the table `MusicTrackRecord` in the database
`adorm_fastapi_viewsets_demo`, which is created and filled when missing, and the server-side benchmark can run.

adorm additionally needs adorm and the libraries of its driver; `ADORM_UNAVAILABLE` says why it is off.
`DEMO_ADORM_DRIVER` selects the adorm driver: `asyncpg` (default) or `django`.

The benchmark's connections (Django alias and adorm profile `bench`) go to `127.0.0.1:$DEMO_BENCH_DB_PORT`, the
latency proxy, when that variable is set, else straight to the server.

Imports nothing of Django or adorm: `django_setup` reads it before Django is configured.
"""

import importlib.util
import os

from urllib.parse import unquote, urlsplit

DATABASE_URL = os.environ.get("DEMO_DATABASE_URL", "")
DATABASE_NAME = "adorm_fastapi_viewsets_demo"
DATABASE_ALIAS = "postgres"
"""The Django alias of the database for the UI's data sources; the adorm profile `default` names it."""
BENCH_ALIAS = "bench"
"""The Django alias and the adorm profile of the benchmark's connections."""

BENCH_DB_PORT = os.environ.get("DEMO_BENCH_DB_PORT", "")
"""The latency proxy's port; empty when the benchmark connects to the server directly."""

ADORM_POOL = int(os.environ.get("DEMO_ADORM_POOL", "40"))
"""The adorm pool size (`max_connections`) of the benchmark profile, per process."""

DRIVERS = {"asyncpg": "adorm.drivers.asyncpg.AsyncpgDriver", "django": "adorm.drivers.django.DjangoDriver"}
DRIVER = os.environ.get("DEMO_ADORM_DRIVER", "asyncpg")

_ADORM_REQUIRED = {"asyncpg": ("adorm", "asyncpg"), "django": ("adorm",)}

CONNECT_TIMEOUT = 3
"""Seconds the startup check waits for the PostgreSQL server."""


def _server_parameters() -> dict[str, str]:
    """psycopg's connection parameters of the server in `DATABASE_URL`, without the database name."""
    parts = urlsplit(DATABASE_URL)
    parameters = {
        "user": unquote(parts.username or ""),
        "password": unquote(parts.password or ""),
        "host": parts.hostname or "",
        "port": str(parts.port or ""),
    }
    return {key: value for key, value in parameters.items() if value}


def _unavailable_reason() -> str | None:
    if not DATABASE_URL:
        return "DEMO_DATABASE_URL names no PostgreSQL server (demo.py --db URL)"
    if importlib.util.find_spec("psycopg") is None:
        return "not installed: psycopg"
    import psycopg

    try:
        psycopg.connect(dbname="postgres", connect_timeout=CONNECT_TIMEOUT, **_server_parameters()).close()
    except psycopg.Error as error:
        return f"the PostgreSQL server cannot be used: {error}".strip()
    return None


def _adorm_unavailable_reason() -> str | None:
    if UNAVAILABLE is not None:
        return UNAVAILABLE
    if DRIVER not in DRIVERS:
        return f"DEMO_ADORM_DRIVER is {DRIVER!r}, not one of {', '.join(sorted(DRIVERS))}"
    missing = [name for name in _ADORM_REQUIRED[DRIVER] if importlib.util.find_spec(name) is None]
    if missing:
        return f"not installed: {', '.join(missing)}"
    return None


UNAVAILABLE = _unavailable_reason()
"""Why PostgreSQL is off; None when it is on."""
ENABLED = UNAVAILABLE is None

ADORM_UNAVAILABLE = _adorm_unavailable_reason()
"""Why the adorm data source is off; None when it is on."""
ADORM_ENABLED = ADORM_UNAVAILABLE is None


def disable(reason: str) -> None:
    """Turns PostgreSQL off at run time: `/backends` reports `reason`; `/music-db` and `/music-adorm` answer 503."""
    global ENABLED, UNAVAILABLE, ADORM_ENABLED, ADORM_UNAVAILABLE  # noqa: PLW0603 - the module's state, read per request
    ENABLED, UNAVAILABLE = False, reason
    ADORM_ENABLED, ADORM_UNAVAILABLE = False, reason


def django_database() -> dict:
    """The Django `DATABASES` entry of the demo database, connected directly."""
    server = _server_parameters()
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": DATABASE_NAME,
        "USER": server.get("user", ""),
        "PASSWORD": server.get("password", ""),
        "HOST": server.get("host", ""),
        "PORT": server.get("port", ""),
    }


def bench_database() -> dict:
    """
    The Django `DATABASES` entry of the benchmark: through the latency proxy when it runs, with persistent
    connections, so every executor reuses its connection.
    """
    entry = {**django_database(), "CONN_MAX_AGE": None}
    if BENCH_DB_PORT:
        entry.update(HOST="127.0.0.1", PORT=BENCH_DB_PORT)
    return entry


class TablesRouter:
    """Sends Django's queries of the PostgreSQL table to the `postgres` alias unless a query names another."""

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

    with psycopg.connect(dbname="postgres", autocommit=True, **_server_parameters()) as admin:
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
    """adorm's connection profiles: `default` on the demo database, `bench` on the benchmark's alias."""
    import adorm

    adorm.configure(
        profiles={
            "default": {"driver": DRIVERS[DRIVER], "alias": DATABASE_ALIAS, "pool": {"max_connections": 8}},
            BENCH_ALIAS: {"driver": DRIVERS[DRIVER], "alias": BENCH_ALIAS, "pool": {"max_connections": ADORM_POOL}},
        }
    )
