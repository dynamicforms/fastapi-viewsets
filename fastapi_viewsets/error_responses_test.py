import pickle

import pytest

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from fastapi_viewsets.collection_viewset import CollectionViewSet
from fastapi_viewsets.decorators import route_viewset
from fastapi_viewsets.error_namespace import ERROR_NAMESPACE_PYDANTIC
from fastapi_viewsets.error_responses import (
    ApiErrorBody,
    error_body,
    error_response,
    field_errors,
    install_error_handlers,
)
from fastapi_viewsets.exceptions import CommandInvalidError, DfViewSetError, NotFoundError, UnhandledError
from fastapi_viewsets.mixins import ViewSetMixin


def body(detail, code, params=None, fields=None):
    return {"detail": detail, "detail_code": code, "detail_params": params or {}, "field_errors": fields or {}}


UNHANDLED = body("Internal Server Error", "fastapi-viewsets.unhandled_error")

# error_body


def test_a_df_viewset_error_is_its_detail_code_and_params():
    assert error_body(NotFoundError(5)) == body("Item with pk 5 not found", "fastapi-viewsets.not_found", {"pk": 5})
    assert error_body(DfViewSetError(402, "short", "app.short", {"n": 1})) == body("short", "app.short", {"n": 1})


def test_an_http_exception_is_http_error_with_its_status():
    assert error_body(HTTPException(403, "Not your item")) == body(
        "Not your item", "fastapi-viewsets.http_error", {"status": 403}
    )
    assert error_body(StarletteHTTPException(404)) == body("Not Found", "fastapi-viewsets.http_error", {"status": 404})


def test_an_http_exception_with_a_detail_that_is_not_text_carries_it_in_params():
    assert error_body(HTTPException(400, {"field": "x"})) == body(
        "Bad Request", "fastapi-viewsets.http_error", {"status": 400, "detail": {"field": "x"}}
    )
    assert error_body(HTTPException(499, ["x"]))["detail"] == "HTTP 499"


def test_any_other_exception_is_unhandled_error_without_its_text():
    assert error_body(RuntimeError("secret")) == UNHANDLED
    assert error_body(UnhandledError()) == UNHANDLED


def test_a_validation_error_is_validation_failed_with_the_field_errors():
    errors = [
        {
            "type": "string_too_short",
            "loc": ("body", "name"),
            "msg": "too short",
            "input": "a",
            "ctx": {"min_length": 3},
        },
        {"type": "missing", "loc": ("body",), "msg": "Field required", "input": None},
        {"type": "int_parsing", "loc": ("body", "address", "zip"), "msg": "not an int", "input": "x"},
        {"type": "int_type", "loc": ("body", "items", 0), "msg": "first", "input": 1},
        {"type": "app.slug_taken", "loc": ("body", "items", 0), "msg": "second", "input": 1, "ctx": {"type": "x"}},
        {"type": "value_error", "loc": ("query", "q"), "msg": "Value error, bad", "ctx": {"error": ValueError("bad")}},
    ]
    assert error_body(RequestValidationError(errors)) == body(
        "The request failed validation",
        "fastapi-viewsets.validation_failed",
        fields={
            "name": [{"code": "pydantic.string_too_short", "params": {"min_length": 3}, "detail": "too short"}],
            "": [{"code": "pydantic.missing", "params": {}, "detail": "Field required"}],
            "address.zip": [{"code": "pydantic.int_parsing", "params": {}, "detail": "not an int"}],
            "items.0": [
                {"code": "pydantic.int_type", "params": {}, "detail": "first"},
                {
                    "code": "pydantic.custom_error",
                    "params": {"type": "app.slug_taken", "ctx": {"type": "x"}},
                    "detail": "second",
                },
            ],
            "q": [{"code": "pydantic.value_error", "params": {"error": "bad"}, "detail": "Value error, bad"}],
        },
    )


def test_pydantics_own_types_are_the_values_of_its_error_type_literal():
    from typing import get_args

    from pydantic_core.core_schema import ErrorType

    from fastapi_viewsets.error_responses import PYDANTIC_ERROR_TYPES

    assert PYDANTIC_ERROR_TYPES == frozenset(get_args(ErrorType))
    assert {"missing", "string_too_short", "value_error", "int_parsing"} <= PYDANTIC_ERROR_TYPES
    assert "custom_error" not in PYDANTIC_ERROR_TYPES


