from contextlib import asynccontextmanager

from asgiref.sync import sync_to_async
from fastapi import APIRouter, FastAPI, WebSocket
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse
from muxws import accept, Stream

from demo.backend.django_setup import ensure_database
from demo.backend.openapi_docs import add_try_links, REDOC_HEAD, SWAGGER_HEAD
from demo.backend.viewsets import MusicTrackDbViewSet, MusicTrackViewSet, records, USE_CELERY
from fastapi_viewsets.decorators.route_viewset import route_viewset
from fastapi_viewsets.mux_ws import process_command


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001 - signature required by FastAPI
    # In a thread, because Django refuses a synchronous query from inside a running event loop -
    # and the lifespan is always inside one. Doing it at import time only appeared to work: it
    # depended on the app being imported before the loop started, which is true for
    # `uvicorn.run(app)` and false for `uvicorn.run("demo.backend.main:app")`.
    await sync_to_async(ensure_database, thread_sensitive=False)(records)

    if not USE_CELERY:
        yield
        return

    from demo.backend.celery_app import redis_async
    from fastapi_viewsets.decorators.celery_viewset import (
        get_result_queue_key,
        start_result_reader,
        stop_result_reader,
    )

    await start_result_reader(redis_async, get_result_queue_key("music"))
    yield
    await stop_result_reader()


# Both doc pages are served by hand: FastAPI's built-ins have no hook for the extra <head> the
# "Try it in Swagger UI" link needs (see demo/backend/openapi_docs.py).
app = FastAPI(title="Demo ViewSet App", lifespan=lifespan, docs_url=None, redoc_url=None)


def custom_openapi():
    if not app.openapi_schema:
        app.openapi_schema = add_try_links(get_openapi(
            title=app.title, version=app.version, routes=app.routes,
        ))
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
# The same API over SQLite via the Django ORM, so the two backends can be compared
# side by side on the same data - see MusicTrackDbViewSet.
route_viewset(router, base_path="/music-db", pk_field_name="id")(MusicTrackDbViewSet)

app.include_router(router)


@app.websocket("/ws")
async def muxws_endpoint(websocket: WebSocket) -> None:
    """
    The same viewsets, over one WebSocket.

    Note that websocket.accept() is never called here: muxws performs the upgrade itself, because
    only it knows which muxws.v1.<codec> subprotocol to select.
    """
    peer = await accept(websocket)

    async def on_stream(payload, stream: Stream) -> None:
        if await process_command(payload, stream, connection=websocket):
            return
        # Not a viewset command. A real application would dispatch its own protocol here; this
        # demo has none, so saying so is more useful than silence.
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
    # Without this `python -m demo.backend.main` imports the module, defines main(), and exits -
    # leaving nothing listening, which the dev server reports as a 502 and muxws as a socket that
    # never opened.
    main()
