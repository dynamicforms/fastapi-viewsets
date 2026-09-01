from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    """
    The value every `HTTPException` this package raises puts under the response's own `detail` key.

    `message` is the English default, fully interpolated - a plain HTTP client with no translation
    layer of its own can show it as-is. `code` is a stable identifier a frontend can switch on, or
    look up its own translation for, regardless of what `message` says. `params` are the raw values
    `message` was interpolated with, so a translation-aware frontend can re-interpolate its own
    template with them instead of parsing them back out of English prose.
    """

    message: str
    code: str
    params: dict[str, Any] = {}


def not_found_detail(pk: Any) -> ErrorDetail:
    return ErrorDetail(message=f"Item with pk {pk} not found", code="not_found", params={"pk": pk})


class NotFoundResponse(BaseModel):
    detail: ErrorDetail = Field(default_factory=lambda: not_found_detail("{pk}"))


NOT_FOUND_RESPONSE = {"404": {"model": NotFoundResponse}}


class NotFoundError(HTTPException):
    def __init__(self, pk: Any):
        super().__init__(status_code=404, detail=not_found_detail(pk).model_dump())
