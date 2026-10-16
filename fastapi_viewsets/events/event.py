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

    The class-level `event_version`, 1 unless the subclass sets a larger int, is the version of the
    payload's shape. It travels with every event, so a consumer tells the shapes of an event's
    payload apart and handles several versions side by side:

        class ImportFinished(Event):
            event_name = "import_finished"
            event_version = 2

            file: str
            rows: int
            skipped: int

    Both are class attributes: a plain assignment, as above, or a `ClassVar` annotation. A subclass
    that annotates either one as a field (`event_version: int = 2`), lacks a non-empty string
    `event_name`, or has an `event_version` that is not an int of at least 1 (a `bool` is not),
    raises `TypeError` when the class is created. The payload on the wire is
    `model_dump(mode="json")`.
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
