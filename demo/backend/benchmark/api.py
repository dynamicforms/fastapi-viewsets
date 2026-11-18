"""The benchmark's API: what can run (`GET /benchmark`) and one measured run (`POST /benchmark/runs`)."""

import asyncio
import json
import os
import signal
import socket
import sys

from contextlib import suppress
from typing import Literal

from fastapi import APIRouter
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field

from demo.backend import benchmark, postgres_demo
from demo.backend.benchmark.latency_proxy import latency_command
from demo.backend.viewsets import LIBRARY_SIZE
from fastapi_viewsets.exceptions import DfViewSetError
from fastapi_viewsets.response_classes import error_entry, merge_responses

Executor = Literal["fastapi", "classic", "hybrid"]
DbApi = Literal["django", "adorm"]
Transport = Literal["rest", "muxws"]
Operation = Literal["retrieve", "list"]

INSPECT_TIMEOUT_S = 1.0
BENCH_STATUS_TIMEOUT_S = 1.0
"""Seconds a benchmark process has to answer `GET /bench-status`."""
DRAIN_TIMEOUT_S = 30.0
LOADGEN_GRACE_S = 60.0
"""Seconds a load generator may run beyond its warm-up and duration (start-up of its processes, requests in flight)."""


class BenchmarkBusyError(DfViewSetError):
    """Another run is in progress."""

    def __init__(self):
        super().__init__(409, "A benchmark run is in progress; one runs at a time", "demo.benchmark_busy")


class BenchmarkUnavailableError(DfViewSetError):
    """The selected combination cannot run; `reason` says why."""

    def __init__(self, reason: str):
        super().__init__(503, f"The benchmark cannot run: {reason}", "demo.benchmark_unavailable", {"reason": reason})


class BenchmarkFailedError(DfViewSetError):
    """The load generator or the latency proxy failed; `reason` says how."""

    def __init__(self, reason: str):
        super().__init__(500, f"The benchmark run failed: {reason}", "demo.benchmark_failed", {"reason": reason})


class ExecutorStatus(BaseModel):
    """An executor of the benchmark viewsets and its processes."""

    name: Executor = Field(description="`fastapi`, `classic` or `hybrid`: the value a run selects it by.")
    label: str = Field(description="Its name for people, e.g. `Celery hybrid`.")
    available: bool = Field(description="Whether runs on it can start.")
    reason: str | None = Field(None, description="Why it is not available; null when it is.")
    description: str = Field(
        description="Its configured processes, e.g. `2 prefork + 2 async processes (aconcurrency 200)`."
    )
    processes: int = Field(description="Configured processes that run actions: prefork and async together.")
    prefork_processes: int = Field(description="Configured processes running one action at a time.")
    async_processes: int = Field(description="Configured processes running many actions at a time on an event loop.")
    async_concurrency: int | None = Field(
        None, description="Actions at a time over all async processes (`--aconcurrency`); null without them."
    )
    active_processes: int = Field(
        description="Processes the running worker reports (FastAPI: the benchmark's uvicorn processes that answer); "
        "0 when none answers."
    )
    active_prefork_processes: int = Field(description="Prefork processes of `active_processes`.")
    active_async_processes: int = Field(description="Async processes of `active_processes`.")


class BenchmarkStatus(BaseModel):
    """What the benchmark can run and with which settings."""

    available: bool = Field(description="Whether any run can start: the demo has a PostgreSQL server.")
    reason: str | None = Field(None, description="Why the benchmark is not available; null when it is.")
    running: bool = Field(description="Whether a run is in progress.")
    database: str | None = Field(description="The PostgreSQL server and database the benchmark reads.")
    db_latency_ms: float | None = Field(
        description="The round-trip time the latency proxy adds now, in ms; null without the proxy."
    )
    latency_proxy: bool = Field(description="Whether the benchmark's connections go through the latency proxy.")
    adorm_available: bool = Field(description="Whether the `adorm` DB API can run.")
    adorm_reason: str | None = Field(None, description="Why adorm cannot run; null when it can.")
    adorm_pool: int = Field(description="The adorm pool size (`max_connections`) of each process.")
    adorm_driver: str = Field(description="The adorm driver: `asyncpg` or `django`.")
    library_size: int = Field(description="Tracks in the table; retrieves spread their ids over 1..library_size.")
    bench_processes: int = Field(
        description="uvicorn processes serving the benchmark viewsets; the load is spread over them."
    )
    loadgen_processes: int = Field(description="Processes a run's load generator splits its virtual users over.")
    executors: list[ExecutorStatus] = Field(description="`fastapi`, `classic` and `hybrid`, in that order.")


