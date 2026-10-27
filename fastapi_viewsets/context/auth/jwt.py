import datetime

from typing import Any, TYPE_CHECKING

from ...conf import settings
from .. import LazyObject
from . import AuthBackend

if TYPE_CHECKING:
    from fastapi import Request


class _JWTUserLazy(LazyObject):
    """
    The decoded claims of a JWT, verified synchronously without I/O. The recipe carries the token and the
    backend's position in `settings.viewsets_auth_processors`, never the secret; a worker whose settings
    hold no `JWTAuthBackend` at that position resolves it to None.
    """

    def __init__(self, token: str, backend: "JWTAuthBackend | None"):
        super().__init__()
        self._token = token
        self._backend = backend

    def resolve(self) -> Any:
        import jwt as pyjwt

        if self._backend is None:
            return None
        try:
            return pyjwt.decode(self._token, self._backend.secret, algorithms=[self._backend.algorithm])
        except pyjwt.PyJWTError:
            return None  # expired or invalid signature

    def serialize_recipe(self) -> Any:
        processors = settings.viewsets_auth_processors
        position = next((i for i, backend in enumerate(processors) if backend is self._backend), None)
        return {"token": self._token, "backend": position}

    @classmethod
    def deserialize_recipe(cls, data: Any) -> "_JWTUserLazy":
        processors = settings.viewsets_auth_processors
        position = data.get("backend")
        backend = processors[position] if isinstance(position, int) and 0 <= position < len(processors) else None
        return cls(data["token"], backend if isinstance(backend, JWTAuthBackend) else None)


class JWTAuthBackend(AuthBackend):
    """
    Verifies a `Bearer` JWT's signature and expiry locally. Requires the `jwt` extra. A JWT cannot be
    revoked: mint short-lived tokens (`encode_jwt`) only for an active, revocable session.
    """

    def __init__(self, secret: str, algorithm: str = "HS256", header_name: str = "authorization"):
        self.secret = secret
        self.algorithm = algorithm
        self.header_name = header_name

    def try_handle(self, request: "Request") -> LazyObject | None:
        header = request.headers.get(self.header_name, "")
        if not header.lower().startswith("bearer "):
            return None  # not a bearer token: the next backend tries
        return _JWTUserLazy(header[len("bearer ") :].strip(), self)


def encode_jwt(
    claims: dict[str, Any],
    secret: str,
    algorithm: str = "HS256",
    expires_in: datetime.timedelta = datetime.timedelta(minutes=15),
) -> str:
    """A signed JWT of `claims` plus `iat` and `exp`, for the application's own login endpoint."""
    import jwt as pyjwt

    now = datetime.datetime.now(datetime.timezone.utc)
    payload = {**claims, "iat": now, "exp": now + expires_in}
    return pyjwt.encode(payload, secret, algorithm=algorithm)
