import asyncio
import base64
import hashlib
import json
import logging
import time
import uuid

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

from fastapi.encoders import jsonable_encoder
from starlette.responses import Response

from ..conf import settings
from ..exceptions import (
    _error_from_jsonable,
    _error_to_jsonable,
    ActionDispatchError,
    IdempotencyKeyInvalidError,
    IdempotencyKeyReusedError,
)
from ..routing import GATE_RUN_SCOPE_KEY
from . import Middleware, ViewSetResult

if TYPE_CHECKING:
    from fastapi import Request
    from redis.asyncio import Redis

    from ..context import Context

logger = logging.getLogger(__name__)

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"

# Methods whose outcome is stored only when a repeat attached to the call while it ran.
_SAFE_METHODS = frozenset({"GET", "HEAD"})

# Replaces the running record written by this process with the outcome, or deletes it. A record that
# is no longer the one this process wrote (its lock expired and another call took the key over) is
# left alone. Returns 2 when the outcome was stored, 1 when the record was deleted, 0 when it was not
# this process's record.
_FINISH_SCRIPT = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
    return 0
end
local store = ARGV[4] == 'always'
    or (ARGV[4] == 'attached' and (ARGV[5] == '1' or redis.call('EXISTS', KEYS[2]) == 1))
redis.call('DEL', KEYS[2])
if store then
    redis.call('SET', KEYS[1], ARGV[2], 'PX', ARGV[3])
    return 2
end
redis.call('DEL', KEYS[1])
return 1
"""

# Marks the running record KEYS[1] as attached, with the record's remaining lifetime, only while it is
# still the running record ARGV[1] the repeat read. Returns 1 when the mark was set.
_ATTACH_SCRIPT = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
    return 0
end
local ttl = redis.call('PTTL', KEYS[1])
if ttl <= 0 then
    return 0
end
redis.call('SET', KEYS[2], '1', 'PX', ttl)
return 1
"""

# Extends the running record KEYS[1] and its attached mark KEYS[2] to ARGV[2] milliseconds, only while
# KEYS[1] is still the running record ARGV[1] this process wrote. Returns 1 when it was extended.
_RENEW_SCRIPT = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
    return 0
end
redis.call('PEXPIRE', KEYS[1], ARGV[2])
if redis.call('EXISTS', KEYS[2]) == 1 then
    redis.call('PEXPIRE', KEYS[2], ARGV[2])
