import pickle
import re

from pathlib import Path

import pytest

from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError

from fastapi_viewsets import ERROR_NAMESPACE, ERROR_NAMESPACE_PYDANTIC
from fastapi_viewsets.cursor import CursorError
from fastapi_viewsets.error_namespace import _DECLARED_CODES, _namespaced
from fastapi_viewsets.exceptions import (
    _error_from_jsonable,
    _error_to_jsonable,
    ActionDispatchError,
    ActionFailedError,
    ActionInterruptedError,
    ActionOutcomeUnknownError,
    CursorRequestError,
    DfViewSetError,
    EventCursorsExceededError,
    EventScopeMissingError,
    EventSubscriptionsExceededError,
    EventTopicInvalidError,
    EventTopicRefusedError,
    IdempotencyKeyInvalidError,
    IdempotencyKeyReusedError,
    NotAuthorizedError,
    NotFoundError,
    RateLimitedError,
    ReadOnlyCollectionError,
    ResultReaderStoppedError,
    ResultTimeoutError,
    SessionExpiredError,
    UnhandledError,
    UnsupportedListShapeError,
    UnsupportedListTypeError,
)


def test_not_found_error_carries_a_stable_code_and_the_pk_as_a_param():
    error = NotFoundError(5)
    assert error.status_code == 404
    assert error.detail == "Item with pk 5 not found"
    assert error.code == "fastapi-viewsets.not_found"
    assert error.params == {"pk": 5}


def test_session_expired_error():
    error = SessionExpiredError()
    assert error.status_code == 401
    assert error.detail == "Session expired or invalid"
    assert error.code == "fastapi-viewsets.session_expired"


def test_not_authorized_error():
    error = NotAuthorizedError()
    assert error.status_code == 403
    assert error.detail == "Not authorized to perform this action"
    assert error.code == "fastapi-viewsets.not_authorized"


def test_rate_limited_error():
    error = RateLimitedError()
    assert error.status_code == 429
    assert error.detail == "Rate limit exceeded"
    assert error.code == "fastapi-viewsets.rate_limited"


def test_idempotency_key_invalid_error():
    error = IdempotencyKeyInvalidError()
    assert error.status_code == 400
    assert error.detail == "Idempotency-Key must be a non-empty Structured Field string"
    assert error.code == "fastapi-viewsets.idempotency_key_invalid"


def test_idempotency_key_reused_error_carries_the_key_as_param():
    error = IdempotencyKeyReusedError("k1")
    assert error.status_code == 422
    assert error.detail == "Idempotency key k1 was used for a different request"
    assert error.code == "fastapi-viewsets.idempotency_key_reused"
    assert error.params == {"key": "k1"}


def test_result_timeout_error_carries_task_and_timeout_as_params():
    error = ResultTimeoutError("items.list_items", 3600.0)
    assert error.status_code == 504
    assert error.detail == "No result for items.list_items within 3600.0 s"
    assert error.code == "fastapi-viewsets.result_timeout"
    assert error.params == {"task": "items.list_items", "timeout": 3600.0}


def test_result_reader_stopped_error_carries_queue_key_as_param():
    error = ResultReaderStoppedError("celery_viewset_results:items")
    assert error.status_code == 503
    assert error.detail == "The result reader for celery_viewset_results:items stopped before the result arrived"
    assert error.code == "fastapi-viewsets.result_reader_stopped"
    assert error.params == {"queue_key": "celery_viewset_results:items"}


def test_unsupported_list_shape_error_carries_shape_and_allowed_as_params():
    error = UnsupportedListShapeError("cursor", ["plain", "paginated"])
    assert error.status_code == 422
    assert "cursor" in error.detail
    assert error.code == "fastapi-viewsets.unsupported_list_shape"
    assert error.params == {"shape": "cursor", "allowed": ["plain", "paginated"]}


def test_unsupported_list_type_error_carries_type_and_allowed_as_params():
    error = UnsupportedListTypeError("full", ["Track", "compact"])
    assert error.status_code == 422
    assert "full" in error.detail
    assert error.code == "fastapi-viewsets.unsupported_list_type"
    assert error.params == {"type": "full", "allowed": ["Track", "compact"]}


def test_cursor_request_error_carries_the_wrapped_cursor_errors_code_and_params():
    from fastapi_viewsets.cursor import CursorError

    cause = CursorError(
        "cursor has no value for ordering key(s): id",
        code="fastapi-viewsets.cursor_missing_keys",
        params={"missing": ["id"]},
    )
    error = CursorRequestError(cause)
    assert error.status_code == 400
    assert error.detail == "cursor has no value for ordering key(s): id"
    assert error.code == "fastapi-viewsets.cursor_missing_keys"
    assert error.params == {"missing": ["id"]}