class BenchmarkRunRequest(BaseModel):
    """One cell of the benchmark: a load of `concurrency` virtual users for `duration_s` seconds."""

    transport: Transport = Field(
        description="`rest` (HTTP, a connection per virtual user) or `muxws` (one WebSocket per load generator "
        "process)."
    )
    executor: Executor = Field(description="Who runs the viewset's actions.")
    db_api: DbApi = Field(description="`django` (Django ORM) or `adorm`.")
    operation: Operation = Field("retrieve", description="`retrieve` one track by id, or `list` the first 50.")
    concurrency: int = Field(ge=1, le=1000, description="Virtual users, each sending its next request on an answer.")
    duration_s: float = Field(5, ge=1, le=30, description="Measured seconds, after a 1 s warm-up that is not.")
    db_latency_ms: float | None = Field(
        None, ge=0, le=100, description="Round-trip time the proxy adds, in ms; null keeps the current one."
    )


class LatencyStats(BaseModel):
    """Latencies of the answered requests, in ms; null when none answered."""

    p50: float | None = Field(description="Median.")
    p95: float | None = Field(description="95th percentile.")
    p99: float | None = Field(description="99th percentile.")
    mean: float | None = Field(description="Mean.")
    max: float | None = Field(description="Maximum.")


class BenchmarkRunResult(BaseModel):
    """The measured result of a run, with the request it answers."""

    request: BenchmarkRunRequest = Field(description="The run's settings, `db_latency_ms` as applied.")
    completed: int = Field(
        description="Requests started within the measured window and answered without an error, during the drain too."
    )
    errors: int = Field(description="Requests started within the measured window that failed, during the drain too.")
    error_rate: float = Field(description="`errors` / (`completed` + `errors`), 0 to 1.")
    error_kinds: dict[str, int] = Field(
        description="Failures by kind: `timeout` (10 s), `HTTP <status>` or the exception's class name."
    )
    throughput: float = Field(
        description="Requests answered without an error within the measured window, per second of `measured_s`."
    )
    latency_ms: LatencyStats = Field(description="Latencies of the `completed` requests.")
    measured_s: float = Field(description="Length of the measured window, in seconds.")
    drain_s: float = Field(description="Seconds the load generator waited after the window for requests in flight.")
    loadgen_cpu_s: float = Field(
        description="CPU seconds of the load generator from the start of the warm-up, summed over its processes."
    )
    loadgen_cpu_max_s: float = Field(
        description="CPU seconds of its busiest process; near `loadgen_active_s` that process saturates a core and "
        "caps the result."
    )
    loadgen_active_s: float = Field(
        description="Wall seconds from the start of the warm-up until the last load generator process finished."
    )
    loadgen_processes: int = Field(
        description="Processes the load generator ran: `loadgen_processes` of `GET /benchmark`, at most one per "
        "virtual user."
    )
    started: str = Field(description="When the load generator started (ISO 8601, UTC).")
    finished: str = Field(description="When it finished (ISO 8601, UTC).")


router = APIRouter(prefix="/benchmark", tags=["benchmark"])
_run_lock = asyncio.Lock()


def _hostname() -> str:
    return socket.gethostname()


def _node(executor: str) -> str:
    return f"{executor}@{_hostname()}"


