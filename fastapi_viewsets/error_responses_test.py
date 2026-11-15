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
    error_status,
    field_errors,
    FieldNames,
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
        {"type": "json_invalid", "loc": ("body", 3), "msg": "Invalid JSON", "ctx": {"error": "EOF"}},
        {"type": "slug_taken", "loc": ("body", "slug"), "msg": "taken", "ctx": {"slug": "blue"}},
    ]
    assert error_body(RequestValidationError(errors)) == body(
        "The request failed validation",
        "fastapi-viewsets.validation_failed",
        fields={
            "name": [{"code": "fastapi-viewsets.min_length", "params": {"minLength": 3}, "detail": "too short"}],
            "": [{"code": "fastapi-viewsets.required", "params": {}, "detail": "Field required"}],
            "address.zip": [{"code": "fastapi-viewsets.invalid_integer", "params": {}, "detail": "not an int"}],
            "items.0": [
                {"code": "fastapi-viewsets.invalid_integer", "params": {}, "detail": "first"},
                {"code": "app.slug_taken", "params": {"type": "x"}, "detail": "second"},
            ],
            "q": [{"code": "fastapi-viewsets.invalid", "params": {}, "detail": "Value error, bad"}],
            "3": [{"code": "pydantic.json_invalid", "params": {"error": "EOF"}, "detail": "Invalid JSON"}],
            "slug": [
                {
                    "code": "pydantic.custom_error",
                    "params": {"type": "slug_taken", "ctx": {"slug": "blue"}},
                    "detail": "taken",
                }
            ],
        },
    )


def test_pydantics_own_types_are_the_values_of_its_error_type_literal():
    from typing import get_args

    from pydantic_core.core_schema import ErrorType

    from fastapi_viewsets.error_responses import PYDANTIC_ERROR_TYPES

    assert PYDANTIC_ERROR_TYPES == frozenset(get_args(ErrorType))
    assert {"missing", "string_too_short", "value_error", "int_parsing"} <= PYDANTIC_ERROR_TYPES
    assert "custom_error" not in PYDANTIC_ERROR_TYPES


# The field error vocabulary


def test_the_vocabulary_is_declared_with_the_package_namespace():
    from fastapi_viewsets import FIELD_ERROR_PARAMS
    from fastapi_viewsets.error_namespace import _DECLARED_CODES, _namespaced

    assert all(code.startswith("fastapi-viewsets.") for code in FIELD_ERROR_PARAMS)
    assert set(FIELD_ERROR_PARAMS) <= {_namespaced(code) for code in _DECLARED_CODES}
    with pytest.raises(TypeError):
        FIELD_ERROR_PARAMS["fastapi-viewsets.x"] = ()  # type: ignore[index]


def test_every_mapped_type_and_code_reads_exactly_the_params_its_code_declares():
    from fastapi_viewsets.error_responses import _DJANGO_FIELD_ERRORS, _PYDANTIC_FIELD_ERRORS, FIELD_ERROR_PARAMS

    for code, sources in [*_PYDANTIC_FIELD_ERRORS.values(), *_DJANGO_FIELD_ERRORS.values()]:
        assert tuple(sources) == FIELD_ERROR_PARAMS[code], code


def test_every_declared_pydantic_type_is_either_mapped_or_forwarded():
    from fastapi_viewsets.error_responses import _PYDANTIC_FIELD_ERRORS, PYDANTIC_ERROR_TYPES

    forwarded = {
        "no_such_attribute", "json_invalid", "json_type", "needs_python_object", "recursion_loop", "frozen_field",
        "frozen_instance", "extra_forbidden", "invalid_key", "get_attribute_error", "default_factory_not_called",
        "iteration_error", "string_not_ascii", "set_item_not_hashable", "bytes_invalid_encoding",
        "missing_sentinel_error", "ellipsis_error", "timezone_naive", "timezone_aware", "timezone_offset",
        "union_tag_invalid", "union_tag_not_found", "unexpected_keyword_argument", "unexpected_positional_argument",
        "multiple_argument_values", "url_scheme", "uuid_version", "complex_type", "complex_str_parsing",
    }  # fmt: skip
    assert set(_PYDANTIC_FIELD_ERRORS).isdisjoint(forwarded)
    assert set(_PYDANTIC_FIELD_ERRORS) | forwarded == PYDANTIC_ERROR_TYPES


