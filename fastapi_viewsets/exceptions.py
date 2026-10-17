"""
The HTTP exceptions this package raises on its own behalf.

`detail` is the English default, fully interpolated - a plain string, so an application that does
nothing further receives FastAPI's usual `{"detail": "..."}`. `code` names what went
wrong independently of `detail`'s wording, and `params` are the raw values `detail` was
interpolated with; both reach the response body only once the application registers
`df_viewset_exception_handler` for `DfViewSetError` (see that function). A view's own
`raise HTTPException(status_code, detail="...")` is unaffected either way.
"""

import sys

from typing import Any

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .cursor import CursorError


class DfViewSetError(HTTPException):
    """Base for every `HTTPException` this package raises on its own behalf."""

    def __init__(self, status_code: int, message: str, code: str, params: dict[str, Any] | None = None):
        super().__init__(status_code=status_code, detail=message)
        self.code = code
        self.params = params or {}

    def __reduce__(self):
        # pickled by its fields, not by its constructor's arguments, which differ per subclass
        return _rebuild_error, (type(self), self.status_code, self.detail, self.code, self.params, self.headers)


def _rebuild_error(
    cls: type[DfViewSetError],
    status_code: int,
    detail: Any,
    code: str,
    params: dict[str, Any] | None,
    headers: dict[str, str] | None,
) -> DfViewSetError:
    """An instance of `cls` with these fields, built without `cls.__init__`."""
    error = cls.__new__(cls)
    DfViewSetError.__init__(error, status_code, detail, code, params)
    error.headers = headers
    return error


class NotFoundError(DfViewSetError):
    def __init__(self, pk: Any):
        super().__init__(404, f"Item with pk {pk} not found", "not_found", {"pk": pk})


class ReadOnlyCollectionError(DfViewSetError):
    """A write to a `CollectionViewSet` whose container is immutable (a tuple, a frozenset, a
    read-only mapping)."""

    def __init__(self):
        super().__init__(405, "This collection is read-only", "read_only")


class SessionExpiredError(DfViewSetError):
    def __init__(self):
        super().__init__(401, "Session expired or invalid", "session_expired")


class NotAuthorizedError(DfViewSetError):
    def __init__(self):
        super().__init__(403, "Not authorized to perform this action", "not_authorized")


class RateLimitedError(DfViewSetError):
    def __init__(self):
        super().__init__(429, "Rate limit exceeded", "rate_limited")


class ActionOutcomeUnknownError(DfViewSetError):
    """
    Base of the errors that end a call to a deferred action whose outcome the caller did not learn:
    the action may have run, may still run, or may never run. Not raised itself.
    """


class ResultTimeoutError(ActionOutcomeUnknownError):
    """A `celery_viewset` call whose result did not arrive from the worker within `timeout` seconds."""

    def __init__(self, task: str, timeout: float):
        message = f"No result for {task} within {timeout} s"
        super().__init__(504, message, "result_timeout", {"task": task, "timeout": timeout})


class ResultReaderStoppedError(ActionOutcomeUnknownError):
    """A `celery_viewset` call that was pending when the result reader of its `queue_key` stopped."""

    def __init__(self, queue_key: str):
        message = f"The result reader for {queue_key} stopped before the result arrived"
        super().__init__(503, message, "result_reader_stopped", {"queue_key": queue_key})


class ActionDispatchError(DfViewSetError):
    """
    A call to a deferred action that did not run. `reason`:

    - `send`: the call could not be handed to the executor (the broker is unreachable, the message
      cannot be encoded)
    - `serialization`: the call's arguments, context or request cannot be serialized
    - `unknown_task`: the executor has no task registered under the action's name
    - `rejected`: the executor rejected the message
    - `revoked`: the call was revoked before it started
    - `expired`: the call expired before it started

    Raised in the calling process, the error that prevented the dispatch is the `__cause__`.
    """

    def __init__(self, task: str, reason: str | None = None):
        message = f"{task} could not be handed to its executor"
        super().__init__(503, message, "action_dispatch_failed", {"task": task, "reason": reason})


class ActionInterruptedError(DfViewSetError):
    """
    An execution of a deferred action that the executor ended after it had started. `reason`:

    - `time_limit`: the execution exceeded its time limit (504)
    - `terminated`: the execution was terminated (503)
    - `lost`: the process running the execution exited (503)

    Inside the execution, `Middleware.execute()` sees it from `call_next()` and the action itself
    sees it at the point it was interrupted, or `asyncio.CancelledError` where it was suspended at
    an `await`.
    """

    _MESSAGES = {
        "time_limit": "{task} exceeded its time limit",
        "terminated": "{task} was terminated",
        "lost": "{task} lost the process executing it",
    }

    def __init__(self, task: str, reason: str):
        message = self._MESSAGES.get(reason, "{task} was interrupted").format(task=task)
        status_code = 504 if reason == "time_limit" else 503
        super().__init__(status_code, message, "action_interrupted", {"task": task, "reason": reason})


