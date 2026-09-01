from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from fastapi_viewsets.exceptions import (
    CursorRequestError,
    df_viewset_exception_handler,
    DfViewSetError,
    NotAuthorizedError,
    NotFoundError,
    RateLimitedError,
    SessionExpiredError,
    UnsupportedListShapeError,
)


def test_not_found_error_carries_a_stable_code_and_the_pk_as_a_param():
    error = NotFoundError(5)
    assert error.status_code == 404
    assert error.detail == "Item with pk 5 not found"
    assert error.code == "not_found"
    assert error.params == {"pk": 5}


def test_session_expired_error():
    error = SessionExpiredError()
    assert error.status_code == 401
    assert error.detail == "Session expired or invalid"
    assert error.code == "session_expired"


def test_not_authorized_error():
    error = NotAuthorizedError()
    assert error.status_code == 403
    assert error.detail == "Not authorized to perform this action"
    assert error.code == "not_authorized"


def test_rate_limited_error():
    error = RateLimitedError()
    assert error.status_code == 429
    assert error.detail == "Rate limit exceeded"
    assert error.code == "rate_limited"


def test_unsupported_list_shape_error_carries_shape_and_allowed_as_params():
    error = UnsupportedListShapeError("cursor", ["plain", "paginated"])
    assert error.status_code == 422
    assert "cursor" in error.detail
    assert error.code == "unsupported_list_shape"
    assert error.params == {"shape": "cursor", "allowed": ["plain", "paginated"]}


def test_cursor_request_error_carries_the_wrapped_cursor_errors_code_and_params():
    from fastapi_viewsets.cursor import CursorError

    cause = CursorError(
        "cursor has no value for ordering key(s): id", code="cursor_missing_keys", params={"missing": ["id"]}
    )
    error = CursorRequestError(cause)
    assert error.status_code == 400
    assert error.detail == "cursor has no value for ordering key(s): id"
    assert error.code == "cursor_missing_keys"
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
        "detail_code": "not_found",
        "detail_params": {"pk": 5},
    }