def test_a_pydantic_custom_error_is_custom_error_with_its_type_and_ctx_in_params():
    from pydantic import field_validator, ValidationError
    from pydantic_core import PydanticCustomError

    class Slug(BaseModel):
        slug: str

        @field_validator("slug")
        @classmethod
        def free(cls, value: str) -> str:
            raise PydanticCustomError("slug_taken", "Slug {slug} is taken", {"slug": value})

    with pytest.raises(ValidationError) as caught:
        Slug(slug="blue")
    errors = [{**error, "loc": ("body", *error["loc"])} for error in caught.value.errors()]
    assert field_errors(errors) == {
        "slug": [
            {
                "code": "pydantic.custom_error",
                "params": {"type": "slug_taken", "ctx": {"slug": "blue"}},
                "detail": "Slug blue is taken",
            }
        ]
    }


def test_field_errors_build_the_codes_from_the_pydantic_namespace_and_leave_out_the_input():
    [[entry]] = field_errors([{"type": "missing", "loc": ["body", "x"], "msg": "m", "input": "secret"}]).values()
    assert entry["code"] == f"{ERROR_NAMESPACE_PYDANTIC}.missing"
    assert "secret" not in str(entry)


def test_every_body_is_an_api_error_body():
    for error in (NotFoundError(1), HTTPException(400, "x"), RuntimeError(), RequestValidationError([])):
        ApiErrorBody.model_validate(error_body(error))


def test_error_response_keeps_the_headers_and_sends_no_body_where_the_status_allows_none():
    response = error_response(HTTPException(401, "who", headers={"WWW-Authenticate": "Bearer"}))
    assert (response.status_code, response.headers["www-authenticate"]) == (401, "Bearer")
    assert error_response(HTTPException(304)).body == b""
    assert error_response(RuntimeError()).status_code == 500
    assert error_response(RequestValidationError([])).status_code == 422


def test_the_new_errors_carry_their_codes_and_survive_pickling():
    error = CommandInvalidError("path_relative")
    assert (error.status_code, error.code, error.params, error.detail) == (
        400,
        "fastapi-viewsets.command_invalid",
        {"reason": "path_relative"},
        ":path must be an absolute path",
    )
    for original in (error, UnhandledError()):
        copy = pickle.loads(pickle.dumps(original))  # noqa: S301 - a round trip of this test's own object
        assert (type(copy), copy.code, copy.params) == (type(original), original.code, original.params)


# the routes of a viewset, without any registration


class Track(BaseModel):
    id: int
    title: str = Field(min_length=3)


cleanups: list[str] = []


def guard():
    try:
        yield
    except Exception as error:
        cleanups.append(type(error).__name__)
        raise


def viewset_router(error: Exception | None = None) -> APIRouter:
    router = APIRouter()

    @route_viewset(router, "/tracks", pk_field_name="id")
    class TrackViewSet(CollectionViewSet[int, Track], ViewSetMixin[int, Track]):
        __router = APIRouter()

        def __init__(self):
            super().__init__({1: Track(id=1, title="One")}, pk_field="id")

        @__router.get("/fail", dependencies=[Depends(guard)])
        async def fail(self) -> int:
            raise error

        @__router.get("/teapot")
        async def teapot(self, cups: int) -> int:  # noqa: ARG002 - a required query parameter
            raise HTTPException(418, "short and stout", headers={"x-pot": "1"})

    return router


def client(error: Exception | None = None, install: bool = False, **kwargs) -> TestClient:
    app = FastAPI()
    app.include_router(viewset_router(error))

    @app.get("/own")
    async def own():
        raise HTTPException(403, "Not your item")

    @app.get("/own-crash")
    async def own_crash():
        raise RuntimeError("secret")

    @app.get("/own-query")
    async def own_query(n: int):
        return n

    if install:
        install_error_handlers(app)
    return TestClient(app, **kwargs)


def test_a_viewset_error_is_answered_with_its_code():
    response = client().get("/tracks/9")
    assert (response.status_code, response.json()) == (
        404,
        body("Item with pk 9 not found", "fastapi-viewsets.not_found", {"pk": 9}),
    )


