from celery import Celery
from redis import Redis
from redis.asyncio import Redis as AsyncRedis

celery_app = Celery("demo", broker="redis://localhost:6379/0")
redis_sync = Redis(host="localhost", port=6379, db=0)
redis_async = AsyncRedis(host="localhost", port=6379, db=0)
