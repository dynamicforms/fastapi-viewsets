from abc import ABC, abstractmethod
from typing import Any, TYPE_CHECKING

from ...conf import settings
from .. import LazyObject

if TYPE_CHECKING:
    from fastapi import Request


class AuthBackend(ABC):
    """
    One entry of `settings.viewsets_auth_processors`, recognising one kind of credential. The first
    backend whose `try_handle` returns non-None claims the request, even if its LazyObject later
    resolves to None (an invalid credential of a recognised kind).
    """

    @abstractmethod
    def try_handle(self, request: "Request") -> LazyObject | None:
        """
        Cheap synchronous check: None when the request carries no credential of this kind, otherwise a
        LazyObject resolving to the user, or to None for an invalid credential.
        """
        raise NotImplementedError


async def auth_context_processor(request: "Request", _viewset: Any) -> dict[str, Any]:
    """
    Context processor contributing `{"user": <LazyObject>}` from the first backend of
    `settings.viewsets_auth_processors` that claims the request, else `{"user": None}`. It does not
    reject anonymous requests; `middleware.auth.Session` does.
    """
    for backend in settings.viewsets_auth_processors:
        lazy_user = backend.try_handle(request)
        if lazy_user is not None:
            return {"user": lazy_user}
    return {"user": None}
