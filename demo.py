"""
Runs the demo: FastAPI, the Vue dev server, and optionally Celery workers and the server-side benchmark.

    uv run python demo.py              # backend + frontend
    uv run python demo.py --celery     # ... and every viewset call routed through a Celery worker
    uv run python demo.py --db postgres://user:password@localhost:5432/
                                       # ... and the library on PostgreSQL, through the Django ORM and adorm
    uv run python demo.py --db postgres://user:password@localhost:5432/ --benchmark
                                       # ... and the benchmark's Celery workers (classic and hybrid)

Celery is off by default: its queueing latency would dominate the HTTP vs WebSocket benchmark.
Ctrl-C stops the children one by one; each runs in its own session and is stopped by process group.
The API listens on $DEMO_API_PORT (8000), the frontend on $DEMO_FE_PORT (5173).
"""

import argparse
import os
import resource
import signal
import socket
import subprocess
import sys

from contextlib import suppress
from urllib.parse import urlsplit

from demo.backend import benchmark

STOP_GRACE_SECONDS = 5


def spawn(command: list[str], env: dict[str, str] | None = None) -> subprocess.Popen:
    """Start a child in its own session, so Ctrl-C reaches it only through this process."""
    return subprocess.Popen(command, env={**os.environ, **(env or {})}, start_new_session=True)  # noqa: S603


def stop(process: subprocess.Popen, label: str) -> None:
    """
    SIGTERM a child's process group, SIGKILL after the grace period. The group, because `npm run` is a
    shell whose child holds the port.
    """
    if process.poll() is not None:
        return
    print(f"Stopping {label}...", flush=True)
    with suppress(ProcessLookupError):
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    try:
        process.wait(timeout=STOP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        print(f"{label} did not stop in {STOP_GRACE_SECONDS}s; killing it", flush=True)
        with suppress(ProcessLookupError):
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        process.wait()


def _interrupt(*_args) -> None:
    """
    Turn SIGINT/SIGTERM into KeyboardInterrupt. SIGTERM would otherwise kill this process and leave the
    children running; a background process inherits SIGINT as ignored.
    """
    raise KeyboardInterrupt


def free_port() -> int:
    """A TCP port on the loopback that is free now."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def raise_open_files_limit() -> None:
    """Raises the soft limit of open files to the hard limit, for 1000 concurrent sockets of the benchmark."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft < hard:
        resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--celery",
        action="store_true",
        help="route viewset calls through a Celery worker (needs Redis at DEMO_REDIS_URL, default redis://localhost:6379/0)",
    )
    parser.add_argument(
        "--db",
        metavar="URL",
        default=os.environ.get("DEMO_DATABASE_URL", ""),
        help="PostgreSQL server of the Django ORM and adorm data sources and of the benchmark; "
        "default: $DEMO_DATABASE_URL",
    )
    parser.add_argument(
        "--adorm-driver",
        choices=("asyncpg", "django"),
        default=os.environ.get("DEMO_ADORM_DRIVER", "asyncpg"),
        help="the adorm driver of the adorm viewsets",
    )
    parser.add_argument(
        "--benchmark",
        action="store_true",
        help="start the benchmark's Celery workers, classic and hybrid (needs --db and Redis at DEMO_REDIS_URL)",
    )
    parser.add_argument(
        "--no-frontend",
        action="store_true",
        help="start no Vue dev server (the e2e run starts its own)",
    )
    parser.add_argument(
        "--db-latency",
        metavar="MS",
        type=float,
        default=benchmark.DEFAULT_DB_LATENCY_MS,
        help="round-trip time the latency proxy adds to the benchmark's database connections at start, in ms",
    )
    args = parser.parse_args()
    if args.benchmark and not args.db:
        parser.error("--benchmark needs --db (or DEMO_DATABASE_URL)")

    api_port = os.environ.get("DEMO_API_PORT", "8000")
    fe_port = os.environ.get("DEMO_FE_PORT", "5173")
    # demo.backend reads them at import time
    env = {
        "DEMO_DATABASE_URL": args.db,
        "DEMO_ADORM_DRIVER": args.adorm_driver,
        "DEMO_API_PORT": api_port,
        "DEMO_FE_PORT": fe_port,
    }
    if args.celery:
        env["DEMO_CELERY"] = "1"
    raise_open_files_limit()

    children: list[tuple[str, subprocess.Popen]] = []
    if not args.no_frontend:
        children.append(("the frontend", spawn(["npm", "run", "demo:dev"], env)))  # noqa: S607
    if args.db:
        # Every benchmark database connection goes through the proxy; the UI's data sources connect directly.
        upstream = urlsplit(args.db)
        env["DEMO_BENCH_DB_PORT"] = str(free_port())
        env["DEMO_BENCH_CONTROL_PORT"] = str(free_port())
        children.append(
            (
                "the latency proxy",
                spawn(
                    [
                        sys.executable,
                        "-m",
                        "demo.backend.benchmark.latency_proxy",
                        f"--listen={env['DEMO_BENCH_DB_PORT']}",
                        f"--control={env['DEMO_BENCH_CONTROL_PORT']}",
                        f"--upstream={upstream.hostname or '127.0.0.1'}:{upstream.port or 5432}",
                        f"--latency={args.db_latency:g}",
                    ],
                    env,
                ),
            )
        )
    if args.benchmark:
        env["DEMO_BENCHMARK"] = "1"
        for executor in benchmark.CELERY_EXECUTORS:
            children.append((f"the {executor} benchmark worker", spawn(benchmark.worker_command(executor), env)))
    if args.celery:
        children.append(
            (
                "the Celery worker",
                # One process: the in-memory library is a dict per process (see CollectionViewSet).
                spawn(
                    ["celery", "-A", "demo.backend.celery_worker", "worker", "--loglevel=info", "--concurrency=1"],  # noqa: S607
                    env,
                ),
            )
        )

    print(f"FastAPI docs:  http://127.0.0.1:{api_port}/docs")
    print(f"API reference: http://127.0.0.1:{api_port}/redoc")
    print(f"Celery:        {'on' if args.celery else 'off (--celery to enable)'}")
    print(f"PostgreSQL:    {'on, adorm driver ' + args.adorm_driver if args.db else 'off (--db to enable)'}")
    if not args.db:
        print("Benchmark:     off (needs --db)", flush=True)
    elif args.benchmark:
        print(
            f"Benchmark:     on, FastAPI + Celery classic + Celery hybrid, DB latency {args.db_latency:g} ms",
            flush=True,
        )
    else:
        print(
            f"Benchmark:     FastAPI only, DB latency {args.db_latency:g} ms (--benchmark for the Celery executors)",
            flush=True,
        )

    backend = spawn(
        [sys.executable, "-m", "uvicorn", "demo.backend.main:app", "--host", "127.0.0.1", "--port", api_port],
        env,
    )
    children.insert(0, ("the backend", backend))

    for received in (signal.SIGINT, signal.SIGTERM):
        signal.signal(received, _interrupt)

    try:
        backend.wait()
    except KeyboardInterrupt:
        print()  # past the echoed ^C
    finally:
        for label, process in children:
            stop(process, label)
        print("Stopped.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
