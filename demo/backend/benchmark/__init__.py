"""
The server-side benchmark: the same read-only viewset on the Django ORM and on adorm, run by FastAPI in-process, by a
classic Celery worker and by a Celery worker with async processes, served by uvicorn processes of their own
(`demo.backend.benchmark.app`) under load from separate processes (`demo.backend.benchmark.loadgen`).

This module holds the configuration that `demo.py` shares with the web process; it imports nothing of the demo.
"""

import os

EXECUTORS = ("fastapi", "classic", "hybrid")
DB_APIS = ("django", "adorm")
TRANSPORTS = ("rest", "muxws")
OPERATIONS = ("retrieve", "list")

CLASSIC_PROCESSES = 2
"""Prefork processes of the classic worker."""
HYBRID_PROCESSES = 2
"""Prefork processes of the hybrid worker."""
HYBRID_ASYNC_PROCESSES = 2
"""Async processes of the hybrid worker."""
HYBRID_ASYNC_CONCURRENCY = 200
"""The hybrid worker's `--aconcurrency`: async executions at a time, shared by its async processes."""

CELERY_EXECUTORS = ("classic", "hybrid")
QUEUES = {executor: f"bench-{executor}" for executor in CELERY_EXECUTORS}
"""The queue of each benchmark Celery executor; the UI's viewsets use the default queue."""

DEFAULT_DB_LATENCY_MS = 10

CELERY_ENABLED = os.environ.get("DEMO_BENCHMARK", "").lower() in ("1", "true", "yes")
"""Whether `demo.py --benchmark` started the benchmark's Celery workers."""

CONTROL_PORT = os.environ.get("DEMO_BENCH_CONTROL_PORT", "")
"""The latency proxy's control port; empty when no proxy runs."""

BENCH_PROCESSES = max(1, int(os.environ.get("DEMO_BENCH_PROCESSES") or 2))
"""uvicorn processes serving the benchmark viewsets (`demo.backend.benchmark.app`)."""
BENCH_PORT = int(os.environ.get("DEMO_BENCH_PORT") or int(os.environ.get("DEMO_API_PORT") or 8000) + 10)
"""The port of the first benchmark process; process `i` listens on `BENCH_PORT + i`."""
LOADGEN_PROCESSES = max(1, int(os.environ.get("DEMO_LOADGEN_PROCESSES") or 4))
"""Processes the load generator splits its virtual users over."""


def bench_urls() -> list[str]:
    """The base URLs of the benchmark processes, in port order."""
    return [f"http://127.0.0.1:{BENCH_PORT + index}" for index in range(BENCH_PROCESSES)]


def worker_command(executor: str) -> list[str]:
    """The command line of the benchmark Celery worker of `executor` (`classic` or `hybrid`)."""
    command = ["celery", "-A", "demo.backend.benchmark.worker", "worker", "--loglevel=warning", "-Q", QUEUES[executor]]
    if executor == "classic":
        return [*command, f"--concurrency={CLASSIC_PROCESSES}", "-n", "classic@%h"]
    return [
        *command,
        f"--concurrency={HYBRID_PROCESSES}",
        f"--aconcurrency={HYBRID_ASYNC_CONCURRENCY}",
        f"--aprocesses={HYBRID_ASYNC_PROCESSES}",
        "-n",
        "hybrid@%h",
    ]
