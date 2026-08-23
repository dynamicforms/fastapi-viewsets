import pytest

from muxws.frames import ABSENT

from .protocol import (
    body_to_payload,
    build_response_meta,
    EnvelopeError,
    is_command,
    merge_headers,
    parse_request,
)


def test_is_command_requires_both_pseudo_headers():
    assert is_command({":method": "GET", ":path": "/items"})
    assert not is_command({":method": "GET"})
    assert not is_command({":path": "/items"})
    assert not is_command({"action": "quote"})
    assert not is_command({})
    assert not is_command(None)


def test_parse_request_splits_pseudo_headers_from_http_headers():
    method, path, query, headers = parse_request(
        {
            ":method": "get",
            ":path": "/items/1",
            "Authorization": "Bearer tok",
        }
    )
    assert method == "GET"
    assert path == "/items/1"
    assert query == b""
    assert headers == {"authorization": "Bearer tok"}


def test_parse_request_accepts_a_query_string():
    _, _, query, _ = parse_request({":method": "GET", ":path": "/items", ":query": "?sort=-year"})
    assert query == b"sort=-year"


def test_parse_request_encodes_a_query_mapping():
    _, _, query, _ = parse_request(
        {
            ":method": "GET",
            ":path": "/items",
            ":query": {"sort": "-year", "page": 2},
        }
    )
    assert query == b"sort=-year&page=2"


def test_parse_request_expands_list_values_into_repeated_keys():
    """A comma-joined value would bind as one string; FastAPI wants list[str] as repeats."""
    _, _, query, _ = parse_request(
        {
            ":method": "GET",
            ":path": "/items",
            ":query": {"genre": ["jazz", "blues"]},
        }
    )
    assert query == b"genre=jazz&genre=blues"


def test_parse_request_drops_none_query_values():
    _, _, query, _ = parse_request({":method": "GET", ":path": "/items", ":query": {"a": None, "b": 1}})
    assert query == b"b=1"


def test_parse_request_rejects_a_non_command():
    with pytest.raises(EnvelopeError):
        parse_request({"action": "quote"})


def test_parse_request_rejects_a_relative_path():
    with pytest.raises(EnvelopeError, match="absolute"):
        parse_request({":method": "GET", ":path": "items"})


def test_parse_request_rejects_non_string_pseudo_headers():
    with pytest.raises(EnvelopeError, match="strings"):
        parse_request({":method": 7, ":path": "/items"})


def test_parse_request_rejects_a_query_of_the_wrong_type():
    with pytest.raises(EnvelopeError, match="string or a mapping"):
        parse_request({":method": "GET", ":path": "/items", ":query": 7})


def test_merge_headers_lets_the_call_override_the_connection():
    merged = merge_headers({"Authorization": "handshake", "x-trace": "abc"}, {"authorization": "per-call"})
    assert merged == {"authorization": "per-call", "x-trace": "abc"}


def test_merge_headers_lowercases_the_baseline():
    assert merge_headers({"X-Session-Token": "t"}, {}) == {"x-session-token": "t"}


def test_build_response_meta_carries_status_and_headers():
    assert build_response_meta(201, {"set-cookie": "a=b"}) == {":status": 201, "set-cookie": "a=b"}


def test_body_to_payload_maps_none_to_absent():
    """muxws distinguishes "no payload" from a null payload, and a 204 means the former."""
    assert body_to_payload(None) is ABSENT
    assert body_to_payload({"a": 1}) == {"a": 1}
