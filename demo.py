"""
Runs the demo: FastAPI, the Vue dev server, and optionally a Celery worker.

    python demo.py              # backend + frontend
    python demo.py --celery     # ... and every viewset call routed through a Celery worker

Celery is off by default because it dominates the latency the demo exists to measure: queueing a
task through Redis and waiting for a worker dwarfs the difference between an HTTP request and a
WebSocket frame. With `--celery` that path is exercised instead, at the cost of the benchmark
measuring the queue.

Ctrl-C stops everything, in order, quietly. Two things make that work, and neither is incidental:

* Each child gets its **own session** (`start_new_session=True`). Otherwise Ctrl-C goes to the
  whole foreground process group at once - every process gets the signal simultaneously, each
  prints its own traceback, and the ones that shut down slowly are still running when the shell
  hands you the prompt back. With their own session they hear nothing until this process tells
  them, one at a time.
* Each is stopped by **process group**, not by pid. `npm run` is a shell that spawns node; killing
  the shell leaves the dev server holding the port, which is the "node server does not stop" that
  makes the next run fail on an address already in use.
"""

import argparse
import os
import signal
import subprocess
import sys

from contextlib import suppress

STOP_GRACE_SECONDS = 5


def spawn(command: list[str], env: dict[str, str] | None = None) -> subprocess.Popen:
    """Starts a child in its own session, so that Ctrl-C reaches it only through us."""
    return subprocess.Popen(command, env={**os.environ, **(env or {})}, start_new_session=True)  # noqa: S603


def stop(process: subprocess.Popen, label: str) -> None:
    """
    Asks a child's whole process group to stop, and insists if it will not.

    The group, not the process: `npm run dev` is a shell whose child holds the port, and signalling
    only the shell orphans the server that matters.
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
    Turns a stop signal into the one exception the shutdown path already handles.

    Both are installed explicitly. SIGTERM because that is what a supervisor, an IDE's stop button
    and plain `kill` send, and its default action is to die on the spot, leaving every child still
    holding its port. SIGINT because a process started in the background never gets Python's own
    handler at all - the shell sets it to ignored, and Python leaves an inherited SIG_IGN alone.
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

    # Read at import time by demo.backend.viewsets, so it has to be set before the app is imported
    # - which uvicorn does inside its own event loop, later than it looks.
    env = {"DEMO_CELERY": "1"} if args.celery else {}

    children: list[tuple[str, subprocess.Popen]] = [
        ("the frontend", spawn(["npm", "run", "demo:dev"], env)),  # noqa: S607
    ]
    if args.celery:
        children.append((
            "the Celery worker",
            spawn(
                ["celery", "-A", "demo.backend.celery_worker", "worker",  # noqa: S607
                 "--loglevel=info", "--concurrency=4"],
                env,
            ),
        ))

    print("FastAPI docs:  http://127.0.0.1:8000/docs")
    print("API reference: http://127.0.0.1:8000/redoc")
    print(f"Celery:        {'on' if args.celery else 'off (--celery to enable)'}")

    backend = spawn(
        [sys.executable, "-m", "uvicorn", "demo.backend.main:app",
         "--host", "127.0.0.1", "--port", "8000"],
        env,
    )
    children.insert(0, ("the backend", backend))

    for received in (signal.SIGINT, signal.SIGTERM):
        signal.signal(received, _interrupt)

    try:
        backend.wait()
    except KeyboardInterrupt:
        # The children are in their own sessions, so this process is the only one the terminal
        # interrupted. A blank line puts "Stopping..." past the ^C the shell echoed.
        print()
    finally:
        for label, process in children:
            stop(process, label)
        print("Stopped.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