def test_the_vocabulary_agrees_with_the_list_the_ts_package_is_tested_against():
    import json
    import pathlib

    from fastapi_viewsets import FIELD_ERROR_PARAMS

    path = pathlib.Path(__file__).parent.parent / "vue" / "field-error-codes.json"
    listed = json.loads(path.read_text())
    assert list(listed.items()) == [(code, list(params)) for code, params in FIELD_ERROR_PARAMS.items()]


def _validation_errors(model, data):
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as caught:
        model.model_validate(data)
    return [{**error, "loc": ("body", *error["loc"])} for error in caught.value.errors()]


def test_pydantic_constraints_become_field_error_codes_with_normalised_params():
    import datetime

    from decimal import Decimal
    from typing import Literal

    class Limits(BaseModel):
        short: str = Field(min_length=3)
        long: list[int] = Field(max_length=1)
        low: int = Field(ge=1)
        above: int = Field(gt=1)
        high: datetime.date = Field(le=datetime.date(2000, 1, 1))
        below: float = Field(lt=1.5)
        step: int = Field(multiple_of=5)
        digits: Decimal = Field(max_digits=3)
        places: Decimal = Field(decimal_places=1)
        whole: Decimal = Field(max_digits=3, decimal_places=2)
        slug: str = Field(pattern="^[a-z]+$")
        size: Literal["s", "m"]

    data = {
        "short": "ab",
        "long": [1, 2],
        "low": 0,
        "above": 1,
        "high": "2001-01-01",
        "below": 2,
        "step": 3,
        "digits": "1234",
        "places": "1.25",
        "whole": "12.0",
        "slug": "A",
        "size": "xl",
    }
    found = {name: entry for name, (entry,) in field_errors(_validation_errors(Limits, data)).items()}
    assert {name: (entry["code"], entry["params"]) for name, entry in found.items()} == {
        "short": ("fastapi-viewsets.min_length", {"minLength": 3}),
        "long": ("fastapi-viewsets.max_length", {"maxLength": 1}),
        "low": ("fastapi-viewsets.min_value", {"minValue": 1}),
        "above": ("fastapi-viewsets.min_value_exclusive", {"minValue": 1}),
        "high": ("fastapi-viewsets.max_value", {"maxValue": "2000-01-01"}),
        "below": ("fastapi-viewsets.max_value_exclusive", {"maxValue": 1.5}),
        "step": ("fastapi-viewsets.multiple_of", {"multipleOf": 5}),
        "digits": ("fastapi-viewsets.max_digits", {"maxDigits": 3}),
        "places": ("fastapi-viewsets.max_decimal_places", {"maxDecimalPlaces": 1}),
        "whole": ("fastapi-viewsets.max_whole_digits", {"maxWholeDigits": 1}),
        "slug": ("fastapi-viewsets.pattern", {"pattern": "^[a-z]+$"}),
        "size": ("fastapi-viewsets.in_allowed_values", {"allowedAsText": "'s' or 'm'"}),
    }
    assert found["short"]["detail"] == "String should have at least 3 characters"


_EXPECTED_PYDANTIC_CODES = {
    "required": ("missing", "missing_argument", "missing_keyword_only_argument", "missing_positional_only_argument"),
    "invalid_integer": ("int_type", "int_parsing", "int_parsing_size", "int_from_float"),
    "invalid_number": (
        "float_type", "float_parsing", "finite_number", "decimal_type", "decimal_parsing", "fraction_type",
        "fraction_parsing",
    ),
    "invalid_boolean": ("bool_type", "bool_parsing"),
    "invalid_date": ("date_type", "date_parsing", "date_from_datetime_parsing", "date_from_datetime_inexact"),
    "invalid_time": ("time_type", "time_parsing"),
    "invalid_datetime": ("datetime_type", "datetime_parsing", "datetime_object_invalid", "datetime_from_date_parsing"),
    "invalid_duration": ("time_delta_type", "time_delta_parsing"),
    "invalid_url": ("url_type", "url_parsing", "url_syntax_violation"),
    "invalid_uuid": ("uuid_type", "uuid_parsing"),
    "past": ("date_past", "datetime_past"),
    "future": ("date_future", "datetime_future"),
    "invalid_type": (
        "string_type", "string_unicode", "bytes_type", "dict_type", "frozen_dict_type", "ordered_dict_type",
        "counter_type", "list_type", "tuple_type", "set_type", "frozen_set_type", "deque_type", "mapping_type",
        "iterable_type", "model_type", "model_attributes_type", "dataclass_type", "dataclass_exact_type",
        "named_tuple_type", "none_required", "arguments_type", "callable_type", "is_instance_of", "is_subclass_of",
    ),
    "invalid": ("value_error", "assertion_error"),
}  # fmt: skip

