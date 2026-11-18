"""
The benchmark processes' app: only the benchmark viewsets, at `/bench/{db_api}/{executor}` over REST and over the
muxws endpoint `/ws`, and their in-flight request count at `/bench-status`. `demo.py --db` runs it in
`--bench-processes` uvicorn processes; the UI and the `/benchmark` API stay in `demo.backend.main`.
"""

import os

from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, WebSocket
from muxws import accept, Stream
from pydantic import BaseModel, Field

from demo.backend import benchmark

# The UI's viewsets stay in-process here: they are not routed, so they need no Celery result readers.
os.environ.pop("DEMO_CELERY", None)

from demo.backend import postgres_demo  # noqa: E402
from fastapi_viewsets.decorators.route_viewset import route_viewset
from fastapi_viewsets.endpoint_docs import apply_viewset_tags
from fastapi_viewsets.error_responses import install_error_handlers
from fastapi_viewsets.mux_ws import process_command

BENCH_PATH_PREFIX = "/bench/"
MUXWS_MAX_STREAMS = 2000
"""Streams at a time per socket: a load generator process sends up to 1000 over one."""

_in_flight = 0
"""Requests to the benchmark viewsets, over REST or muxws, this process is serving."""


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001 - signature required by FastAPI
    if postgres_demo.ADORM_ENABLED:
        postgres_demo.configure_adorm()
    try:
        async with _result_readers():
            yield
    finally:
        if postgres_demo.ADORM_ENABLED:
            from adorm.settings import close_pools

            await close_pools()


@asynccontextmanager
async def _result_readers():
    """The readers of the benchmark Celery viewsets' results, for the lifespan."""
    if not (postgres_demo.ENABLED and benchmark.CELERY_ENABLED):
        yield
        return

    from demo.backend.benchmark.viewsets import task_prefix, VIEWSETS
    from demo.backend.celery_app import redis_async
    from fastapi_viewsets.decorators.celery_viewset import (
        check_result_readers,
        get_result_queue_key,
        start_result_reader,
        stop_result_reader,
    )

    for key in VIEWSETS:
        if key[1] in benchmark.CELERY_EXECUTORS:
            await start_result_reader(redis_async, get_result_queue_key(task_prefix(*key)))
    check_result_readers()
    try:
        yield
    finally:
        await stop_result_reader()


class BenchInFlightMiddleware:
    """ASGI middleware counting the REST requests to `/bench/...` in progress, including those whose client has gone."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith(BENCH_PATH_PREFIX):
            await self.app(scope, receive, send)
            return
        global _in_flight  # noqa: PLW0603 - one counter per process
        _in_flight += 1
        try:
            await self.app(scope, receive, send)
        finally:
            _in_flight -= 1


app = FastAPI(
    title="Demo benchmark process",
    description="The benchmark viewsets of the demo, served by one of the benchmark's uvicorn processes.",
    lifespan=lifespan,
)
install_error_handlers(app)
app.add_middleware(BenchInFlightMiddleware)

router = APIRouter()
if postgres_demo.ENABLED:
    from demo.backend.benchmark.viewsets import VIEWSETS

    for (db_api, executor), bench_viewset in VIEWSETS.items():
        route_viewset(router, base_path=f"/bench/{db_api}/{executor}", pk_field_name="id")(bench_viewset)


class BenchProcessStatus(BaseModel):
    """The load on one benchmark process."""

    pid: int = Field(description="The process id.")
    in_flight: int = Field(description="Requests to the benchmark viewsets, over REST or muxws, it is serving now.")


@router.get(
    "/bench-status",
    tags=["benchmark"],
    summary="Requests in progress",
    response_description="The process id and its benchmark requests in progress.",
)
async def bench_status() -> BenchProcessStatus:
    """The benchmark requests this process is serving, over REST or muxws, including those whose client has gone."""
    return BenchProcessStatus(pid=os.getpid(), in_flight=_in_flight)


app.include_router(router)
apply_viewset_tags(app)


def _no_origin(origin: str | None) -> bool:
    """Admits only clients without an Origin: the load generator, not browser pages."""
    return origin is None


@app.websocket("/ws")
async def muxws_endpoint(websocket: WebSocket) -> None:
    """The benchmark viewsets over one WebSocket per load generator process."""
    peer = await accept(websocket, allowed_origins=_no_origin, max_concurrent_streams=MUXWS_MAX_STREAMS)

    async def on_stream(payload, stream: Stream) -> None:
        global _in_flight  # noqa: PLW0603 - one counter per process
        _in_flight += 1
        try:
            if await process_command(payload, stream, connection=websocket, peer=peer):
                return
            await stream.reply({"error": "unknown command", "hint": "viewset commands carry :method and :path"})
        finally:
            _in_flight -= 1

    peer.on_stream(on_stream)
    await peer.serve()
