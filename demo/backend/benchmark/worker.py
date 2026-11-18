"""
The Celery app of the benchmark's workers: registers only the benchmark viewsets' tasks.

    celery -A demo.backend.benchmark.worker worker -Q bench-classic --concurrency 2 -n classic@%h
"""

import os

from demo.backend import benchmark

# Running this worker means the benchmark's Celery executors are wanted; set before the viewsets are wrapped. The UI's
# viewsets stay in-process here: this worker serves only the benchmark queues.
benchmark.CELERY_ENABLED = True
os.environ.pop("DEMO_CELERY", None)

from demo.backend import postgres_demo  # noqa: E402

if not postgres_demo.ENABLED:
    raise SystemExit(f"The benchmark worker needs PostgreSQL: {postgres_demo.UNAVAILABLE}")

import demo.backend.benchmark.viewsets  # noqa: E402, F401 - registers the benchmark tasks on celery_app

from demo.backend.celery_app import celery_app  # noqa: E402

# Prefetch accounting in the worker's memory, one Redis round trip per message; the tasks run with acks_late=False.
celery_app.conf.broker_transport_options = {"ack_emulation": False}

if postgres_demo.ADORM_ENABLED:
    postgres_demo.configure_adorm()

app = celery_app