_EXPECTED_PYDANTIC_CODES_WITH_PARAMS = [
    ("string_too_short", {"min_length": 2}, "min_length", {"minLength": 2}),
    ("too_short", {"min_length": 2}, "min_length", {"minLength": 2}),
    ("bytes_too_short", {"min_length": 2}, "min_length", {"minLength": 2}),
    ("string_too_long", {"max_length": 2}, "max_length", {"maxLength": 2}),
    ("too_long", {"max_length": 2}, "max_length", {"maxLength": 2}),
    ("bytes_too_long", {"max_length": 2}, "max_length", {"maxLength": 2}),
    ("url_too_long", {"max_length": 2}, "max_length", {"maxLength": 2}),
    ("greater_than_equal", {"ge": 1}, "min_value", {"minValue": 1}),
    ("greater_than", {"gt": 1}, "min_value_exclusive", {"minValue": 1}),
    ("less_than_equal", {"le": 1}, "max_value", {"maxValue": 1}),
    ("less_than", {"lt": 1}, "max_value_exclusive", {"maxValue": 1}),
    ("multiple_of", {"multiple_of": 3}, "multiple_of", {"multipleOf": 3}),
    ("decimal_max_digits", {"max_digits": 4}, "max_digits", {"maxDigits": 4}),
    ("decimal_max_places", {"decimal_places": 1}, "max_decimal_places", {"maxDecimalPlaces": 1}),
    ("decimal_whole_digits", {"whole_digits": 2}, "max_whole_digits", {"maxWholeDigits": 2}),
    ("string_pattern_mismatch", {"pattern": "^a$"}, "pattern", {"pattern": "^a$"}),
    ("enum", {"expected": "'a' or 'b'"}, "in_allowed_values", {"allowedAsText": "'a' or 'b'"}),
    ("literal_error", {"expected": "'a' or 'b'"}, "in_allowed_values", {"allowedAsText": "'a' or 'b'"}),
]


def test_every_mapped_pydantic_type_becomes_its_documented_code():
    from fastapi_viewsets.error_responses import _PYDANTIC_FIELD_ERRORS

    expected = {error_type: (code, {}, {}) for code, types in _EXPECTED_PYDANTIC_CODES.items() for error_type in types}
    expected |= {
        error_type: (code, ctx, params) for error_type, ctx, code, params in _EXPECTED_PYDANTIC_CODES_WITH_PARAMS
    }
    assert set(expected) == set(_PYDANTIC_FIELD_ERRORS)
    for error_type, (code, ctx, params) in expected.items():
        entry = {"type": error_type, "loc": ("body", "f"), "msg": "m", "ctx": ctx, "input": 1}
        assert field_errors([entry]) == {
            "f": [{"code": f"fastapi-viewsets.{code}", "params": params, "detail": "m"}]
        }, error_type


def test_a_null_for_a_value_of_a_type_or_a_choice_is_required():
    from typing import Literal

    from pydantic import field_validator

    class Nulls(BaseModel):
        count: int
        size: Literal["s", "m"]
        name: str
        checked: str

        @field_validator("checked", mode="before")
        @classmethod
        def not_null(cls, value):
            if value is None:
                raise ValueError("say something")
            return value

    found = field_errors(_validation_errors(Nulls, {"count": None, "size": None, "name": None, "checked": None}))
    assert {name: [entry["code"] for entry in entries] for name, entries in found.items()} == {
        "count": ["fastapi-viewsets.required"],
        "size": ["fastapi-viewsets.required"],
        "name": ["fastapi-viewsets.required"],
        "checked": ["fastapi-viewsets.invalid"],
    }
    assert found["checked"][0]["detail"] == "Value error, say something"
    # a type error without `input` becomes its field error code, not `required`
    [[entry]] = field_errors([{"type": "int_type", "loc": ("body", "n"), "msg": "m"}]).values()
    assert entry["code"] == "fastapi-viewsets.invalid_integer"


