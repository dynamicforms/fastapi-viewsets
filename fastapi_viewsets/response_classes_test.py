"""`error_entry()`, `merge_responses()` and the package's own `responses` entries built on them."""

import copy

import pytest

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from .error_responses import ApiErrorBody
from .exceptions import DfViewSetError, NotFoundError, RateLimitedError
from .response_classes import (
    CURSOR_ERROR_RESPONSES,
    error_entry,
    merge_responses,
    NOT_FOUND_RESPONSE,
    POLL_ERROR_RESPONSES,
    SUBSCRIBE_ERROR_RESPONSES,
    VIEWSET_ERROR_RESPONSES,
)

REF = {"$ref": "#/components/schemas/ApiErrorBody"}


class SlugTakenError(DfViewSetError):
    def __init__(self, slug: str):
        super().__init__(409, f"Slug {slug} is taken", "app.slug_taken", {"slug": slug})


def examples(responses: dict, status: str, media_type: str = "application/json") -> dict:
    return responses[status]["content"][media_type]["examples"]


# error_entry


def test_a_positional_error_is_named_after_its_code_and_summarised_by_its_detail():
    assert error_entry(409, "The slug is taken.", SlugTakenError("blue")) == {
        "409": {
            "model": ApiErrorBody,
            "description": "The slug is taken.",
            "content": {
                "application/json": {
                    "examples": {
                        "slug_taken": {
                            "summary": "Slug blue is taken",
                            "value": {
                                "detail": "Slug blue is taken",
                                "detail_code": "app.slug_taken",
                                "detail_params": {"slug": "blue"},
                                "field_errors": {},
                            },
                        }
                    }
                }
            },
        }
    }


def test_a_named_error_is_named_and_summarised_by_its_keyword():
    entry = error_entry(403, "Refused.", not_owner=HTTPException(403, "Not your item"))
    assert examples(entry, "403") == {
        "not_owner": {
            "summary": "not_owner",
            "value": {
                "detail": "Not your item",
                "detail_code": "fastapi-viewsets.http_error",
                "detail_params": {"status": 403},
                "field_errors": {},
            },
        }
    }


def test_positional_examples_come_before_named_ones():
    entry = error_entry(
        403, "Refused.", SlugTakenError("blue"), not_owner=HTTPException(403, "x"), locked=HTTPException(403, "y")
    )
    assert list(examples(entry, "403")) == ["slug_taken", "not_owner", "locked"]


def test_the_ref_form_refers_to_the_error_body_schema():
    entry = error_entry(404, "Missing.", NotFoundError(1), as_ref=True)["404"]
    assert "model" not in entry
    assert entry["content"]["application/json"]["schema"] == REF
    assert entry["content"]["application/json"]["examples"]["not_found"]["value"]["detail_params"] == {"pk": 1}


def test_an_entry_without_errors_has_no_examples():
    assert error_entry("default", "Anything else.") == {
        "default": {"model": ApiErrorBody, "description": "Anything else.", "content": {"application/json": {}}}
    }


def test_two_examples_of_one_name_raise():
    with pytest.raises(ValueError, match="not_found"):
        error_entry(404, "Missing.", NotFoundError(1), NotFoundError(2))
    with pytest.raises(ValueError, match="slug_taken"):
        error_entry(409, "Taken.", SlugTakenError("a"), slug_taken=SlugTakenError("b"))


def test_the_model_form_is_documented_by_fastapi_with_the_examples():
    app = FastAPI()

    @app.get("/x", responses=error_entry(409, "The slug is taken.", SlugTakenError("blue")))
    def x():
        return {}

    response = app.openapi()["paths"]["/x"]["get"]["responses"]["409"]
    assert response["description"] == "The slug is taken."
    assert response["content"]["application/json"]["schema"] == REF
    assert set(response["content"]["application/json"]["examples"]) == {"slug_taken"}


# merge_responses


def test_entries_of_different_statuses_are_kept_and_keys_become_strings():
    merged = merge_responses({404: {"description": "a"}}, {"409": {"description": "b"}})
    assert merged == {"404": {"description": "a"}, "409": {"description": "b"}}


def test_descriptions_of_one_status_are_joined_each_once():
    merged = merge_responses(
        {"409": {"description": "First."}},
        {"409": {"description": "Second."}},
        {"409": {"description": "First."}},
    )
    assert merged["409"]["description"] == "First.\n\nSecond."
    again = merge_responses(merged, {"409": {"description": "Second."}})
    assert again["409"]["description"] == "First.\n\nSecond."


def test_examples_of_one_status_are_merged_by_name_in_either_form():
    for as_ref in (False, True):
        merged = merge_responses(
            error_entry(409, "Taken.", SlugTakenError("a"), as_ref=as_ref),
            error_entry(409, "Locked.", locked=HTTPException(409, "Locked"), as_ref=as_ref),
        )
        assert list(examples(merged, "409")) == ["slug_taken", "locked"]
        assert merged["409"]["description"] == "Taken.\n\nLocked."
        assert ("model" in merged["409"]) is not as_ref


