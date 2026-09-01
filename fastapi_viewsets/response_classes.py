from pydantic import BaseModel


class NotFoundResponse(BaseModel):
    detail: str = "Item with pk {pk} not found"


NOT_FOUND_RESPONSE = {"404": {"model": NotFoundResponse}}
