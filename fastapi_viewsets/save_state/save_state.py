from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from .serialize_state import SerializeState

if TYPE_CHECKING:
    from redis.asyncio import Redis

class SaveState(SerializeState, ABC):
    def __init__(self, instance_id: str):
        super().__init__()
        self.instance_id = instance_id

    @abstractmethod
    async def load_state(self):  # noqa: B027
        pass

    @abstractmethod
    async def save_state(self):
        pass


class SaveStateRedis(SaveState, ABC):
    save_state_redis_key: str = None

    def __init__(self, instance_id: str, redis: "Redis"):
        super().__init__(instance_id)
        self.redis = redis
        if self.save_state_redis_key is None:
            raise ValueError("save_state_redis_key class variable must be set")

    async def load_state(self):
        return await self.deserialize_state(await self.redis.get(self.save_state_redis_key))

    async def save_state(self):
        return await self.redis.set(self.save_state_redis_key, await self.serialize_state())