class ActionFailedError(DfViewSetError):
    """
    A deferred action that failed in its executor with an error that is not an `HTTPException`.
    `detail` names the task only; the original error is logged in the executor's process and is the
    `__cause__` there. `reason`:

    - `error`: the action, or the executor's handling of its call, raised
    - `result`: the action's result cannot be serialized or deserialized
    - `executor`: the executor failed outside the action (`Middleware.executor_started()` raised,
      or the executor's own machinery failed)
    """

    def __init__(self, task: str | None, reason: str = "error"):
        message = f"{task} failed" if task else "The action failed"
        super().__init__(500, message, "action_failed", {"task": task, "reason": reason})


class IdempotencyKeyInvalidError(DfViewSetError):
    """An `Idempotency-Key` header that is not a single, non-empty Structured Field string."""

    def __init__(self):
        super().__init__(400, "Idempotency-Key must be a non-empty Structured Field string", "idempotency_key_invalid")


class IdempotencyKeyReusedError(DfViewSetError):
    """An `Idempotency-Key` already recorded for a call with a different method, path, query or body."""

    def __init__(self, key: str):
        message = f"Idempotency key {key} was used for a different request"
        super().__init__(422, message, "idempotency_key_reused", {"key": key})


class UnsupportedListShapeError(DfViewSetError):
    def __init__(self, shape: str, allowed: list[str]):
        message = f"unsupported list shape {shape!r}; this endpoint offers {', '.join(allowed)}"
        super().__init__(422, message, "unsupported_list_shape", {"shape": shape, "allowed": allowed})


class CursorRequestError(DfViewSetError):
    """Raised in place of a caught `CursorError`, carrying the same `code`/`params` as a 400 response."""

    def __init__(self, error: CursorError):
        super().__init__(400, str(error), error.code, error.params)


class EventTopicInvalidError(DfViewSetError):
    """A topic that is neither a viewset's routed base path nor a source name with an optional key."""

    def __init__(self, topic: str):
        message = f"Topic {topic} is neither a path starting with / nor a source name with an optional key"
        super().__init__(422, message, "event_topic_invalid", {"topic": topic})


class EventTopicRefusedError(DfViewSetError):
    """A topic no event authorizer admitted to the caller."""

    def __init__(self, topic: str):
        super().__init__(403, f"Topic {topic} is not admitted", "event_topic_refused", {"topic": topic})


class EventSubscriptionsExceededError(DfViewSetError):
    """A subscription on a socket that already holds `settings.viewsets_event_max_subscriptions` topics."""

    def __init__(self, limit: int):
        message = f"This socket already holds {limit} topics"
        super().__init__(409, message, "event_subscriptions_exceeded", {"limit": limit})


class EventCursorsExceededError(DfViewSetError):
    """A poll that carries more cursors than `settings.viewsets_event_max_subscriptions`."""

    def __init__(self, count: int, limit: int):
        message = f"A poll carries at most {limit} cursors, this one carries {count}"
        super().__init__(422, message, "event_cursors_exceeded", {"count": count, "limit": limit})


def _error_to_jsonable(error: BaseException) -> dict[str, Any]:
    """
    `error` as the JSON fields of an `HTTPException`: `status_code`, `detail` and `headers`, plus
    `class`, `code` and `params` for a `DfViewSetError`. A `RequestValidationError` is a 422 with its
    errors as `detail`; any other error a 500 with `detail` `"Internal Server Error"`.
    """
    if isinstance(error, RequestValidationError):
        return {"status_code": 422, "detail": jsonable_encoder(error.errors()), "headers": None}
    if isinstance(error, HTTPException):
        encoded = {
            "status_code": error.status_code,
            "detail": jsonable_encoder(error.detail),
            "headers": dict(error.headers) if error.headers else None,
        }
        if isinstance(error, DfViewSetError):
            encoded["class"] = type(error).__name__
            encoded["code"] = error.code
            encoded["params"] = jsonable_encoder(error.params)
        return encoded
    return {"status_code": 500, "detail": "Internal Server Error", "headers": None}


def _error_from_jsonable(data: dict[str, Any]) -> HTTPException:
    """
    The exception `_error_to_jsonable` encoded. Fields with a `code` are a `DfViewSetError`: of the
    class `class` names when this module defines a `DfViewSetError` subclass of that name, else of
    `DfViewSetError` itself, with the same fields either way. Fields without one are an
    `HTTPException`.
    """
    if "code" not in data:
        return HTTPException(status_code=data["status_code"], detail=data["detail"], headers=data.get("headers"))
    cls = getattr(sys.modules[__name__], data.get("class") or "", None)
    if not (isinstance(cls, type) and issubclass(cls, DfViewSetError)):
        cls = DfViewSetError
    return _rebuild_error(
        cls, data["status_code"], data["detail"], data["code"], data.get("params"), data.get("headers")
    )


async def df_viewset_exception_handler(_request: Request, exc: DfViewSetError) -> JSONResponse:
    """
    Adds `detail_code` and `detail_params` alongside `detail` in the response body.

    Opt in once, for `DfViewSetError` - every exception this package raises on its own descends
    from it:

        from fastapi_viewsets.exceptions import DfViewSetError, df_viewset_exception_handler

        app.add_exception_handler(DfViewSetError, df_viewset_exception_handler)

    An application that never registers this receives `{"detail": "..."}`, from FastAPI's own
    default `HTTPException` handling; `code` and `params` reach nothing without it.
    """
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "detail_code": exc.code, "detail_params": exc.params},
        headers=exc.headers,
    )