def test_a_mapped_type_without_the_ctx_its_code_reads_is_forwarded_as_pydantic():
    [[entry]] = field_errors([{"type": "string_too_short", "loc": ("body", "x"), "msg": "m"}]).values()
    assert (entry["code"], entry["params"]) == ("pydantic.string_too_short", {})


def test_a_namespaced_pydantic_custom_error_keeps_its_type_and_ctx():
    from pydantic import field_validator
    from pydantic_core import PydanticCustomError

    class Slug(BaseModel):
        slug: str

        @field_validator("slug")
        @classmethod
        def free(cls, value: str) -> str:
            raise PydanticCustomError("app.slug_taken", "Slug {slug} is taken", {"slug": value})

    assert field_errors(_validation_errors(Slug, {"slug": "blue"})) == {
        "slug": [{"code": "app.slug_taken", "params": {"slug": "blue"}, "detail": "Slug blue is taken"}]
    }


def test_a_pydantic_custom_error_without_a_namespace_is_custom_error_with_its_type_and_ctx_in_params():
    from pydantic import field_validator
    from pydantic_core import PydanticCustomError

    class Slug(BaseModel):
        slug: str

        @field_validator("slug")
        @classmethod
        def free(cls, value: str) -> str:
            raise PydanticCustomError("slug_taken", "Slug {slug} is taken", {"slug": value})

    assert field_errors(_validation_errors(Slug, {"slug": "blue"})) == {
        "slug": [
            {
                "code": "pydantic.custom_error",
                "params": {"type": "slug_taken", "ctx": {"slug": "blue"}},
                "detail": "Slug blue is taken",
            }
        ]
    }


def test_field_errors_leave_out_the_input():
    [[entry]] = field_errors([{"type": "url_scheme", "loc": ["body", "x"], "msg": "m", "input": "secret"}]).values()
    assert entry["code"] == f"{ERROR_NAMESPACE_PYDANTIC}.url_scheme"
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
        ("post", "/tracks", {"title": "a"}, {"title": ["fastapi-viewsets.min_length"]}),
        ("post", "/tracks", [1], {"": ["fastapi-viewsets.invalid_type"]}),
        ("get", "/tracks/x", None, {"pk": ["fastapi-viewsets.invalid_integer"]}),
        ("get", "/tracks/teapot", None, {"cups": ["fastapi-viewsets.required"]}),
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


# Django's ValidationError


def _django_fields(error):
    return {
        name: [(entry["code"], entry["params"]) for entry in entries]
        for name, entries in error_body(error)["field_errors"].items()
    }


def test_a_django_validation_error_is_validation_failed_keyed_by_field_name():
    from django.core.exceptions import NON_FIELD_ERRORS, ValidationError
    from django.core.validators import MaxLengthValidator, MinValueValidator

    errors = {}
    for field, validator, value in [("title", MaxLengthValidator(3), "abcd"), ("year", MinValueValidator(1900), 1)]:
        try:
            validator(value)
        except ValidationError as error:
            errors[field] = error
    errors[NON_FIELD_ERRORS] = ValidationError("Title and year clash", code="clash")
    error = ValidationError(errors)
    assert error_status(error) == 422
    assert error_body(error) == body(
        "The request failed validation",
        "fastapi-viewsets.validation_failed",
        fields={
            "title": [
                {
                    "code": "fastapi-viewsets.max_length",
                    "params": {"maxLength": 3},
                    "detail": "Ensure this value has at most 3 characters (it has 4).",
                }
            ],
            "year": [
                {
                    "code": "fastapi-viewsets.min_value",
                    "params": {"minValue": 1900},
                    "detail": "Ensure this value is greater than or equal to 1900.",
                }
            ],
            "": [{"code": "django.clash", "params": {}, "detail": "Title and year clash"}],
        },
    )


