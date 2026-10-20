"""
The HTTP exceptions this package raises on its own behalf.

`detail` is the English default, fully interpolated. `code` names what went wrong independently of
`detail`'s wording, and `params` are the raw values `detail` was interpolated with. A response
carries all three in the error body of `fastapi_viewsets.error_responses`.

`code` is a full code, `<namespace>.<code>`: the errors this module defines carry codes in the
namespace `ERROR_NAMESPACE` (`fastapi-viewsets.not_found`). The code of an application's own
`DfViewSetError` is used as given; the application prefixes it with a namespace of its own.
"""

import sys

from typing import Any

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError

from .cursor import CursorError
from .error_namespace import _namespaced


class DfViewSetError(HTTPException):
    """
    Base for every `HTTPException` this package raises on its own behalf. `code` is stored as given;
    the subclasses this module defines pass a code in the namespace `ERROR_NAMESPACE`.
    """

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
        super().__init__(404, f"Item with pk {pk} not found", _namespaced("not_found"), {"pk": pk})


class ReadOnlyCollectionError(DfViewSetError):
    """A write to a `CollectionViewSet` whose container is immutable (a tuple, a frozenset, a
    read-only mapping)."""

    def __init__(self):
        super().__init__(405, "This collection is read-only", _namespaced("read_only"))


class SessionExpiredError(DfViewSetError):
    def __init__(self):
        super().__init__(401, "Session expired or invalid", _namespaced("session_expired"))


class NotAuthorizedError(DfViewSetError):
    def __init__(self):
        super().__init__(403, "Not authorized to perform this action", _namespaced("not_authorized"))


class RateLimitedError(DfViewSetError):
    def __init__(self):
        super().__init__(429, "Rate limit exceeded", _namespaced("rate_limited"))


class ActionOutcomeUnknownError(DfViewSetError):
    """
    Base of the errors that end a call to a deferred action whose outcome the caller did not learn:
    the action may have run, may still run, or may never run. Not raised itself.
    """


class ResultTimeoutError(ActionOutcomeUnknownError):
    """A `celery_viewset` call whose result did not arrive from the worker within `timeout` seconds."""

    def __init__(self, task: str, timeout: float):
        message = f"No result for {task} within {timeout} s"
        super().__init__(504, message, _namespaced("result_timeout"), {"task": task, "timeout": timeout})


class ResultReaderStoppedError(ActionOutcomeUnknownError):
    """A `celery_viewset` call that was pending when the result reader of its `queue_key` stopped."""

    def __init__(self, queue_key: str):
        message = f"The result reader for {queue_key} stopped before the result arrived"
        super().__init__(503, message, _namespaced("result_reader_stopped"), {"queue_key": queue_key})


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
        super().__init__(503, message, _namespaced("action_dispatch_failed"), {"task": task, "reason": reason})


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
        super().__init__(status_code, message, _namespaced("action_interrupted"), {"task": task, "reason": reason})


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
        super().__init__(500, message, _namespaced("action_failed"), {"task": task, "reason": reason})


class IdempotencyKeyInvalidError(DfViewSetError):
    """An `Idempotency-Key` header that is not a single, non-empty Structured Field string."""

    def __init__(self):
        super().__init__(
            400, "Idempotency-Key must be a non-empty Structured Field string", _namespaced("idempotency_key_invalid")
        )


class IdempotencyKeyReusedError(DfViewSetError):
    """An `Idempotency-Key` already recorded for a call with a different method, path, query or body."""

    def __init__(self, key: str):
        message = f"Idempotency key {key} was used for a different request"
        super().__init__(422, message, _namespaced("idempotency_key_reused"), {"key": key})


class UnsupportedListShapeError(DfViewSetError):
    def __init__(self, shape: str, allowed: list[str]):
        message = f"unsupported list shape {shape!r}; this endpoint offers {', '.join(allowed)}"
        super().__init__(422, message, _namespaced("unsupported_list_shape"), {"shape": shape, "allowed": allowed})


class CursorRequestError(DfViewSetError):
    """Raised in place of a caught `CursorError`, carrying the same `code`/`params` as a 400 response."""

    def __init__(self, error: CursorError):
        super().__init__(400, str(error), error.code, error.params)


class EventTopicInvalidError(DfViewSetError):
    """A topic that is neither a viewset's routed base path nor a source name with an optional key."""

    def __init__(self, topic: str):
        message = f"Topic {topic} is neither a path starting with / nor a source name with an optional key"
        super().__init__(422, message, _namespaced("event_topic_invalid"), {"topic": topic})


