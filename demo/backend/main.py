import asyncio
import os
import re

from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from asgiref.sync import sync_to_async
from fastapi import APIRouter, FastAPI, WebSocket
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse
from muxws import accept, Stream
from pydantic import BaseModel, Field

from demo.backend import benchmark, postgres_demo
from demo.backend.benchmark.api import (
    BenchInFlightMiddleware,
    READER_IDLE_POLL_S,
    router as benchmark_router,
    start_result_readers as start_bench_result_readers,
)
from demo.backend.django_setup import ensure_database, MusicTrackRow
from demo.backend.openapi_docs import add_try_links, REDOC_HEAD, SWAGGER_HEAD
from demo.backend.viewsets import (
    DB_TASK_PREFIX,
    MEMORY_TASK_PREFIX,
    MusicTrackDbViewSet,
    MusicTrackViewSet,
    records,
    USE_CELERY,
)
from fastapi_viewsets.decorators.route_viewset import route_viewset
from fastapi_viewsets.endpoint_docs import apply_viewset_tags
from fastapi_viewsets.error_responses import install_error_handlers
from fastapi_viewsets.events import check_events, register_events, start_events, stop_events
from fastapi_viewsets.mux_ws import process_command


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001 - signature required by FastAPI
    if postgres_demo.ENABLED:
        import psycopg

        try:
            await asyncio.to_thread(postgres_demo.ensure_database, records)
        except psycopg.Error as error:
            # The in-memory data source still starts; the others report why they are off.
            postgres_demo.disable(f"the PostgreSQL server cannot be used: {error}")
        else:
            if postgres_demo.ADORM_ENABLED:
                postgres_demo.configure_adorm()
    else:
        # In a thread: Django refuses synchronous queries inside a running event loop.
        await sync_to_async(ensure_database, thread_sensitive=False)(records)
    await start_events()
    check_events()
    try:
        async with _result_readers():
            yield
    finally:
        await stop_events()
        if postgres_demo.ADORM_ENABLED:
            from adorm.settings import close_pools

            await close_pools()


@asynccontextmanager
async def _result_readers():
    """The readers of the Celery viewsets' results, for the lifespan."""
    prefixes = (MEMORY_TASK_PREFIX, DB_TASK_PREFIX) if USE_CELERY else ()
    bench = postgres_demo.ENABLED and benchmark.CELERY_ENABLED
    if not prefixes and not bench:
        yield
        return

    from demo.backend.celery_app import redis_async
    from fastapi_viewsets.decorators.celery_viewset import (
        check_result_readers,
        get_result_queue_key,
        start_result_reader,
        stop_result_reader,
    )

    for prefix in prefixes:
        await start_result_reader(redis_async, get_result_queue_key(prefix), 0.05)
    # Polled faster only during a run of their executor (POST /benchmark/runs).
    await start_bench_result_readers(READER_IDLE_POLL_S)
    check_result_readers()
    yield
    await stop_result_reader()


# Both doc pages are served by hand: FastAPI's built-ins have no hook for the extra <head> the
# "Try it in Swagger UI" link needs (see demo/backend/openapi_docs.py).
app = FastAPI(title="Demo ViewSet App", lifespan=lifespan, docs_url=None, redoc_url=None)
# the errors of the routes outside the viewsets, and of a path no route matches, in the viewsets' body
install_error_handlers(app)
# Counts the benchmark requests in progress, so a run starts after those of the previous one have ended.
app.add_middleware(BenchInFlightMiddleware)


def custom_openapi():
    if not app.openapi_schema:
        # Replacing app.openapi bypasses app.openapi_tags; without tags= the group descriptions vanish.
        app.openapi_schema = add_try_links(
            get_openapi(
                title=app.title,
                version=app.version,
                routes=app.routes,
                tags=app.openapi_tags,
            )
        )
    return app.openapi_schema


app.openapi = custom_openapi


@app.get("/docs", include_in_schema=False)
async def swagger_ui_page() -> HTMLResponse:
    html = get_swagger_ui_html(openapi_url="/openapi.json", title=f"{app.title} - Swagger UI")
    return HTMLResponse(html.body.decode().replace("</head>", SWAGGER_HEAD + "</head>"))


@app.get("/redoc", include_in_schema=False)
async def redoc_page() -> HTMLResponse:
    html = get_redoc_html(openapi_url="/openapi.json", title=f"{app.title} - ReDoc")
    return HTMLResponse(html.body.decode().replace("</head>", REDOC_HEAD + "</head>"))


router = APIRouter()

