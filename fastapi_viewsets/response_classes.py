from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel


class NotFoundResponse(BaseModel):
    detail: str = "Item with pk {pk} not found"

NOT_FOUND_RESPONSE= { "404": { "model": NotFoundResponse, "message": NotFoundResponse().detail } }

class NotFoundError(HTTPException):
    def __init__(self, pk: Any):
        # NotFoundResponse has only "detail", so it deconstructs into exactly the same parameter for HTTPException
        super().__init__(status_code=404, **NotFoundResponse(detail=f"Item with pk {pk} not found").model_dump())