async def _worker_stats() -> dict[str, dict]:
    """`inspect().stats()` of the benchmark workers that answer within a second; empty without Celery."""
    if not benchmark.CELERY_ENABLED:
        return {}
    from demo.backend.celery_app import celery_app

    nodes = [_node(executor) for executor in benchmark.CELERY_EXECUTORS]
    inspector = celery_app.control.inspect(timeout=INSPECT_TIMEOUT_S, destination=nodes, limit=len(nodes))
    try:
        return await asyncio.to_thread(inspector.stats) or {}
    except Exception:  # noqa: BLE001 - an unreachable broker is reported as no active workers
        return {}


def _executor_status(name: str, stats: dict[str, dict], bench: list[dict | None]) -> ExecutorStatus:
    unavailable = _executor_unavailable(name) or _bench_unavailable(bench)
    if name == "fastapi":
        processes = benchmark.BENCH_PROCESSES
        active = sum(status is not None for status in bench)
        return ExecutorStatus(
            name="fastapi",
            label="FastAPI in-process",
            available=unavailable is None,
            reason=unavailable,
            description=f"{processes} uvicorn process{'' if processes == 1 else 'es'}",
            processes=processes,
            prefork_processes=0,
            async_processes=processes,
            active_processes=active,
            active_prefork_processes=0,
            active_async_processes=active,
        )
    pool = stats.get(_node(name), {}).get("pool", {})
    active_prefork = len(pool.get("processes") or [])
    active_async = len(pool.get("async-processes") or [])
    if name == "classic":
        prefork, asynchronous, concurrency = benchmark.CLASSIC_PROCESSES, 0, None
        description = f"{prefork} prefork processes"
    else:
        prefork, asynchronous = benchmark.HYBRID_PROCESSES, benchmark.HYBRID_ASYNC_PROCESSES
        concurrency = benchmark.HYBRID_ASYNC_CONCURRENCY
        description = f"{prefork} prefork + {asynchronous} async processes (aconcurrency {concurrency})"
    if unavailable is None:
        unavailable = _celery_unavailable(name, stats)
    return ExecutorStatus(
        name=name,
        label="Celery classic" if name == "classic" else "Celery hybrid",
        available=unavailable is None,
        reason=unavailable,
        description=description,
        processes=prefork + asynchronous,
        prefork_processes=prefork,
        async_processes=asynchronous,
        async_concurrency=concurrency,
        active_processes=active_prefork + active_async,
        active_prefork_processes=active_prefork,
        active_async_processes=active_async,
    )


async def _bench_statuses() -> list[dict | None]:
    """`GET /bench-status` of each benchmark process, None for one that does not answer within a second."""
    if not postgres_demo.ENABLED:
        return [None] * benchmark.BENCH_PROCESSES
    import httpx2 as httpx

    async with httpx.AsyncClient(timeout=BENCH_STATUS_TIMEOUT_S) as client:

        async def status(url: str) -> dict | None:
            try:
                response = await client.get(f"{url}/bench-status")
                response.raise_for_status()
                return response.json()
            except Exception:  # noqa: BLE001 - a process that does not answer is reported as inactive
                return None

        return list(await asyncio.gather(*(status(url) for url in benchmark.bench_urls())))


def _bench_unavailable(bench: list[dict | None]) -> str | None:
    """Why runs cannot start because of the benchmark processes; None when every one answers."""
    missing = [url for url, status in zip(benchmark.bench_urls(), bench, strict=True) if status is None]
    if not missing:
        return None
    return f"benchmark processes do not answer at {', '.join(missing)} (demo.py --db starts them)"


def _celery_unavailable(executor: str, stats: dict[str, dict]) -> str | None:
    """Why the Celery worker of `executor` cannot run actions, by its `inspect().stats()`; None when it can."""
    if not stats.get(_node(executor), {}).get("pool"):
        return f"the worker {_node(executor)} does not answer"
    return None


def _unavailable() -> str | None:
    if not postgres_demo.ENABLED:
        return f"PostgreSQL is off: {postgres_demo.UNAVAILABLE}"
    return None


def _executor_unavailable(executor: str) -> str | None:
    reason = _unavailable()
    if reason is not None:
        return reason
    if executor != "fastapi" and not benchmark.CELERY_ENABLED:
        return "the benchmark's Celery workers run with demo.py --benchmark (needs Redis)"
    return None


