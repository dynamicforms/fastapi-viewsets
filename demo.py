"""
Runs the demo: FastAPI, the Vue dev server, and optionally Celery workers and the server-side benchmark.

    uv run python demo.py              # backend + frontend
    uv run python demo.py --celery     # ... and every viewset call routed through a Celery worker
    uv run python demo.py --db postgres://user:password@localhost:5432/
                                       # ... and the library on PostgreSQL, through the Django ORM and adorm,
                                       # and the benchmark's own uvicorn processes (--bench-processes, default 2)
    uv run python demo.py --db postgres://user:password@localhost:5432/ --benchmark
                                       # ... and the benchmark's Celery workers (classic and hybrid)

Celery is off by default: its queueing latency would dominate the HTTP vs WebSocket benchmark.
Ctrl-C stops the children one by one; each runs in its own session and is stopped by process group.
The API listens on $DEMO_API_PORT (8000), the frontend on $DEMO_FE_PORT (5173), the benchmark processes on
$DEMO_BENCH_PORT and up (default $DEMO_API_PORT + 10). The load generator runs in $DEMO_LOADGEN_PROCESSES (4) processes.
"""

import argparse
import os
import resource
import signal
import socket
import subprocess
import sys
import time
import urllib.request

from contextlib import suppress
from urllib.error import URLError
from urllib.parse import urlsplit

from demo.backend import benchmark

STOP_GRACE_SECONDS = 5
BENCH_READY_TIMEOUT_SECONDS = 60


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


def wait_until_serving(process: subprocess.Popen, url: str, timeout: float) -> bool:
    """Polls `url` until it answers, `process` exits or `timeout` seconds pass; True when it answered."""
    deadline = time.monotonic() + timeout
    while process.poll() is None and time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1):  # noqa: S310 - a loopback http URL built here
                return True
        except (URLError, OSError):
            time.sleep(0.2)
    return False


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
        "--bench-processes",
        metavar="N",
        type=int,
        default=int(os.environ.get("DEMO_BENCH_PROCESSES") or 2),
        help="uvicorn processes serving the benchmark viewsets (with --db), on ports $DEMO_BENCH_PORT and up "
        "(default $DEMO_API_PORT + 10); default: $DEMO_BENCH_PROCESSES or 2",
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
    if args.bench_processes < 1:
        parser.error("--bench-processes must be at least 1")

    api_port = os.environ.get("DEMO_API_PORT", "8000")
    fe_port = os.environ.get("DEMO_FE_PORT", "5173")
    bench_port = int(os.environ.get("DEMO_BENCH_PORT") or int(api_port) + 10)
    # demo.backend reads them at import time
    env = {
        "DEMO_DATABASE_URL": args.db,
        "DEMO_ADORM_DRIVER": args.adorm_driver,
        "DEMO_API_PORT": api_port,
        "DEMO_FE_PORT": fe_port,
        "DEMO_BENCH_PORT": str(bench_port),
        "DEMO_BENCH_PROCESSES": str(args.bench_processes),
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

    bench_processes = []
    if args.db:
        for index in range(args.bench_processes):
            port = bench_port + index
            process = spawn(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "demo.backend.benchmark.app:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--no-access-log",
                ],
                env,
            )
            children.append((f"benchmark process {index + 1}", process))
            bench_processes.append((process, f"http://127.0.0.1:{port}/bench-status"))

    print(f"FastAPI docs:  http://127.0.0.1:{api_port}/docs")
    print(f"API reference: http://127.0.0.1:{api_port}/redoc")
    print(f"Celery:        {'on' if args.celery else 'off (--celery to enable)'}")
    print(f"PostgreSQL:    {'on, adorm driver ' + args.adorm_driver if args.db else 'off (--db to enable)'}")
    if not args.db:
        print("Benchmark:     off (needs --db)", flush=True)
    elif args.benchmark:
        print(
            f"Benchmark:     on, FastAPI + Celery classic + Celery hybrid, DB latency {args.db_latency:g} ms, "
            f"{args.bench_processes} uvicorn processes from port {bench_port}",
            flush=True,
        )
    else:
        print(
            f"Benchmark:     FastAPI only, DB latency {args.db_latency:g} ms, {args.bench_processes} uvicorn processes "
            f"from port {bench_port} (--benchmark for the Celery executors)",
            flush=True,
        )

    for received in (signal.SIGINT, signal.SIGTERM):
        signal.signal(received, _interrupt)

    try:
        # The main backend starts once the benchmark processes serve, so its readiness covers theirs.
        for process, url in bench_processes:
            if not wait_until_serving(process, url, BENCH_READY_TIMEOUT_SECONDS):
                print(f"The benchmark process at {url} does not answer; the benchmark reports it", flush=True)
    except KeyboardInterrupt:
        for label, process in children:
            stop(process, label)
        print("Stopped.", flush=True)
        return 0

    backend = spawn(
        [sys.executable, "-m", "uvicorn", "demo.backend.main:app", "--host", "127.0.0.1", "--port", api_port],
        env,
    )
    children.insert(0, ("the backend", backend))

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