def test_event_topic_invalid_error_carries_the_topic():
    error = EventTopicInvalidError("Not A Topic")
    assert (error.status_code, error.code, error.params) == (
        422,
        "fastapi-viewsets.event_topic_invalid",
        {"topic": "Not A Topic"},
    )
    assert error.detail == "Topic Not A Topic is neither a path starting with / nor a source name with an optional key"


def test_event_topic_refused_error_carries_the_topic():
    error = EventTopicRefusedError("org:9")
    assert (error.status_code, error.code, error.params) == (
        403,
        "fastapi-viewsets.event_topic_refused",
        {"topic": "org:9"},
    )
    assert error.detail == "Topic org:9 is not admitted"


def test_event_subscriptions_exceeded_error_carries_the_limit():
    error = EventSubscriptionsExceededError(256)
    assert (error.status_code, error.code, error.params) == (
        409,
        "fastapi-viewsets.event_subscriptions_exceeded",
        {"limit": 256},
    )
    assert error.detail == "This socket already holds 256 topics"


def test_event_cursors_exceeded_error_carries_the_count_and_the_limit():
    error = EventCursorsExceededError(300, 256)
    assert (error.status_code, error.code, error.params) == (
        422,
        "fastapi-viewsets.event_cursors_exceeded",
        {"count": 300, "limit": 256},
    )
    assert error.detail == "A poll carries at most 256 cursors, this one carries 300"


def test_action_dispatch_error_carries_task_and_reason():
    error = ActionDispatchError("items.create", "revoked")
    assert (error.status_code, error.code) == (503, "fastapi-viewsets.action_dispatch_failed")
    assert error.params == {"task": "items.create", "reason": "revoked"}


@pytest.mark.parametrize(
    ("reason", "status", "detail"),
    [
        ("time_limit", 504, "items.create exceeded its time limit"),
        ("terminated", 503, "items.create was terminated"),
        ("lost", 503, "items.create lost the process executing it"),
    ],
)
def test_action_interrupted_error_takes_its_status_from_the_reason(reason, status, detail):
    error = ActionInterruptedError("items.create", reason)
    assert (error.status_code, error.detail, error.code) == (status, detail, "fastapi-viewsets.action_interrupted")
    assert error.params == {"task": "items.create", "reason": reason}


def test_action_failed_error_names_the_task_only():
    error = ActionFailedError("items.create", "result")
    assert (error.status_code, error.detail, error.code) == (
        500,
        "items.create failed",
        "fastapi-viewsets.action_failed",
    )
    assert error.params == {"task": "items.create", "reason": "result"}


def test_the_errors_of_a_call_whose_outcome_is_unknown_share_a_base():
    assert isinstance(ResultTimeoutError("t", 1.0), ActionOutcomeUnknownError)
    assert isinstance(ResultReaderStoppedError("q"), ActionOutcomeUnknownError)


def test_a_df_viewset_error_survives_pickling_with_its_class_and_fields():
    error = ActionInterruptedError("items.create", "lost")
    error.headers = {"Retry-After": "1"}
    copy = pickle.loads(pickle.dumps(error))  # noqa: S301 - a round trip of this test's own object
    assert type(copy) is ActionInterruptedError
    assert (copy.status_code, copy.detail, copy.code, copy.params, copy.headers) == (
        503,
        error.detail,
        "fastapi-viewsets.action_interrupted",
        error.params,
        {"Retry-After": "1"},
    )


def test_the_encoded_fields_rebuild_the_error():
    rebuilt = _error_from_jsonable(_error_to_jsonable(NotFoundError(3)))
    assert type(rebuilt) is NotFoundError
    assert (rebuilt.status_code, rebuilt.code, rebuilt.params) == (404, "fastapi-viewsets.not_found", {"pk": 3})
    assert _error_to_jsonable(RuntimeError("x")) == {
        "status_code": 500,
        "detail": "Internal Server Error",
        "headers": None,
        "class": "UnhandledError",
        "code": "fastapi-viewsets.unhandled_error",
        "params": {},
    }
    rebuilt = _error_from_jsonable(_error_to_jsonable(RuntimeError("x")))
    assert (type(rebuilt), rebuilt.code) == (UnhandledError, "fastapi-viewsets.unhandled_error")


_ERRORS = [
    {"type": "string_too_short", "loc": ["body", "name"], "msg": "short", "input": "a", "ctx": {"min_length": 3}}
]


def test_a_request_validation_error_is_encoded_with_its_class_and_rebuilt_as_one():
    encoded = _error_to_jsonable(RequestValidationError(_ERRORS))
    assert encoded == {"status_code": 422, "detail": _ERRORS, "headers": None, "class": "RequestValidationError"}
    rebuilt = _error_from_jsonable(encoded)
    assert type(rebuilt) is RequestValidationError
    assert list(rebuilt.errors()) == _ERRORS


