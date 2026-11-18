"""
The server-side benchmark: the same read-only viewset on the Django ORM and on adorm, run by FastAPI in-process, by a
classic Celery worker and by a Celery worker with async processes, under load from a separate process.

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
