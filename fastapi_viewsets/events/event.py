from typing import Any, ClassVar

from pydantic import BaseModel


class Event(BaseModel):
    """
    The base of every event. A subclass sets the class-level `event_name`, which the frontend
    dispatches on together with the topic, and declares the payload as pydantic fields:

        class ImportFinished(Event):
            event_name = "import_finished"

            file: str
            rows: int

    A subclass without a non-empty string `event_name` raises `TypeError` when the class is created.
    The payload on the wire is `model_dump(mode="json")`.
    """

    event_name: ClassVar[str]

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        name = getattr(cls, "event_name", None)
        if not isinstance(name, str) or not name:
            raise TypeError(f"{cls.__qualname__} sets no event_name; an Event subclass sets it to a non-empty string")
