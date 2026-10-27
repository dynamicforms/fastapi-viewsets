"""
Runs the demo: FastAPI, the Vue dev server, and optionally a Celery worker.

    python demo.py              # backend + frontend
    python demo.py --celery     # ... and every viewset call routed through a Celery worker

Celery is off by default: its queueing latency would dominate the HTTP vs WebSocket benchmark.
Ctrl-C stops the children one by one; each runs in its own session and is stopped by process group.
"""

import argparse
import os
import signal
import subprocess
import sys

from contextlib import suppress

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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--celery",
        action="store_true",
        help="route viewset calls through a Celery worker (needs Redis on localhost:6379)",
    )
    args = parser.parse_args()

    # demo.backend.viewsets reads it at import time
    env = {"DEMO_CELERY": "1"} if args.celery else {}

    children: list[tuple[str, subprocess.Popen]] = [
        ("the frontend", spawn(["npm", "run", "demo:dev"], env)),  # noqa: S607
    ]
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

    print("FastAPI docs:  http://127.0.0.1:8000/docs")
    print("API reference: http://127.0.0.1:8000/redoc")
    print(f"Celery:        {'on' if args.celery else 'off (--celery to enable)'}")

    backend = spawn(
        [sys.executable, "-m", "uvicorn", "demo.backend.main:app", "--host", "127.0.0.1", "--port", "8000"],
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
