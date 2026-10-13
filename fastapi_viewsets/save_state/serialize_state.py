import json

from abc import ABC, abstractmethod


class SerializeState(ABC):
    def __init__(self, custom_json_encoder=None, custom_json_decoder=None):
        self.custom_json_encoder = custom_json_encoder
        self.custom_json_decoder = custom_json_decoder

    @abstractmethod
    async def serialize_state(self) -> str:  # noqa: B027
        pass

    @abstractmethod
    async def deserialize_state(self, state: str | None):
        """Restores what `serialize_state()` produced; `state` is None when nothing is stored yet."""


class SerializeStateSlots(SerializeState):
    async def serialize_state(self) -> str:  # noqa: B027
        return json.dumps(
            {slot: getattr(self, slot) for slot in self.__slots__},
            cls=self.custom_json_encoder,
        )

    async def deserialize_state(self, state: str | None):
        # nothing stored yet: the slots keep the values the constructor gave them
        if state is None:
            return
        state = json.loads(state, cls=self.custom_json_decoder)
        for slot in self.__slots__:
            setattr(self, slot, state.get(slot))
