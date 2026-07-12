from abc import ABC, abstractmethod
from typing import Any, TYPE_CHECKING

from ...conf import settings
from .. import LazyObject

if TYPE_CHECKING:
    from fastapi import Request


class AuthBackend(ABC):
    """
    One entry in `settings.viewsets_auth_processors` (see `auth_context_processor` below). Each
    backend recognizes a different way of proving who's calling - a fixed/ad-hoc user list, a
    real Django session, anything else an app wants to plug in.

    `settings.viewsets_auth_processors` is tried in order; the first backend whose `try_handle`
    returns non-None "claims" the request - no other backend is tried after that, even if the
    claimed LazyObject later resolves to `None` (an invalid/expired credential of a *recognized*
    shape is not the same as "this isn't my kind of credential, try the next backend").
    """

    @abstractmethod
    def try_handle(self, request: "Request") -> LazyObject | None:
        """
        Cheap, synchronous check. Return None immediately if this backend doesn't recognize
        credentials in this request (the caller tries the next backend). Otherwise return a
        LazyObject that resolves (sync or async, see LazyObject) to the authenticated user, or
        None if the credential turns out invalid/expired once actually resolved.
        """
        raise NotImplementedError


async def auth_context_processor(request: "Request", _viewset: Any) -> dict[str, Any]:
    """
    A ready-to-use `settings.viewsets_context_processors` entry: iterates
    `settings.viewsets_auth_processors` and uses the first backend that claims this request,
    contributing `{"user": <LazyObject>}` to context. If no backend claims it (or none are
    configured), contributes `{"user": None}` - context processors only ever gather this fact,
    they don't decide what to do about it (see `fastapi_viewsets.middleware.auth.Session` for the
    command middleware that actually rejects unauthenticated requests).

    Wire it in like any other processor:

        from fastapi_viewsets.conf import settings
        from fastapi_viewsets.context.auth import auth_context_processor

        settings.viewsets_context_processors = [auth_context_processor]
        settings.viewsets_auth_processors = [my_backend_a, my_backend_b]
    """
    for backend in settings.viewsets_auth_processors:
        lazy_user = backend.try_handle(request)
        if lazy_user is not None:
            return {"user": lazy_user}
    return {"user": None}