class EventTopicRefusedError(DfViewSetError):
    """A topic no event authorizer admitted to the caller."""

    def __init__(self, topic: str):
        super().__init__(403, f"Topic {topic} is not admitted", _namespaced("event_topic_refused"), {"topic": topic})


class EventScopeMissingError(DfViewSetError):
    """A scoped viewset topic whose scope resolver returned None for the caller."""

    def __init__(self, topic: str):
        message = f"Topic {topic} has no event scope for this caller"
        super().__init__(403, message, _namespaced("event_scope_missing"), {"topic": topic})


class EventSubscriptionsExceededError(DfViewSetError):
    """A subscription on a socket that already holds `settings.viewsets_event_max_subscriptions` topics."""

    def __init__(self, limit: int):
        message = f"This socket already holds {limit} topics"
        super().__init__(409, message, _namespaced("event_subscriptions_exceeded"), {"limit": limit})


class EventCursorsExceededError(DfViewSetError):
    """A poll that carries more cursors than `settings.viewsets_event_max_subscriptions`."""

    def __init__(self, count: int, limit: int):
        message = f"A poll carries at most {limit} cursors, this one carries {count}"
        super().__init__(422, message, _namespaced("event_cursors_exceeded"), {"count": count, "limit": limit})


class UnhandledError(DfViewSetError):
    """The error an exception without a handler of its own is answered with. Its `detail` names no
    part of the exception; the exception is logged."""

    def __init__(self):
        super().__init__(500, "Internal Server Error", _namespaced("unhandled_error"))


class CommandInvalidError(DfViewSetError):
    """
    A muxws stream addressed to a viewset (its headers carry `:method` and `:path`) whose headers are
    not a command a route can be matched against. `reason`:

    - `address_missing`: `:method` or `:path` is absent
    - `address_type`: `:method` or `:path` is not a string
    - `path_relative`: `:path` does not start with `/`
    - `query_type`: `:query` is neither a string nor a mapping
    """

    _MESSAGES = {
        "address_missing": "The stream headers carry no :method or :path",
        "address_type": ":method and :path must both be strings",
        "path_relative": ":path must be an absolute path",
        "query_type": ":query must be a string or a mapping",
    }

    def __init__(self, reason: str):
        message = self._MESSAGES.get(reason, "The stream headers are not a viewset command")
        super().__init__(400, message, _namespaced("command_invalid"), {"reason": reason})


def _error_to_jsonable(error: BaseException) -> dict[str, Any]:
    """
    `error` as the JSON fields of an `HTTPException`: `status_code`, `detail` and `headers`, plus
    `class`, `code` and `params` for a `DfViewSetError`, its `code` the full code as given. A
    `RequestValidationError` is a 422 with its errors as `detail` and `class`
    `"RequestValidationError"`; any other error is the fields of an `UnhandledError`.

    The fields are read back by another process (a `celery_viewset` result) or later (an idempotency
    record) with `_error_from_jsonable`.
    """
    if isinstance(error, RequestValidationError):
        return {
            "status_code": 422,
            "detail": jsonable_encoder(error.errors()),
            "headers": None,
            "class": "RequestValidationError",
        }
    if not isinstance(error, HTTPException):
        error = UnhandledError()
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


def _error_from_jsonable(data: dict[str, Any]) -> Exception:
    """
    The exception `_error_to_jsonable` encoded. Fields with a `code` are a `DfViewSetError`, with
    `code` as given: of the class `class` names when this module defines a `DfViewSetError` subclass
    of that name, else of `DfViewSetError` itself, with the same fields either way. Fields without a
    `code` whose `class` is `"RequestValidationError"` are a `RequestValidationError` of the errors
    in `detail`. Any other fields are an `HTTPException`.
    """
    if "code" not in data:
        if data.get("class") == "RequestValidationError":
            return RequestValidationError(data["detail"])
        return HTTPException(status_code=data["status_code"], detail=data["detail"], headers=data.get("headers"))
    cls = getattr(sys.modules[__name__], data.get("class") or "", None)
    if not (isinstance(cls, type) and issubclass(cls, DfViewSetError)):
        cls = DfViewSetError
    return _rebuild_error(
        cls, data["status_code"], data["detail"], data["code"], data.get("params"), data.get("headers")
    )
