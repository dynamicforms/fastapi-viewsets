import datetime

from typing import Any, TYPE_CHECKING

from ...conf import settings
from .. import LazyObject
from . import AuthBackend

if TYPE_CHECKING:
    from fastapi import Request


class _JWTUserLazy(LazyObject):
    """
    resolve() is sync and needs no I/O - signature+expiry verification is cheap, local computation
    (unlike DjangoSessionAuthBackend, there's no session store to hit). Uses the default
    recipe+resolved_value shortcut (see LazyObject) since decoded claims are already JSON-safe.

    The recipe carries the token and the position of the verifying backend in
    `settings.viewsets_auth_processors`, never the secret: a Celery worker, configured with the same
    settings, verifies the token with its own copy of that backend. Where that position holds no
    `JWTAuthBackend`, the token resolves to None.
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
            # expired/invalid signature - same "recognized shape, invalid credential" case as
            # DjangoSessionAuthBackend
            return None

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
    Verifies a `Bearer` JWT's signature and expiry - no session store, no database lookup, purely a
    local computation. Requires the `jwt` extra (`pip install "dynamicforms-fastapi-viewsets[jwt]"`).

        settings.viewsets_auth_processors = [JWTAuthBackend(secret="...", algorithm="HS256")]

    A bare, stateless JWT can't be revoked - there's no server-side record to delete, unlike
    `DjangoSessionAuthBackend`. Don't use this as a drop-in session replacement; instead mint
    short-lived tokens (see `encode_jwt` below) only once an actual, revocable session (e.g. a
    Django session) is confirmed active, so "logout"/password-change/force-logout-everywhere still
    work as expected (they revoke the underlying session; already-issued tokens simply expire on
    their own, soon, being short-lived) - see docs/guide/authentication.md.
    """

    def __init__(self, secret: str, algorithm: str = "HS256", header_name: str = "authorization"):
        self.secret = secret
        self.algorithm = algorithm
        self.header_name = header_name

    def try_handle(self, request: "Request") -> LazyObject | None:
        header = request.headers.get(self.header_name, "")
        if not header.lower().startswith("bearer "):
            return None  # not a bearer token - let the next backend try it
        return _JWTUserLazy(header[len("bearer ") :].strip(), self)


def encode_jwt(
    claims: dict[str, Any],
    secret: str,
    algorithm: str = "HS256",
    expires_in: datetime.timedelta = datetime.timedelta(minutes=15),
) -> str:
    """
    Mints a short-lived, signed JWT - for an app's own login endpoint to call once it has confirmed
    an active session (e.g. via `DjangoSessionAuthBackend`), not called by this library itself.
    Stamps `exp` (and `iat`) onto `claims`; `JWTAuthBackend`/`_JWTUserLazy` verify both on the way
    back in.
    """
    import jwt as pyjwt

    now = datetime.datetime.now(datetime.timezone.utc)
    payload = {**claims, "iat": now, "exp": now + expires_in}
    return pyjwt.encode(payload, secret, algorithm=algorithm)