@pytest.mark.parametrize(
    ("method", "path", "json", "fields"),
    [
        ("post", "/tracks", {"title": "a"}, {"title": ["pydantic.string_too_short"]}),
        ("post", "/tracks", [1], {"": ["pydantic.model_attributes_type"]}),
        ("get", "/tracks/x", None, {"pk": ["pydantic.int_parsing"]}),
        ("get", "/tracks/teapot", None, {"cups": ["pydantic.missing"]}),
    ],
)
def test_a_request_that_fails_validation_is_answered_validation_failed(method, path, json, fields):
    response = getattr(client(), method)(path, **({"json": json} if json is not None else {}))
    assert response.status_code == 422
    assert response.json()["detail_code"] == "fastapi-viewsets.validation_failed"
    assert {name: [error["code"] for error in errors] for name, errors in response.json()["field_errors"].items()} == (
        fields
    )


def test_a_validation_error_the_endpoint_raises_is_answered_validation_failed():
    error = RequestValidationError([{"type": "unique", "loc": ("body", "title"), "msg": "taken"}])
    response = client(error).get("/tracks/fail")
    assert (response.status_code, response.json()["field_errors"]) == (
        422,
        {"title": [{"code": "pydantic.custom_error", "params": {"type": "unique", "ctx": {}}, "detail": "taken"}]},
    )


def test_an_http_exception_of_the_endpoint_is_answered_http_error_with_its_headers():
    response = client().get("/tracks/teapot", params={"cups": 1})
    assert (response.status_code, response.json(), response.headers["x-pot"]) == (
        418,
        body("short and stout", "fastapi-viewsets.http_error", {"status": 418}),
        "1",
    )


def test_an_unhandled_exception_is_answered_500_without_its_text_and_re_raised():
    response = client(RuntimeError("secret"), raise_server_exceptions=False).get("/tracks/fail")
    assert (response.status_code, response.json()) == (500, UNHANDLED)
    assert "secret" not in response.text
    with pytest.raises(RuntimeError, match="secret"):
        client(RuntimeError("secret")).get("/tracks/fail")


def test_a_dependency_with_yield_sees_the_exception_before_it_is_answered():
    cleanups.clear()
    client(HTTPException(409, "x")).get("/tracks/fail")
    client(RuntimeError("x"), raise_server_exceptions=False).get("/tracks/fail")
    assert cleanups == ["HTTPException", "RuntimeError"]


class _DomainError(Exception):
    pass


def test_the_handlers_the_application_registered_answer_as_it_wrote_them():
    app = FastAPI()
    app.include_router(viewset_router(_DomainError()))

    @app.exception_handler(_DomainError)
    async def domain(_request, _error):
        return JSONResponse({"domain": True}, status_code=409)

    assert TestClient(app).get("/tracks/fail").json() == {"domain": True}

    app = FastAPI()
    app.include_router(viewset_router())

    @app.exception_handler(StarletteHTTPException)
    async def own_http(_request, error):
        return JSONResponse({"own": error.detail}, status_code=error.status_code)

    assert TestClient(app).get("/tracks/9").json() == {"own": "Item with pk 9 not found"}

    app = FastAPI()
    app.include_router(viewset_router())
    app.add_exception_handler(404, own_http)
    assert TestClient(app).get("/tracks/9").json() == {"own": "Item with pk 9 not found"}

    app = FastAPI()
    app.include_router(viewset_router(RuntimeError("x")))

    @app.exception_handler(Exception)
    async def crashed(_request, _error):
        return JSONResponse({"crashed": True}, status_code=500)

    assert TestClient(app, raise_server_exceptions=False).get("/tracks/fail").json() == {"crashed": True}


# the application's other routes and Starlette's routing


def test_without_the_installer_the_other_routes_answer_as_fastapi_does():
    plain = client(raise_server_exceptions=False)
    assert plain.get("/own").json() == {"detail": "Not your item"}
    assert plain.get("/nope").json() == {"detail": "Not Found"}
    assert plain.post("/tracks/fail").json() == {"detail": "Method Not Allowed"}
    assert plain.get("/own-crash").text == "Internal Server Error"