route_viewset(router, base_path="/music", pk_field_name="id")(MusicTrackViewSet)
# The same API over SQLite via the Django ORM, for comparing the two backends on the same data.
route_viewset(router, base_path="/music-db", pk_field_name="id")(MusicTrackDbViewSet)
if postgres_demo.ADORM_ENABLED:
    from demo.backend.adorm_viewsets import MusicTrackAdormViewSet

    # The same API on PostgreSQL through adorm, with the generated adorm model as its records.
    route_viewset(router, base_path="/music-adorm", pk_field_name="id")(MusicTrackAdormViewSet)
if postgres_demo.ENABLED:
    from demo.backend.benchmark.viewsets import VIEWSETS as BENCH_VIEWSETS

    # The benchmark's read-only viewsets, one per DB API and executor.
    for (db_api, executor), bench_viewset in BENCH_VIEWSETS.items():
        route_viewset(router, base_path=f"/bench/{db_api}/{executor}", pk_field_name="id")(bench_viewset)
# subscribe, unsubscribe and poll of the viewsets' topics, under /events
register_events(router, base_path="/events")


class DemoBackend(BaseModel):
    """A data source of the demo's music library."""

    name: str = Field(description="`memory`, `db` or `adorm`: the value the frontend selects it by.")
    label: str = Field(description="Its name for people: the database and the library that reads it.")
    base_path: str = Field(description="Where its viewset is routed.")
    available: bool = Field(description="Whether the viewset is routed.")
    reason: str | None = Field(None, description="Why it is not available; null when it is.")


@router.get("/backends", tags=["demo"], summary="Data sources of the demo")
async def backends() -> list[DemoBackend]:
    """
    The demo's three data sources. With a PostgreSQL server the Django ORM one reads PostgreSQL instead of SQLite, and
    the adorm one is available with adorm installed.
    """
    on_postgres = MusicTrackDbViewSet.model is not MusicTrackRow
    return [
        DemoBackend(name="memory", label="in-memory", base_path="/music", available=True),
        DemoBackend(
            name="db",
            label="PostgreSQL (Django ORM)" if on_postgres else "SQLite (Django ORM)",
            base_path="/music-db",
            available=not on_postgres or postgres_demo.ENABLED,
            reason=postgres_demo.UNAVAILABLE if on_postgres else None,
        ),
        DemoBackend(
            name="adorm",
            label="PostgreSQL (adorm)",
            base_path="/music-adorm",
            available=postgres_demo.ADORM_ENABLED,
            reason=postgres_demo.ADORM_UNAVAILABLE,
        ),
    ]


app.include_router(router)
app.include_router(benchmark_router)

# The viewsets' docstrings become the tag group descriptions; must run after they are decorated.
apply_viewset_tags(app)


# In GitHub Codespaces a forwarded port is served at https://<codespace name>-<port>.<forwarding domain>.
_CODESPACE_NAME = os.environ.get("CODESPACE_NAME")
_FORWARDING_DOMAIN = os.environ.get("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN")
_CODESPACE_HOST = (
    re.compile(rf"{re.escape(_CODESPACE_NAME)}-\d+\.{re.escape(_FORWARDING_DOMAIN)}")
    if _CODESPACE_NAME and _FORWARDING_DOMAIN
    else None
)


def _local_origin(origin: str | None) -> bool:
    """
    Admit a page from this machine (any port), from this codespace's forwarded ports, or a non-browser
    client without an Origin.
    """
    if origin is None:
        return True
    hostname = urlsplit(origin).hostname or ""
    return hostname in ("127.0.0.1", "localhost") or bool(_CODESPACE_HOST and _CODESPACE_HOST.fullmatch(hostname))


MUXWS_MAX_STREAMS = 2000


@app.websocket("/ws")
async def muxws_endpoint(websocket: WebSocket) -> None:
    """The same viewsets over one WebSocket; muxws accepts it, selecting the codec subprotocol."""
    # Origin check against cross-site WebSocket hijacking: other origins get HTTP 403.
    # Streams at a time per socket: the benchmark's load generator sends up to 1000 over one.
    peer = await accept(websocket, allowed_origins=_local_origin, max_concurrent_streams=MUXWS_MAX_STREAMS)

    async def on_stream(payload, stream: Stream) -> None:
        # peer= is the socket `subscribe` registers the subscription on.
        if await process_command(payload, stream, connection=websocket, peer=peer):
            return
        # Not a viewset command; an application would dispatch its own protocol here.
        await stream.reply({"error": "unknown command", "hint": "viewset commands carry :method and :path"})

    peer.on_stream(on_stream)
    await peer.serve()


def main():
    import uvicorn

    print("Starting demo application at http://127.0.0.1:8000")
    print("Documentation is available at http://127.0.0.1:8000/docs")
    print(f"muxws endpoint at ws://127.0.0.1:8000/ws (Celery {'on' if USE_CELERY else 'off'})")
    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
