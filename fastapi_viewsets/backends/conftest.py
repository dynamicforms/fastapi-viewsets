"""
Fixtures of the adorm backend's tests: the PostgreSQL database of the `adorm` Django alias with the tables of
`adorm_models_test.py`, adorm configured on each driver, and a connection of the test's own.
"""

import os

import pytest
import pytest_asyncio

POSTGRES_URL_VARIABLE = "FASTAPI_VIEWSETS_TEST_POSTGRES_URL"
ADORM_ALIAS = "adorm"
DRIVERS = {"django": "adorm.drivers.django.DjangoDriver", "asyncpg": "adorm.drivers.asyncpg.AsyncpgDriver"}


def _server_arguments(settings_dict: dict) -> dict:
    return {
        "user": settings_dict["USER"] or None,
        "password": settings_dict["PASSWORD"] or None,
        "host": settings_dict["HOST"] or None,
        "port": int(settings_dict["PORT"]) if settings_dict["PORT"] else None,
    }


@pytest.fixture(scope="session")
def adorm_database() -> dict:
    """
    Creates the database of the `adorm` alias with the test tables, and drops it after the session. Returns the
    asyncpg connection arguments of the database. Skips without adorm, its drivers' libraries or
    `FASTAPI_VIEWSETS_TEST_POSTGRES_URL`.
    """
    pytest.importorskip("adorm")
    pytest.importorskip("asyncpg")
    psycopg = pytest.importorskip("psycopg")
    if not os.environ.get(POSTGRES_URL_VARIABLE):
        pytest.skip(f"{POSTGRES_URL_VARIABLE} names no PostgreSQL server")

    from django.db import connections
    from psycopg import sql

    from .adorm_models_test import DJANGO_MODELS

    settings_dict = connections[ADORM_ALIAS].settings_dict
    server = {key: value for key, value in _server_arguments(settings_dict).items() if value is not None}
    name = sql.Identifier(settings_dict["NAME"])
    with psycopg.connect(dbname="postgres", autocommit=True, **server) as admin:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(name))
        admin.execute(sql.SQL("CREATE DATABASE {}").format(name))
    with connections[ADORM_ALIAS].schema_editor() as editor:
        for model in DJANGO_MODELS:
            editor.create_model(model)
    connections[ADORM_ALIAS].close()
    try:
        yield {**server, "database": settings_dict["NAME"]}
    finally:
        with psycopg.connect(dbname="postgres", autocommit=True, **server) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(name))


@pytest_asyncio.fixture
async def database(adorm_database):
    """An asyncpg connection of the test's own to the test database, its tables emptied before the test."""
    import asyncpg

    connection = await asyncpg.connect(**adorm_database)
    await connection.execute("TRUNCATE backends_adormmember, backends_adormclub RESTART IDENTITY CASCADE")
    try:
        yield connection
    finally:
        await connection.close()


@pytest_asyncio.fixture(params=sorted(DRIVERS))
async def adorm_driver(request, database):  # noqa: ARG001 - needs the emptied tables
    """
    adorm configured with the profile `default` on the driver under test (`django`, `asyncpg`), over the test
    database; its pools close after the test. Returns the driver's name.
    """
    import adorm

    from adorm.settings import close_pools

    adorm.configure(
        profiles={
            "default": {
                "driver": DRIVERS[request.param],
                "alias": ADORM_ALIAS,
                "pool": {"max_connections": 2, "acquire_timeout": 5},
            }
        }
    )
    try:
        yield request.param
    finally:
        await close_pools()
        adorm.configure()