def test_a_later_example_replaces_one_of_the_same_name():
    merged = merge_responses(error_entry(409, "a", SlugTakenError("a")), error_entry(409, "a", SlugTakenError("b")))
    assert examples(merged, "409")["slug_taken"]["value"]["detail_params"] == {"slug": "b"}


def test_a_lone_example_becomes_a_named_one():
    merged = merge_responses(
        {"409": {"description": "a", "content": {"application/json": {"example": {"x": 1}}}}},
        error_entry(409, "b", SlugTakenError("a")),
        {"409": {"description": "c", "content": {"application/json": {"example": {"x": 2}}}}},
        {"409": {"description": "c", "content": {"application/json": {"example": {"x": 1}}}}},
    )
    media = merged["409"]["content"]["application/json"]
    assert "example" not in media
    assert media["examples"] == {
        "example": {"value": {"x": 1}},
        "slug_taken": examples(error_entry(409, "b", SlugTakenError("a")), "409")["slug_taken"],
        "example_2": {"value": {"x": 2}},
    }


def test_a_lone_example_of_a_single_entry_is_kept_as_is():
    entry = {"409": {"description": "a", "content": {"application/json": {"example": {"x": 1}}}}}
    assert merge_responses(entry) == entry


def test_headers_are_merged_by_name():
    merged = merge_responses(
        {"429": {"description": "a", "headers": {"Retry-After": {"description": "old"}, "X-A": {"description": "a"}}}},
        {"429": {"description": "a", "headers": {"Retry-After": {"description": "new"}}}},
    )
    assert merged["429"]["headers"] == {"Retry-After": {"description": "new"}, "X-A": {"description": "a"}}


def test_different_media_types_are_both_kept():
    merged = merge_responses(
        error_entry(409, "a", SlugTakenError("a")),
        {"409": {"description": "a", "content": {"text/plain": {"example": "Slug a is taken"}}}},
    )
    assert set(merged["409"]["content"]) == {"application/json", "text/plain"}
    assert merged["409"]["content"]["text/plain"] == {"example": "Slug a is taken"}


def test_the_arguments_are_not_modified():
    before = error_entry(422, "a", SlugTakenError("a"))
    snapshot = copy.deepcopy(before)
    merge_responses(VIEWSET_ERROR_RESPONSES, before, error_entry(422, "b", locked=RateLimitedError()))
    assert before == snapshot
    assert set(examples(VIEWSET_ERROR_RESPONSES, "422")) == {"validation_failed", "idempotency_key_reused"}


# the package's entries


def test_the_package_entries_keep_their_examples():
    assert list(NOT_FOUND_RESPONSE) == ["404"]
    assert examples(NOT_FOUND_RESPONSE, "404")["not_found"]["value"] == {
        "detail": "Item with pk 42 not found",
        "detail_code": "fastapi-viewsets.not_found",
        "detail_params": {"pk": 42},
        "field_errors": {},
    }
    assert list(VIEWSET_ERROR_RESPONSES) == ["422", "500", "default"]
    assert list(examples(VIEWSET_ERROR_RESPONSES, "422")) == ["validation_failed", "idempotency_key_reused"]
    assert list(examples(VIEWSET_ERROR_RESPONSES, "500")) == ["unhandled_error"]
    assert list(examples(VIEWSET_ERROR_RESPONSES, "default")) == ["http_error", "rate_limited"]
    assert list(examples(CURSOR_ERROR_RESPONSES, "default")) == ["http_error", "cursor_stale", "rate_limited"]
    assert list(examples(SUBSCRIBE_ERROR_RESPONSES, "422")) == [
        "validation_failed",
        "event_topic_invalid",
        "idempotency_key_reused",
    ]
    assert list(examples(POLL_ERROR_RESPONSES, "422")) == [
        "validation_failed",
        "event_cursors_exceeded",
        "idempotency_key_reused",
    ]
    assert examples(VIEWSET_ERROR_RESPONSES, "422")["validation_failed"]["value"] == {
        "detail": "The request failed validation",
        "detail_code": "fastapi-viewsets.validation_failed",
        "detail_params": {},
        "field_errors": {
            "name": [
                {
                    "code": "pydantic.string_too_short",
                    "params": {"min_length": 3},
                    "detail": "String should have at least 3 characters",
                }
            ],
            "": [{"code": "pydantic.missing", "params": {}, "detail": "Field required"}],
        },
    }
    assert examples(VIEWSET_ERROR_RESPONSES, "default")["http_error"]["value"]["detail_params"] == {"status": 403}


def test_an_application_entry_documents_its_examples_in_a_served_schema():
    app = FastAPI()

    @app.get(
        "/x", responses=merge_responses(NOT_FOUND_RESPONSE, error_entry(404, "Or archived.", archived=NotFoundError(7)))
    )
    def x():
        return {}

    document = TestClient(app).get("/openapi.json").json()
    response = document["paths"]["/x"]["get"]["responses"]["404"]
    assert set(response["content"]["application/json"]["examples"]) == {"not_found", "archived"}
    assert response["description"].endswith("\n\nOr archived.")