@pytest.mark.parametrize(
    "data",
    [
        {"status_code": 422, "detail": _ERRORS, "headers": None},
        {"status_code": 500, "detail": "Internal Server Error", "headers": None},
        {"status_code": 403, "detail": "Not your item", "headers": {"X-A": "1"}},
    ],
)
def test_fields_without_a_code_or_a_class_rebuild_an_http_exception(data):
    rebuilt = _error_from_jsonable(data)
    assert type(rebuilt) is HTTPException
    assert (rebuilt.status_code, rebuilt.detail, rebuilt.headers) == (
        data["status_code"],
        data["detail"],
        data["headers"],
    )


def test_the_encoded_fields_carry_the_full_code():
    assert _error_to_jsonable(NotFoundError(3))["code"] == "fastapi-viewsets.not_found"
    assert _error_to_jsonable(CursorRequestError(CursorError("x", code=_namespaced("cursor_stale"))))["code"] == (
        "fastapi-viewsets.cursor_stale"
    )


def test_encoded_fields_rebuild_an_error_of_this_module_with_its_code_as_given():
    data = {"status_code": 404, "detail": "Item with pk 3 not found", "headers": None, "params": {"pk": 3}}
    rebuilt = _error_from_jsonable({**data, "class": "NotFoundError", "code": "fastapi-viewsets.not_found"})
    assert (type(rebuilt), rebuilt.code, rebuilt.params) == (NotFoundError, "fastapi-viewsets.not_found", {"pk": 3})
    # a bare code, as 0.11 wrote it, is read as given
    assert _error_from_jsonable({**data, "class": "NotFoundError", "code": "not_found"}).code == "not_found"


class _ApplicationError(DfViewSetError):
    def __init__(self, code: str):
        super().__init__(409, "conflict", code, {"n": 1})


class _ApplicationNotFoundError(NotFoundError):
    pass


@pytest.mark.parametrize("code", ["app.conflict", "conflict", "not_found", "fastapi-viewsets.later_code"])
def test_the_code_of_an_application_error_is_encoded_and_rebuilt_as_given(code):
    encoded = _error_to_jsonable(_ApplicationError(code))
    assert encoded["code"] == code
    rebuilt = _error_from_jsonable(encoded)
    assert (type(rebuilt), rebuilt.code, rebuilt.params) == (DfViewSetError, code, {"n": 1})
    base = _error_from_jsonable(_error_to_jsonable(DfViewSetError(400, "bad", code)))
    assert base.code == code


def test_an_application_subclass_of_an_error_of_this_module_is_rebuilt_as_a_df_viewset_error():
    encoded = _error_to_jsonable(_ApplicationNotFoundError(3))
    assert (encoded["class"], encoded["code"]) == ("_ApplicationNotFoundError", "fastapi-viewsets.not_found")
    rebuilt = _error_from_jsonable(encoded)
    assert (type(rebuilt), rebuilt.code) == (DfViewSetError, "fastapi-viewsets.not_found")


def test_the_declared_codes_are_the_codes_the_source_builds():
    root = Path(__file__).parent
    built = {
        match
        for path in root.rglob("*.py")
        if not path.name.endswith("_test.py")
        for match in re.findall(r'_namespaced\("([a-z0-9_]+)"\)', path.read_text())
    }
    assert built == _DECLARED_CODES


def test_the_package_exports_the_namespaces_of_its_error_codes():
    assert (ERROR_NAMESPACE, ERROR_NAMESPACE_PYDANTIC) == ("fastapi-viewsets", "pydantic")


def test_every_error_this_module_defines_carries_a_full_code_of_the_package_namespace():
    errors = [
        NotFoundError(1),
        ReadOnlyCollectionError(),
        SessionExpiredError(),
        NotAuthorizedError(),
        RateLimitedError(),
        ResultTimeoutError("t", 1.0),
        ResultReaderStoppedError("q"),
        ActionDispatchError("t"),
        ActionInterruptedError("t", "lost"),
        ActionFailedError("t"),
        IdempotencyKeyInvalidError(),
        IdempotencyKeyReusedError("k"),
        UnsupportedListShapeError("x", ["plain"]),
        UnsupportedListTypeError("x", ["Track"]),
        EventTopicInvalidError("t"),
        EventTopicRefusedError("t"),
        EventScopeMissingError("t"),
        EventSubscriptionsExceededError(1),
        EventCursorsExceededError(2, 1),
    ]
    for error in errors:
        namespace, _, code = error.code.rpartition(".")
        assert namespace == ERROR_NAMESPACE
        assert re.fullmatch(r"[a-z0-9]+(_[a-z0-9]+)*", code), error.code