def test_django_codes_map_onto_the_vocabulary_with_normalised_params():
    import datetime

    from decimal import Decimal

    from django.core.exceptions import ValidationError
    from django.core.validators import (
        DecimalValidator,
        MaxValueValidator,
        MinLengthValidator,
        MinValueValidator,
        StepValueValidator,
    )
    from django.utils.translation import gettext_lazy

    def raised(validator, value):
        try:
            validator(value)
        except ValidationError as error:
            return error
        raise AssertionError(value)

    cases = [
        (ValidationError("x", code="required"), ("fastapi-viewsets.required", {})),
        (ValidationError("x", code="blank"), ("fastapi-viewsets.required", {})),
        (ValidationError("x", code="null"), ("fastapi-viewsets.required", {})),
        (
            ValidationError("x", code="max_length", params={"max": 10, "length": 12}),
            ("fastapi-viewsets.max_length", {"maxLength": 10}),
        ),
        (
            raised(MaxValueValidator(datetime.date(2000, 1, 1)), datetime.date(2001, 1, 1)),
            ("fastapi-viewsets.max_value", {"maxValue": "2000-01-01"}),
        ),
        (raised(StepValueValidator(5), 3), ("fastapi-viewsets.multiple_of", {"multipleOf": 5})),
        (
            raised(StepValueValidator(5, offset=1), 3),
            ("django.step_size", {"limit_value": 5, "offset": 1, "valid_value1": 6, "valid_value2": 11}),
        ),
        (raised(DecimalValidator(3, 1), Decimal("1234")), ("fastapi-viewsets.max_digits", {"maxDigits": 3})),
        (
            raised(DecimalValidator(3, 1), Decimal("1.25")),
            ("fastapi-viewsets.max_decimal_places", {"maxDecimalPlaces": 1}),
        ),
        (raised(DecimalValidator(3, 1), Decimal("123")), ("fastapi-viewsets.max_whole_digits", {"maxWholeDigits": 2})),
        (
            ValidationError("x %(value)s", code="invalid_choice", params={"value": "q"}),
            ("fastapi-viewsets.invalid_choice", {}),
        ),
        (ValidationError("x", code="invalid_date", params={"value": "q"}), ("fastapi-viewsets.invalid_date", {})),
        (ValidationError("x", code="invalid_time", params={"value": "q"}), ("fastapi-viewsets.invalid_time", {})),
        (
            ValidationError("x", code="invalid_datetime", params={"value": "q"}),
            ("fastapi-viewsets.invalid_datetime", {}),
        ),
        (raised(MinLengthValidator(3), "ab"), ("fastapi-viewsets.min_length", {"minLength": 3})),
        (raised(MinValueValidator(5), 3), ("fastapi-viewsets.min_value", {"minValue": 5})),
        (ValidationError("x", code="invalid"), ("fastapi-viewsets.invalid", {})),
        (ValidationError("x"), ("fastapi-viewsets.invalid", {})),
        (ValidationError("x", code="max_length"), ("django.max_length", {})),
        (ValidationError("x", code="app.taken", params={"slug": "blue"}), ("app.taken", {"slug": "blue"})),
        (
            ValidationError(
                "x", code="overflow", params={"min_days": 1, "label": gettext_lazy("Days"), "when": datetime.time(1)}
            ),
            ("django.overflow", {"min_days": 1, "label": "Days", "when": "01:00:00"}),
        ),
    ]
    for error, expected in cases:
        assert _django_fields(error) == {"": [expected]}, expected


def test_a_django_unique_error_lists_its_fields_and_drops_the_model_objects():
    from django.contrib.auth.models import User
    from django.core.exceptions import ValidationError

    unique = User().unique_error_message(User, ("username",))
    together = ValidationError(
        "together", code="unique_together", params={"model": User(), "model_class": User, "unique_check": ("a", "b")}
    )
    other = ValidationError(
        "%(model_name)s", code="unique_for_date", params={"model": User(), "model_class": User, "model_name": "user"}
    )
    assert _django_fields(ValidationError({"username": [unique]})) == {
        "username": [("fastapi-viewsets.unique", {"fields": ["username"]})]
    }
    assert _django_fields(together) == {"": [("fastapi-viewsets.unique", {"fields": ["a", "b"]})]}
    assert _django_fields(other) == {"": [("django.unique_for_date", {"model_name": "user"})]}


def test_a_django_message_is_interpolated_and_its_lazy_text_rendered():
    from django.core.exceptions import ValidationError
    from django.utils.translation import gettext_lazy

    error = ValidationError(gettext_lazy("%(name)s is taken"), code="app.taken", params={"name": gettext_lazy("Blue")})
    assert error_body(error)["field_errors"] == {
        "": [{"code": "app.taken", "params": {"name": "Blue"}, "detail": "Blue is taken"}]
    }


