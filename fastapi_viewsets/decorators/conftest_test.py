"""Tests for the iter_routes() route-flattening helper in conftest.py."""

from .conftest import iter_routes


class _FakeRoute:
    def __init__(self, path):
        self.path = path


class _FakeIncludedRouter:
    """Stand-in for fastapi's internal include_router() wrapper, duck-typed on
    .original_router rather than importing the real (private) class."""

    def __init__(self, routes):
        self.original_router = _FakeRouter(routes)


class _FakeRouter:
    def __init__(self, routes):
        self.routes = routes


def test_flat_routes_are_yielded_as_is():
    routes = [_FakeRoute("/a"), _FakeRoute("/b")]
    assert list(iter_routes(routes)) == routes


def test_wrapped_router_is_unwrapped_into_its_own_routes():
    inner = [_FakeRoute("/a"), _FakeRoute("/b")]
    routes = [_FakeIncludedRouter(inner)]
    assert list(iter_routes(routes)) == inner


def test_mixed_flat_and_wrapped_routes_are_all_yielded():
    flat = _FakeRoute("/flat")
    inner = [_FakeRoute("/a"), _FakeRoute("/b")]
    routes = [flat, _FakeIncludedRouter(inner)]
    assert list(iter_routes(routes)) == [flat, *inner]


def test_nested_wrapped_routers_are_recursively_unwrapped():
    innermost = [_FakeRoute("/deep")]
    middle = [_FakeIncludedRouter(innermost)]
    routes = [_FakeIncludedRouter(middle)]
    assert list(iter_routes(routes)) == innermost