def test_the_installer_answers_every_route_and_starlettes_routing_with_the_body():
    installed = client(install=True, raise_server_exceptions=False)
    assert installed.get("/own").json() == body("Not your item", "fastapi-viewsets.http_error", {"status": 403})
    response = installed.get("/own-query", params={"n": "x"})
    assert (response.status_code, list(response.json()["field_errors"])) == (422, ["n"])
    assert response.json()["detail_code"] == "fastapi-viewsets.validation_failed"
    response = installed.get("/own-crash")
    assert (response.status_code, response.json()) == (500, UNHANDLED)
    assert installed.get("/tracks/9").json()["detail_code"] == "fastapi-viewsets.not_found"


def test_starlettes_404_and_405_are_http_error_with_their_status():
    installed = client(install=True)
    not_found = installed.get("/nope")
    assert (not_found.status_code, not_found.json()) == (
        404,
        body("Not Found", "fastapi-viewsets.http_error", {"status": 404}),
    )
    not_allowed = installed.post("/tracks/fail")
    assert (not_allowed.status_code, not_allowed.json()) == (
        405,
        body("Method Not Allowed", "fastapi-viewsets.http_error", {"status": 405}),
    )
    assert "GET" in not_allowed.headers["allow"]


def test_the_installer_re_raises_an_unhandled_exception_for_the_server():
    with pytest.raises(RuntimeError, match="secret"):
        client(install=True).get("/own-crash")


def test_a_handler_registered_after_the_installer_replaces_its_handler():
    test_client = client(install=True)

    @test_client.app.exception_handler(StarletteHTTPException)
    async def own_http(_request, error):
        return JSONResponse({"own": error.detail}, status_code=error.status_code)

    assert test_client.get("/own").json() == {"own": "Not your item"}


# OpenAPI


def openapi() -> dict:
    app = FastAPI()
    app.include_router(viewset_router())
    return app.openapi()


def test_every_operation_of_a_viewset_documents_its_error_responses_with_the_body():
    document = openapi()
    ref = {"$ref": "#/components/schemas/ApiErrorBody"}
    operations = [operation for item in document["paths"].values() for operation in item.values()]
    assert operations
    for operation in operations:
        responses = operation["responses"]
        assert {"422", "500", "default"} <= set(responses)
        for status in ("422", "500", "default"):
            content = responses[status]["content"]["application/json"]
            assert content["schema"] == ref
            assert content["examples"]
            assert responses[status]["description"]
    assert document["paths"]["/tracks/{pk}"]["get"]["responses"]["404"]["content"]["application/json"]["schema"] == ref
    example = document["paths"]["/tracks"]["post"]["responses"]["422"]["content"]["application/json"]["examples"]
    assert example["validation_failed"]["value"]["detail_code"] == "fastapi-viewsets.validation_failed"


def test_the_error_schemas_are_named_and_describe_every_field():
    schemas = openapi()["components"]["schemas"]
    assert "HTTPValidationError" not in schemas
    assert "ValidationError" not in schemas
    envelope, entry = schemas["ApiErrorBody"], schemas["ApiFieldError"]
    assert set(envelope["required"]) == {"detail", "detail_code", "detail_params", "field_errors"}
    assert set(entry["required"]) == {"code", "params", "detail"}
    for schema in (envelope, entry):
        assert schema["description"]
        assert all(field["description"] for field in schema["properties"].values())
    assert envelope["properties"]["field_errors"]["additionalProperties"]["items"] == {
        "$ref": "#/components/schemas/ApiFieldError"
    }


def test_the_schema_endpoint_documents_the_error_responses_too():
    app = FastAPI()
    app.include_router(viewset_router())
    document = TestClient(app).get("/tracks/schema").json()
    assert "ApiErrorBody" in document["components"]["schemas"]
    assert "HTTPValidationError" not in document["components"]["schemas"]


def test_an_unhandled_exception_of_a_viewset_route_with_the_installer_is_answered_once():
    response = client(RuntimeError("secret"), install=True, raise_server_exceptions=False).get("/tracks/fail")
    assert (response.status_code, response.json()) == (500, UNHANDLED)
