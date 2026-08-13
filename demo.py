"""
Runs the demo: FastAPI, the Vue dev server, and optionally a Celery worker.

    python demo.py              # backend + frontend
    python demo.py --celery     # ... and every viewset call routed through a Celery worker

Celery is off by default because it dominates the latency the demo exists to measure: queueing a
task through Redis and waiting for a worker dwarfs the difference between an HTTP request and a
WebSocket frame. With `--celery` that path is exercised instead, at the cost of the benchmark
measuring the queue.
"""

import argparse
import multiprocessing
import os
import subprocess


def run_celery():
    subprocess.run(
        ["celery", "-A", "demo.backend.celery_worker", "worker", "--loglevel=info", "--concurrency=4"],  # noqa: S607
        check=False,
    )


def run_fe():
    subprocess.run(["npm", "run", "demo:dev"], check=False)  # noqa: S607


def run_fastapi():
    import uvicorn
    uvicorn.run("demo.backend.main:app", host="127.0.0.1", port=8000, reload=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--celery",
        action="store_true",
        help="route viewset calls through a Celery worker (needs Redis on localhost:6379)",
    )
    args = parser.parse_args()

    # Read at import time by demo.backend.viewsets, so it has to be set before the app is imported
    # - which uvicorn does inside its own event loop, later than it looks.
    if args.celery:
        os.environ["DEMO_CELERY"] = "1"

    print("FastAPI docs: http://127.0.0.1:8000/docs")
    print(f"Celery: {'on' if args.celery else 'off (--celery to enable)'}")

    processes = [multiprocessing.Process(target=run_fe, daemon=True)]
    if args.celery:
        processes.append(multiprocessing.Process(target=run_celery, daemon=True))
    for process in processes:
        process.start()

    try:
        run_fastapi()
    except KeyboardInterrupt:
        pass
    finally:
        for process in processes:
            process.terminate()
            process.join()
        print("Stopped.")


if __name__ == "__main__":
    main()
