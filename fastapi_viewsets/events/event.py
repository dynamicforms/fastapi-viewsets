from typing import Any, ClassVar

from pydantic import BaseModel


class Event(BaseModel):
    """
    Base of every event; the pydantic fields are the payload (`model_dump(mode="json")` on the wire).
    A subclass sets the class attributes `event_name` (what the frontend dispatches on) and optionally
    `event_version` (payload shape version, default 1). Declaring either as a field, an empty name or a
    version that is not an int >= 1 raises TypeError at class creation.
    """

    event_name: ClassVar[str]
    event_version: ClassVar[int] = 1

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        for attribute in ("event_name", "event_version"):
            if attribute in cls.model_fields:
                raise TypeError(
                    f"{cls.__qualname__} declares {attribute} as a field; an Event subclass sets it as a class "
                    "attribute, by plain assignment or as a ClassVar"
                )
        name = getattr(cls, "event_name", None)
        if not isinstance(name, str) or not name:
            raise TypeError(f"{cls.__qualname__} sets no event_name; an Event subclass sets it to a non-empty string")
        version = getattr(cls, "event_version", None)
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise TypeError(
                f"{cls.__qualname__} sets event_version to {version!r}; "
                "an Event subclass sets it to an int of at least 1"
            )
