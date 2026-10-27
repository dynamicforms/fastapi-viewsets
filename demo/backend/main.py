from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from asgiref.sync import sync_to_async
from fastapi import APIRouter, FastAPI, WebSocket
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse
from muxws import accept, Stream

from demo.backend.django_setup import ensure_database
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
    # In a thread: Django refuses synchronous queries inside a running event loop.
    await sync_to_async(ensure_database, thread_sensitive=False)(records)
    await start_events()
    check_events()

    if not USE_CELERY:
        yield
        await stop_events()
        return

    from demo.backend.celery_app import redis_async
    from fastapi_viewsets.decorators.celery_viewset import (
        check_result_readers,
        get_result_queue_key,
        start_result_reader,
        stop_result_reader,
    )

    for task_prefix in (MEMORY_TASK_PREFIX, DB_TASK_PREFIX):
        await start_result_reader(redis_async, get_result_queue_key(task_prefix))
    check_result_readers()
    yield
    await stop_result_reader()
    await stop_events()


# Both doc pages are served by hand: FastAPI's built-ins have no hook for the extra <head> the
# "Try it in Swagger UI" link needs (see demo/backend/openapi_docs.py).
app = FastAPI(title="Demo ViewSet App", lifespan=lifespan, docs_url=None, redoc_url=None)
# the errors of the routes outside the viewsets, and of a path no route matches, in the viewsets' body
install_error_handlers(app)


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
# subscribe, unsubscribe and poll of the viewsets' topics, under /events
register_events(router, base_path="/events")

app.include_router(router)

# The viewsets' docstrings become the tag group descriptions; must run after they are decorated.
apply_viewset_tags(app)


def _local_origin(origin: str | None) -> bool:
    """Admit a page from this machine (any port) or a non-browser client without an Origin."""
    return origin is None or urlsplit(origin).hostname in ("127.0.0.1", "localhost")


@app.websocket("/ws")
async def muxws_endpoint(websocket: WebSocket) -> None:
    """The same viewsets over one WebSocket; muxws accepts it, selecting the codec subprotocol."""
    # Origin check against cross-site WebSocket hijacking: other origins get HTTP 403.
    peer = await accept(websocket, allowed_origins=_local_origin)

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