end
return 1
"""


def parse_idempotency_key(value: str) -> str:
    """
    The key carried by an `Idempotency-Key` header value: a Structured Field string (RFC 8941,
    section 3.3.3), e.g. `"8e03978e-40d5-43e8-bc93-6894a57f9324"` with the quotes. Raises
    `IdempotencyKeyInvalidError` for anything else: a token, a parameter after the string, an
    escape other than `\\"` and `\\\\`, a character outside printable ASCII, or an empty string.
    """
    text = value.strip(" \t")
    if len(text) < 2 or text[0] != '"':
        raise IdempotencyKeyInvalidError()
    chars: list[str] = []
    index = 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 1
            if index >= len(text) or text[index] not in '"\\':
                raise IdempotencyKeyInvalidError()
            chars.append(text[index])
        elif char == '"':
            if index != len(text) - 1 or not chars:
                raise IdempotencyKeyInvalidError()
            return "".join(chars)
        elif not " " <= char <= "~":
            raise IdempotencyKeyInvalidError()
        else:
            chars.append(char)
        index += 1
    raise IdempotencyKeyInvalidError()


def read_idempotency_key(request: "Request") -> str | None:
    """The key of `request`'s `Idempotency-Key` header, or None without one. More than one such
    header raises `IdempotencyKeyInvalidError`, as does a value `parse_idempotency_key` rejects."""
    values = request.headers.getlist(IDEMPOTENCY_KEY_HEADER)
    if not values:
        return None
    if len(values) > 1:
        raise IdempotencyKeyInvalidError()
    return parse_idempotency_key(values[0])


async def request_fingerprint(request: "Request") -> str:
    """
    SHA-256 over the method, path, query string and the SHA-256 of the body of `request`. A body
    FastAPI read as a form (the raw stream is consumed) is hashed as its sorted fields, a file
    field as its file name and size.
    """
    try:
        body = await request.body()
    except RuntimeError:
        form = await request.form()
        fields = sorted(
            (name, value if isinstance(value, str) else f"{value.filename}:{value.size}")
            for name, value in form.multi_items()
        )
        body = json.dumps(fields).encode()
    parts = [request.method, request.url.path, request.url.query, hashlib.sha256(body).hexdigest()]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


@dataclass(frozen=True)
class _StoredResponse:
    """A buffered `Response` as recorded: its status, its raw headers as latin-1 strings without
    `content-length`, and its body."""

    status_code: int
    raw_headers: list[tuple[str, str]]
    body: bytes

    @classmethod
    def of(cls, response: Response) -> "_StoredResponse":
        headers = [
            (name.decode("latin-1"), value.decode("latin-1"))
            for name, value in response.raw_headers
            if name.lower() != b"content-length"
        ]
        return cls(response.status_code, headers, bytes(response.body))

    def to_response(self) -> Response:
        """A new `Response` with the recorded status, headers and body, and no background task."""
        response = Response(content=self.body, status_code=self.status_code)
        response.raw_headers = [(name.encode("latin-1"), value.encode("latin-1")) for name, value in self.raw_headers]
        response.raw_headers.append((b"content-length", str(len(self.body)).encode("latin-1")))
        return response

    @property
    def sets_cookie(self) -> bool:
        return any(name.lower() == "set-cookie" for name, _value in self.raw_headers)

    def to_json(self) -> dict[str, Any]:
        return {
            "status_code": self.status_code,
            "headers": [[name, value] for name, value in self.raw_headers],
            "body": base64.b64encode(self.body).decode("ascii"),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "_StoredResponse":
        headers = [(name, value) for name, value in data["headers"]]
        return cls(data["status_code"], headers, base64.b64decode(data["body"]))


def _is_buffered(body: Any) -> bool:
    return isinstance(body, Response) and isinstance(getattr(body, "body", None), bytes)


@dataclass
class _Outcome:
    """What a call answered: a `ViewSetResult`, or an error as the fields of an `HTTPException`. A
    result whose body is a buffered `Response` holds it as a `_StoredResponse`."""

    result: ViewSetResult | None = None
    error: dict[str, Any] | None = None

    @classmethod
    def of_error(cls, error: Exception) -> "_Outcome":
        return cls(error=_error_to_jsonable(error))

    @classmethod
    def of_result(cls, result: ViewSetResult) -> "_Outcome":
        """The outcome of `result`; a buffered `Response` body is recorded as a `_StoredResponse`."""
        if _is_buffered(result.body):
            result = ViewSetResult(
                body=_StoredResponse.of(result.body),
                headers=result.headers,
                cookies=result.cookies,
                status_code=result.status_code,
            )
        return cls(result=result)

    @property
    def storable(self) -> bool:
        """False for a result whose body is a `Response` without a buffered body (`StreamingResponse`,
        `FileResponse`)."""
        return self.result is None or not isinstance(self.result.body, Response)

    @property
    def sets_cookie(self) -> bool:
        if self.result is not None:
            headers = self.result.headers
            body = self.result.body
            return (
                bool(self.result.cookies)
                or any(name.lower() == "set-cookie" for name in headers)
                or (isinstance(body, _StoredResponse) and body.sets_cookie)
            )
        return any(name.lower() == "set-cookie" for name in (self.error["headers"] or {}))

    def replay(self) -> ViewSetResult:
        """A copy of the recorded result, a recorded `Response` as a new `Response`; a recorded error
        is raised as a new exception."""
        if self.result is not None:
            body = self.result.body
            return ViewSetResult(
                body=body.to_response() if isinstance(body, _StoredResponse) else body,
                headers=dict(self.result.headers),
                cookies=dict(self.result.cookies),
                status_code=self.result.status_code,
            )
        raise _error_from_jsonable(self.error)

    def to_json(self) -> Any:
        """The outcome in JSON form. A result's body is encoded as FastAPI encodes a response body
        (`jsonable_encoder`: models by alias), so the route's response model accepts it on a repeat;
        a recorded `Response` as its status, headers and base64 body under `response`."""
        if self.result is not None:
            body = self.result.body
            encoded: dict[str, Any] = {
                "headers": jsonable_encoder(self.result.headers),
                "status_code": self.result.status_code,
            }
            if isinstance(body, _StoredResponse):
                encoded["response"] = body.to_json()
            else:
                encoded["body"] = jsonable_encoder(body)
            return {"result": encoded}
        return {"error": self.error}

    @classmethod
    def from_json(cls, data: Any) -> "_Outcome":
        if "result" in data:
            result = data["result"]
            body = _StoredResponse.from_json(result["response"]) if "response" in result else result["body"]
            return cls(result=ViewSetResult(body=body, headers=result["headers"], status_code=result["status_code"]))
        return cls(error=data["error"])


@dataclass
class _Record:
    """A key's record: the fingerprint, and either the owner of the running call or the outcome."""

    fingerprint: str
    owner: str | None = None
    outcome: _Outcome | None = None
    expires_at: float = 0.0