@pytest.mark.parametrize("message", ["missing %(v)s", "Must be 100%"])
def test_a_django_message_that_does_not_format_with_its_params_is_its_detail_as_is(message):
    from django.core.exceptions import ValidationError

    assert error_body(ValidationError(message, code="app.x", params={"w": 1}))["field_errors"] == {
        "": [{"code": "app.x", "params": {"w": 1}, "detail": message}]
    }


def test_django_errors_prefix_their_loc():
    from django.core.exceptions import ValidationError

    from fastapi_viewsets.error_responses import _validation_errors

    error = ValidationError({"title": ValidationError("x", code="blank"), "__all__": ValidationError("y")})
    assert field_errors(_validation_errors(error, ("body", 7))) == {
        "7.title": [{"code": "fastapi-viewsets.required", "params": {}, "detail": "x"}],
        "7": [{"code": "fastapi-viewsets.invalid", "params": {}, "detail": "y"}],
    }


def test_django_errors_have_the_django_code_as_type_until_field_errors_maps_it():
    from django.core.exceptions import ValidationError

    from fastapi_viewsets.error_responses import _validation_errors

    error = ValidationError(
        {"title": ValidationError("x", code="blank"), "slug": ValidationError("y", code="app.taken"), "__all__": "z"}
    )
    assert [(entry["type"], entry["loc"]) for entry in _validation_errors(error)] == [
        ("django.blank", ("django", "title")),
        ("app.taken", ("django", "slug")),
        ("django.invalid", ("django",)),
    ]


def test_django_errors_without_a_row_are_not_split_in_a_row_keyed_action():
    from django.core.exceptions import ValidationError

    from fastapi_viewsets.error_responses import _validation_errors, FieldNames

    calls = []

    def rename(field, code):
        calls.append((field, code))
        return "name" if field == "title" else field

    error = ValidationError({"title": ValidationError("x", code="unique")})
    assert list(field_errors(_validation_errors(error), FieldNames(rename, row_keyed=True))) == ["name"]
    assert list(field_errors(_validation_errors(error, ("body", 7)), FieldNames(rename, row_keyed=True))) == ["7.name"]
    assert calls == [("title", "django.unique")] * 2


def test_field_names_rename_each_key_with_the_source_code():
    errors = [
        {"type": "string_too_short", "loc": ("body", "name"), "msg": "m", "ctx": {"min_length": 3}},
        {"type": "django.unique", "loc": ("body", "slug"), "msg": "m", "ctx": {"unique_check": ["slug"]}},
        {"type": "app.taken", "loc": ("body",), "msg": "m"},
        {"type": "not_namespaced", "loc": ("query", "q"), "msg": "m"},
    ]
    seen = []

    def rename(field, code):
        seen.append((field, code))
        return f"x_{field}"

    assert list(field_errors(errors, FieldNames(rename))) == ["x_name", "x_slug", "x_", "x_q"]
    assert seen == [
        ("name", "pydantic.string_too_short"),
        ("slug", "django.unique"),
        ("", "app.taken"),
        ("q", "pydantic.custom_error"),
    ]


def test_row_keyed_field_names_keep_the_rows_key_of_a_body_loc_only():
    errors = [
        {"type": "missing", "loc": ("body", 3, "name"), "msg": "m"},
        {"type": "missing", "loc": ("body", 4), "msg": "m"},
        {"type": "missing", "loc": ("query", "limit"), "msg": "m"},
    ]
    names = FieldNames(lambda field, _code: "" if field == "name" else field.upper(), row_keyed=True)
    assert list(field_errors(errors, names)) == ["3", "4", "LIMIT"]


def _taken():
    from django.core.exceptions import ValidationError

    return ValidationError({"title": ValidationError("Taken", code="unique", params={"unique_check": ("title",)})})


def test_a_django_validation_error_of_a_viewset_route_is_answered_422_and_not_raised():
    response = client(_taken(), raise_server_exceptions=True).get("/tracks/fail")
    assert (response.status_code, response.json()["field_errors"]) == (
        422,
        {"title": [{"code": "fastapi-viewsets.unique", "params": {"fields": ["title"]}, "detail": "Taken"}]},
    )


def test_a_django_validation_error_of_an_installed_route_is_answered_422_and_not_raised():
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/plain")
    async def plain():
        raise _taken()

    response = TestClient(app, raise_server_exceptions=True).get("/plain")
    assert (response.status_code, response.json()["detail_code"]) == (422, "fastapi-viewsets.validation_failed")
