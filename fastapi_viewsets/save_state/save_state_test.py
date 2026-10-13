import json

import pytest

from .save_state import SaveStateRedis
from .serialize_state import SerializeStateSlots


class FakeRedis:
    def __init__(self):
        self.values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str) -> None:
        self.values[key] = value


class SessionItems(SaveStateRedis):
    save_state_redis_key = "session:items"

    def __init__(self, redis: FakeRedis):
        super().__init__(instance_id="session", redis=redis)
        self.items: list[str] = []

    async def serialize_state(self) -> str:
        return json.dumps(self.items)

    async def deserialize_state(self, state: str | None):
        if state is not None:
            self.items = json.loads(state)


@pytest.mark.asyncio
async def test_state_survives_from_one_instance_to_the_next():
    redis = FakeRedis()
    first = SessionItems(redis)
    await first.load_state()
    assert first.items == []
    first.items.append("a")
    await first.save_state()

    second = SessionItems(redis)
    await second.load_state()
    assert second.items == ["a"]


class Counter(SerializeStateSlots):
    __slots__ = ("count",)

    def __init__(self):
        super().__init__()
        self.count = 0


@pytest.mark.asyncio
async def test_slots_keep_their_values_while_nothing_is_stored():
    counter = Counter()
    await counter.deserialize_state(None)
    assert counter.count == 0


@pytest.mark.asyncio
async def test_slots_round_trip():
    counter = Counter()
    counter.count = 3
    restored = Counter()
    await restored.deserialize_state(await counter.serialize_state())
    assert restored.count == 3