def _run_unavailable(executor: str, db_api: str) -> str | None:
    reason = _executor_unavailable(executor)
    if reason is not None:
        return reason
    from demo.backend.benchmark.viewsets import unavailable

    return unavailable(db_api, executor)


async def _current_latency() -> float | None:
    if not benchmark.CONTROL_PORT:
        return None
    try:
        return float(await latency_command(int(benchmark.CONTROL_PORT), "GET"))
    except (OSError, RuntimeError, asyncio.TimeoutError, ValueError):
        return None


def _database() -> str | None:
    if not postgres_demo.ENABLED:
        return None
    entry = postgres_demo.django_database()
    return f"PostgreSQL at {entry['HOST']}:{entry['PORT'] or 5432}, database {entry['NAME']}"


UNAVAILABLE_RESPONSE = error_entry(
    503,
    "The selected executor or DB API cannot run (no PostgreSQL server, a benchmark process or worker does not "
    "answer, adorm not installed): "
    "`demo.benchmark_unavailable`, the reason in `detail_params.reason`.",
    BenchmarkUnavailableError("the benchmark's Celery workers run with demo.py --benchmark (needs Redis)"),
)
BUSY_RESPONSE = error_entry(409, "Another run is in progress: `demo.benchmark_busy`.", BenchmarkBusyError())
FAILED_RESPONSE = error_entry(
    500,
    "The load generator or the latency proxy failed: `demo.benchmark_failed`, the reason in `detail_params.reason`.",
    BenchmarkFailedError("the load generator exited with status 1"),
)
VALIDATION_RESPONSE = error_entry(
    422,
    "The body is invalid (an unknown executor, concurrency outside 1-1000, duration outside 1-30 s, latency "
    "outside 0-100 ms): `fastapi-viewsets.validation_failed` with `field_errors`.",
    RequestValidationError([]),
)


@router.get(
    "",
    summary="What the benchmark can run",
    response_description="Availability, settings and the executors with their configured and active processes.",
)
async def benchmark_status() -> BenchmarkStatus:
    """
    Whether the benchmark can run and why not; the database, the latency the proxy adds now, the adorm pool, the
    benchmark and load generator process counts, and per executor its configured processes and the processes that
    answer: its Celery worker's, or the benchmark's uvicorn processes for FastAPI (asked with a 1 s timeout).
    """
    stats, bench = await asyncio.gather(_worker_stats(), _bench_statuses())
    return BenchmarkStatus(
        available=_unavailable() is None,
        reason=_unavailable(),
        running=_run_lock.locked(),
        database=_database(),
        db_latency_ms=await _current_latency(),
        latency_proxy=bool(benchmark.CONTROL_PORT) and bool(postgres_demo.BENCH_DB_PORT),
        adorm_available=postgres_demo.ADORM_ENABLED,
        adorm_reason=postgres_demo.ADORM_UNAVAILABLE,
        adorm_pool=postgres_demo.ADORM_POOL,
        adorm_driver=postgres_demo.DRIVER,
        library_size=LIBRARY_SIZE,
        bench_processes=benchmark.BENCH_PROCESSES,
        loadgen_processes=benchmark.LOADGEN_PROCESSES,
        executors=[_executor_status(name, stats, bench) for name in benchmark.EXECUTORS],
    )


@router.post(
    "/runs",
    summary="Run one benchmark cell",
    response_description="Throughput, latency percentiles and errors of the measured window.",
    responses=merge_responses(UNAVAILABLE_RESPONSE, BUSY_RESPONSE, FAILED_RESPONSE, VALIDATION_RESPONSE),
)
async def run_benchmark(body: BenchmarkRunRequest) -> BenchmarkRunResult:
    """
    Sets the proxy's latency, drains what earlier runs left in the benchmark's Celery queues and workers and in the
    benchmark processes' requests (up to 30 s), then runs the load generator in processes of its own against the
    benchmark processes, at `/bench/{db_api}/{executor}` over `transport`, and answers its result. One run at a time.
    """
    reason = _run_unavailable(body.executor, body.db_api) or _bench_unavailable(await _bench_statuses())
    if reason is not None:
        raise BenchmarkUnavailableError(reason)
    if body.executor != "fastapi":
        reason = _celery_unavailable(body.executor, await _worker_stats())
        if reason is not None:
            raise BenchmarkUnavailableError(reason)
    if _run_lock.locked():
        raise BenchmarkBusyError()
    async with _run_lock:
        latency = await _apply_latency(body.db_latency_ms)
        await _drain(body.executor)
        result = await _run_loadgen(body)
    return BenchmarkRunResult(request=body.model_copy(update={"db_latency_ms": latency}), **result)


