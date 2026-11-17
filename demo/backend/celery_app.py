import os

from celery import Celery
from redis import Redis
from redis.asyncio import Redis as AsyncRedis

from demo.backend.benchmark import QUEUES as BENCH_QUEUES

REDIS_URL = os.environ.get("DEMO_REDIS_URL", "redis://localhost:6379/0")

celery_app = Celery("demo", broker=REDIS_URL)
celery_app.conf.task_routes = {f"bench_*_{executor}.*": {"queue": queue} for executor, queue in BENCH_QUEUES.items()}
redis_sync = Redis.from_url(REDIS_URL)
redis_async = AsyncRedis.from_url(REDIS_URL)