@dataclass
class _Run:
    """A call this process runs under a key."""

    owner: str
    running: str
    future: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())
    attached: bool = False


class Idempotency(Middleware):
    """
    Makes every call that carries an `Idempotency-Key` header repeatable: a repeat with the same key
    answers with the outcome of the first call instead of running the action again, and a repeat
    that arrives while the first call is running attaches to it and answers with its outcome. See
    the Idempotency guide page for the rules.

        settings.viewsets_command_middleware = [Idempotency(redis_client=redis), ...]

    Placed first in `viewsets_command_middleware`: a middleware ahead of it runs again on every
    repeat.

    - The header value is a Structured Field string; anything else raises
      `IdempotencyKeyInvalidError` (400) from `depends()`.
    - The fingerprint of a call is its method, path, query string and body hash
      (`request_fingerprint`). A key recorded with a different fingerprint raises
      `IdempotencyKeyReusedError` (422).
    - The record is written when the chain reaches this middleware, as running, for
      `settings.viewsets_idempotency_lock_ttl` seconds. While the call runs, this process extends the
      running record to the lock TTL every third of the lock TTL, so it expires only once the process
      stops running the call without completing it. When the call completes, its outcome replaces
      the running record for `settings.viewsets_idempotency_ttl` seconds. An outcome of `GET` or
      `HEAD` is stored only when a repeat attached while the call ran; an outcome with a cookie or a
      `set-cookie` header is never stored. A record that is not stored is deleted.
    - A result whose body is a starlette `Response` with a buffered `body` is recorded as its status,
      raw headers and body, and a repeat receives a new `Response` built from them, without a
      background task; the response's own status is the answer's. A `Response` without a buffered
      body (`StreamingResponse`, `FileResponse`) is not stored, and a `set-cookie` among its headers
      is a cookie.
    - An error outcome is stored too and raised again on a repeat: an `HTTPException` as it was
      raised (a `DfViewSetError` with its `code` and `params`, and with its class when
      `fastapi_viewsets.exceptions` defines it), a `RequestValidationError` as the
      `RequestValidationError` of the same errors, any other exception as an `UnhandledError`. The
      repeat is answered with the body of `fastapi_viewsets.error_responses`, as the first call was.
      An `ActionDispatchError` is not an outcome: the action did not run (it was not sent, or the
      executor revoked, rejected or did not know it), the record is deleted, and a repeat runs the
      call again.

    Without `redis_client` the records live in this instance, which serves one process. With a
    `redis_client` (`redis.asyncio.Redis`) the record of a key is the Redis string
    `<key_prefix><key>`, written with `SET NX PX`; a repeat on another process marks
    `<key_prefix><key>:attached` while the running record it read still exists, subscribes to the
    channel `<key_prefix><key>`, reads the record again and waits for the message published after
    the outcome is stored or the record deleted, or for the running record to expire. An outcome
    that cannot be JSON-encoded is not stored. An events gate run (`GATE_RUN_SCOPE_KEY` in the request
    scope) passes through. A repeat on the process running the call awaits it in
    process either way.
    """

    def __init__(self, redis_client: "Redis | None" = None, key_prefix: str = "idempotency:"):
        self.redis_client = redis_client
        self.key_prefix = key_prefix
        self._records: dict[str, _Record] = {}
        self._runs: dict[str, _Run] = {}
        self._next_prune = 0.0

    async def depends(self, request: "Request | None", _cls: type, _context: "Context") -> None:
        if request is not None:
            read_idempotency_key(request)

    async def __call__(
        self,
        request: "Request | None",
        _viewset: Any,
        _context: "Context",
        call_next: Callable[[], Awaitable[ViewSetResult]],
    ) -> ViewSetResult:
        if request is not None and request.scope.get(GATE_RUN_SCOPE_KEY):
            return await call_next()
        key = read_idempotency_key(request) if request is not None else None
        if key is None:
            return await call_next()
        fingerprint = await request_fingerprint(request)
        storage_key = self.key_prefix + key
        while True:
            claimed = await self._claim(storage_key, fingerprint)
            if isinstance(claimed, _Run):
                return await self._run(storage_key, claimed, request.method, call_next)
            record = claimed
            if record is not None and record.outcome is None and record.fingerprint == fingerprint:
                record = await self._attach(storage_key, record)
            if record is None or (record.outcome is None and record.fingerprint == fingerprint):
                # gone, expired or still running after its lock expired: claim again
                continue
            if record.fingerprint != fingerprint:
                raise IdempotencyKeyReusedError(key)
            return record.outcome.replay()

    async def _run(
        self, storage_key: str, run: _Run, method: str, call_next: Callable[[], Awaitable[ViewSetResult]]
    ) -> ViewSetResult:
        try:
            result = await self._call_renewing(storage_key, run, call_next)
        except ActionDispatchError:
            await self._finish(storage_key, run, None, method)
            raise
        except Exception as error:
            await self._finish(storage_key, run, _Outcome.of_error(error), method)
            raise
        except BaseException:
            await self._finish(storage_key, run, None, method)
            raise
        await self._finish(storage_key, run, _Outcome.of_result(result), method)
        return result

    async def _call_renewing(
        self, storage_key: str, run: _Run, call_next: Callable[[], Awaitable[ViewSetResult]]
    ) -> ViewSetResult:
        renewal = asyncio.ensure_future(self._renew(storage_key, run))
        try:
            return await call_next()
        finally:
            renewal.cancel()

    async def _renew(self, storage_key: str, run: _Run) -> None:
        """Extends the running record of `run` to the lock TTL every third of the lock TTL, until it is
        cancelled or the record is no longer the one this process wrote."""
        lock_ttl = settings.viewsets_idempotency_lock_ttl
        while True:
            await asyncio.sleep(lock_ttl / 3)
            try:
                if self.redis_client is None:
                    renewed = self._renew_in_memory(storage_key, run, lock_ttl)
                else:
                    renewed = await self.redis_client.eval(
                        _RENEW_SCRIPT, 2, storage_key, storage_key + ":attached", run.running, _milliseconds(lock_ttl)
                    )
            except Exception:
                logger.exception("Idempotency: extending the running record of %s failed", storage_key)
                continue
            if not renewed:
                return

    async def _finish(self, storage_key: str, run: _Run, outcome: _Outcome | None, method: str) -> None:
        """Stores the outcome or deletes the record, publishes on the key's channel and resolves the
        in-process repeats, in that order. `outcome` None (the call was cancelled), an outcome that
        sets a cookie and an outcome that is not storable are not stored, and the in-process repeats
        claim the key again."""
        if outcome is None or outcome.sets_cookie or not outcome.storable:
            mode, outcome = "never", None
        else:
            mode = "attached" if method in _SAFE_METHODS else "always"
        try:
            if self.redis_client is None:
                self._finish_in_memory(storage_key, run, outcome, mode)
            else:
                await self._finish_in_redis(storage_key, run, outcome, mode)
        except Exception:
            logger.exception("Idempotency: recording the outcome of %s failed", storage_key)
        finally:
            if self._runs.get(storage_key) is run:
                del self._runs[storage_key]
            if not run.future.done():
                run.future.set_result(outcome)

    async def _attach(self, storage_key: str, record: _Record) -> _Record | None:
        """Waits for the running call of `record` and returns the key's record once it completes,
        or None when the record is gone or its lock expired."""
        run = self._runs.get(storage_key)
        if run is not None:
            run.attached = True
            while not run.future.done():
                remaining = await self._lock_remaining(storage_key)
                if remaining <= 0:
                    return None
                await asyncio.wait({run.future}, timeout=remaining)
            if run.future.result() is None:
                return None
            return _Record(record.fingerprint, outcome=run.future.result())
        if self.redis_client is None:
            # a running record no call of this instance owns
            self._records.pop(storage_key, None)
            return None
        return await self._attach_remote(storage_key)

    # in-process storage

    def _prune(self, now: float) -> None:
        if now < self._next_prune:
            return
        self._records = {key: record for key, record in self._records.items() if record.expires_at > now}
        self._next_prune = now + min(settings.viewsets_idempotency_ttl, settings.viewsets_idempotency_lock_ttl)

    def _claim_in_memory(self, storage_key: str, fingerprint: str) -> _Run | _Record:
        now = time.monotonic()
        self._prune(now)
        record = self._records.get(storage_key)
        if record is not None and record.expires_at > now:
            return record
        owner = uuid.uuid4().hex
        self._records[storage_key] = _Record(
            fingerprint, owner=owner, expires_at=now + settings.viewsets_idempotency_lock_ttl
        )
        run = _Run(owner=owner, running=owner)
        self._runs[storage_key] = run
        return run

    def _renew_in_memory(self, storage_key: str, run: _Run, lock_ttl: float) -> bool:
        record = self._records.get(storage_key)
        if record is None or record.owner != run.owner:
            return False
        record.expires_at = time.monotonic() + lock_ttl
        return True

    def _finish_in_memory(self, storage_key: str, run: _Run, outcome: _Outcome | None, mode: str) -> None:
        record = self._records.get(storage_key)
        if record is None or record.owner != run.owner:
            return
        if mode == "always" or (mode == "attached" and run.attached):
            expires_at = time.monotonic() + settings.viewsets_idempotency_ttl
            self._records[storage_key] = _Record(record.fingerprint, outcome=outcome, expires_at=expires_at)
        else:
            del self._records[storage_key]

    # Redis storage

    @staticmethod
    def _decode(raw: Any) -> _Record | None:
        if raw is None:
            return None
        data = json.loads(raw)
        if "outcome" in data:
            return _Record(data["fingerprint"], outcome=_Outcome.from_json(data["outcome"]))
        return _Record(data["fingerprint"], owner=data["owner"])

    async def _claim(self, storage_key: str, fingerprint: str) -> _Run | _Record | None:
        """Writes the running record and returns its `_Run`, or returns the key's existing record;
        None when the existing record disappeared before it could be read."""
        if self.redis_client is None:
            return self._claim_in_memory(storage_key, fingerprint)
        owner = uuid.uuid4().hex
        running = json.dumps({"fingerprint": fingerprint, "owner": owner})
        lock_ms = _milliseconds(settings.viewsets_idempotency_lock_ttl)
        if await self.redis_client.set(storage_key, running, nx=True, px=lock_ms):
            run = _Run(owner=owner, running=running)
            self._runs[storage_key] = run
            return run
        return self._decode(await self.redis_client.get(storage_key))

    async def _finish_in_redis(self, storage_key: str, run: _Run, outcome: _Outcome | None, mode: str) -> None:
        """An outcome that cannot be encoded is not stored. The message on the key's channel is
        published whether or not the script succeeds, so the repeats on other processes read the
        record again at once."""
        done = ""
        if outcome is not None:
            fingerprint = json.loads(run.running)["fingerprint"]
            try:
                done = json.dumps({"fingerprint": fingerprint, "outcome": outcome.to_json()})
            except Exception:
                logger.exception("Idempotency: the outcome of %s cannot be encoded and is not stored", storage_key)
                mode = "never"
        try:
            await self.redis_client.eval(
                _FINISH_SCRIPT,
                2,
                storage_key,
                storage_key + ":attached",
                run.running,
                done,
                _milliseconds(settings.viewsets_idempotency_ttl),
                mode,
                "1" if run.attached else "0",
            )
        finally:
            await self.redis_client.publish(storage_key, "done")

    async def _lock_remaining(self, storage_key: str) -> float:
        """Seconds until the running record of `storage_key` expires; 0 when it is gone."""
        if self.redis_client is None:
            record = self._records.get(storage_key)
            return 0.0 if record is None else record.expires_at - time.monotonic()
        remaining_ms = await self.redis_client.pttl(storage_key)
        return remaining_ms / 1000 if remaining_ms > 0 else 0.0

    async def _attach_remote(self, storage_key: str) -> _Record | None:
        raw = await self.redis_client.get(storage_key)
        record = self._decode(raw)
        if record is None or record.outcome is not None:
            return record
        await self.redis_client.eval(_ATTACH_SCRIPT, 2, storage_key, storage_key + ":attached", raw)
        pubsub = self.redis_client.pubsub()
        try:
            await pubsub.subscribe(storage_key)
            while True:
                record = self._decode(await self.redis_client.get(storage_key))
                if record is None or record.outcome is not None:
                    return record
                remaining = await self._lock_remaining(storage_key)
                if remaining <= 0:
                    return None
                await pubsub.get_message(ignore_subscribe_messages=True, timeout=remaining)
        finally:
            await pubsub.unsubscribe(storage_key)
            await pubsub.aclose()


def _milliseconds(seconds: float) -> int:
    return max(1, int(seconds * 1000))