async def _apply_latency(latency_ms: float | None) -> float | None:
    if not benchmark.CONTROL_PORT:
        if latency_ms:
            raise BenchmarkUnavailableError("no latency proxy runs, so the latency stays 0 ms")
        return None
    line = "GET" if latency_ms is None else f"SET {latency_ms:g}"
    try:
        return float(await latency_command(int(benchmark.CONTROL_PORT), line))
    except (OSError, RuntimeError, asyncio.TimeoutError, ValueError) as error:
        raise BenchmarkFailedError(f"the latency proxy does not answer: {error!r}") from error


async def _drain(executor: str) -> None:
    """
    Purges the benchmark queues, waits until their workers hold no task and until no benchmark process serves a
    request, up to `DRAIN_TIMEOUT_S` in all.
    """
    deadline = asyncio.get_running_loop().time() + DRAIN_TIMEOUT_S
    if benchmark.CELERY_ENABLED:
        await _drain_celery(executor, deadline)
    while asyncio.get_running_loop().time() < deadline:
        if not sum(status["in_flight"] for status in await _bench_statuses() if status is not None):
            return
        await asyncio.sleep(0.05)


async def _drain_celery(executor: str, deadline: float) -> None:
    from demo.backend.celery_app import celery_app

    def purge() -> None:
        with celery_app.connection_for_write() as connection:
            channel = connection.default_channel
            for queue in benchmark.QUEUES.values():
                channel.queue_purge(queue)

    nodes = [_node(executor) for executor in benchmark.CELERY_EXECUTORS]

    def busy() -> bool:
        inspector = celery_app.control.inspect(timeout=INSPECT_TIMEOUT_S, destination=nodes, limit=len(nodes))
        held = [*(inspector.active() or {}).values(), *(inspector.reserved() or {}).values()]
        return any(held)

    try:
        await asyncio.to_thread(purge)
        while await asyncio.to_thread(busy) and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.2)
    except Exception as error:  # noqa: BLE001 - an unreachable broker leaves nothing to drain for this run
        if executor != "fastapi":
            raise BenchmarkFailedError(f"the Celery broker does not answer: {error!r}") from error


async def _kill_group(process: asyncio.subprocess.Process) -> None:
    """Kills the load generator and its child processes."""
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    await process.wait()


async def _run_loadgen(body: BenchmarkRunRequest) -> dict:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "demo.backend.benchmark.loadgen",
        f"--base-url={','.join(benchmark.bench_urls())}",
        f"--processes={benchmark.LOADGEN_PROCESSES}",
        f"--path=/bench/{body.db_api}/{body.executor}",
        f"--transport={body.transport}",
        f"--operation={body.operation}",
        f"--concurrency={body.concurrency}",
        f"--duration={body.duration_s:g}",
        f"--ids={LIBRARY_SIZE}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # its own process group, so a kill reaches its child processes too
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), body.duration_s + LOADGEN_GRACE_S)
    except asyncio.TimeoutError:
        await _kill_group(process)
        raise BenchmarkFailedError("the load generator did not finish in time") from None
    except asyncio.CancelledError:
        await _kill_group(process)
        raise
    if process.returncode != 0:
        tail = stderr.decode(errors="replace").strip().splitlines()[-1:] or [""]
        raise BenchmarkFailedError(f"the load generator exited with status {process.returncode}: {tail[0]}")
    return json.loads(stdout)
