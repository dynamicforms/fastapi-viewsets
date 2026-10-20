import pickle
import re

from pathlib import Path

import pytest

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

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
    df_viewset_exception_handler,
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
    UnsupportedListShapeError,
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


def _app_with_handler_registered() -> FastAPI:
    router = APIRouter()

    @router.get("/boom")
    async def boom():
        raise NotFoundError(5)

    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(DfViewSetError, df_viewset_exception_handler)
    return app


def _app_without_handler_registered() -> FastAPI:
    router = APIRouter()

    @router.get("/boom")
    async def boom():
        raise NotFoundError(5)

    app = FastAPI()
    app.include_router(router)
    return app


def test_without_the_handler_the_response_is_exactly_a_plain_detail_string():
    """An application that does nothing further sees exactly what it always has."""
    client = TestClient(_app_without_handler_registered())
    response = client.get("/boom")
    assert response.status_code == 404
    assert response.json() == {"detail": "Item with pk 5 not found"}


def test_with_the_handler_registered_detail_code_and_params_are_additive():
    client = TestClient(_app_with_handler_registered())
    response = client.get("/boom")
    assert response.status_code == 404
    assert response.json() == {
        "detail": "Item with pk 5 not found",
        "detail_code": "fastapi-viewsets.not_found",
        "detail_params": {"pk": 5},
    }


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
    }


def test_the_encoded_fields_carry_a_code_of_the_package_namespace_without_it():
    assert _error_to_jsonable(NotFoundError(3))["code"] == "not_found"
    assert _error_to_jsonable(CursorRequestError(CursorError("x", code=_namespaced("cursor_stale"))))["code"] == (
        "cursor_stale"
    )


def test_encoded_fields_without_the_namespace_rebuild_an_error_of_this_module_with_it():
    data = {"status_code": 404, "detail": "Item with pk 3 not found", "headers": None}
    rebuilt = _error_from_jsonable({**data, "class": "NotFoundError", "code": "not_found", "params": {"pk": 3}})
    assert (type(rebuilt), rebuilt.code) == (NotFoundError, "fastapi-viewsets.not_found")
    rebuilt = _error_from_jsonable({**data, "class": "NotFoundError", "code": "fastapi-viewsets.not_found"})
    assert rebuilt.code == "fastapi-viewsets.not_found"


class _ApplicationError(DfViewSetError):
    def __init__(self, code: str):
        super().__init__(409, "conflict", code, {"n": 1})


class _ApplicationNotFoundError(NotFoundError):
    pass


@pytest.mark.parametrize("code", ["app.conflict", "conflict", "app.not_found"])
def test_the_code_of_an_application_error_is_encoded_and_rebuilt_as_given(code):
    encoded = _error_to_jsonable(_ApplicationError(code))
    assert encoded["code"] == code
    rebuilt = _error_from_jsonable(encoded)
    assert (type(rebuilt), rebuilt.code, rebuilt.params) == (DfViewSetError, code, {"n": 1})
    base = _error_from_jsonable(_error_to_jsonable(DfViewSetError(400, "bad", code)))
    assert base.code == code


def test_an_application_subclass_of_an_error_of_this_module_travels_without_the_namespace():
    # a 0.11 reader reads the code it wrote itself
    encoded = _error_to_jsonable(_ApplicationNotFoundError(3))
    assert (encoded["class"], encoded["code"]) == ("_ApplicationNotFoundError", "not_found")
    rebuilt = _error_from_jsonable(encoded)
    assert (type(rebuilt), rebuilt.code) == (DfViewSetError, "fastapi-viewsets.not_found")


def test_a_record_without_the_namespace_of_a_class_the_reader_does_not_define_gains_it():
    # a 0.11 record of an application subclass, and a class a later version defines, with a code this
    # version declares
    for class_name in ("MyNotFound", "NotFoundInALaterVersionError"):
        data = {"status_code": 404, "detail": "x", "headers": None, "class": class_name, "code": "not_found"}
        rebuilt = _error_from_jsonable(data)
        assert (type(rebuilt), rebuilt.code) == (DfViewSetError, "fastapi-viewsets.not_found")


def test_a_code_this_version_does_not_declare_is_read_as_given():
    data = {"status_code": 409, "detail": "x", "headers": None, "class": "LaterError", "code": "later_code"}
    assert _error_from_jsonable(data).code == "later_code"
    # a code in the package namespace that this version does not declare loses the namespace on the way
    encoded = _error_to_jsonable(_ApplicationError("fastapi-viewsets.later_code"))
    assert (encoded["code"], _error_from_jsonable(encoded).code) == ("later_code", "later_code")


def test_an_application_code_without_a_namespace_that_equals_a_package_code_is_read_as_the_packages():
    rebuilt = _error_from_jsonable(_error_to_jsonable(_ApplicationError("not_found")))
    assert rebuilt.code == "fastapi-viewsets.not_found"


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
