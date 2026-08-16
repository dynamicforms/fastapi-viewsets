"""
Regression test: route_sort_key must not crash when a viewset has custom action paths of
different depths that share a common prefix (e.g. "account/register" and
"account/register/verify/resend") - see route_viewset.py docstring in build_schema.py.
"""

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from fastapi_viewsets.decorators.build_schema import route_sort_key
from fastapi_viewsets.decorators.route_viewset import route_viewset


def test_route_sort_key_handles_mixed_depth_prefixed_paths():
    """Sorting paths of different depths that share a prefix must not raise TypeError."""
    router = APIRouter()

    @route_viewset(router, base_path="")
    class AccountViewSet:
        __router = APIRouter()

        @__router.post("account/register")
        async def register(self) -> dict:
            return {"ok": True}

        @__router.post("account/register/verify")
        async def register_verify(self) -> dict:
            return {"ok": True}

        @__router.post("account/register/verify/resend")
        async def register_resend(self) -> dict:
            return {"ok": True}

        @__router.post("session/login")
        async def session_login(self) -> dict:
            return {"ok": True}

    paths = {route.path for route in router.routes}
    assert "/account/register" in paths
    assert "/account/register/verify" in paths
    assert "/account/register/verify/resend" in paths
    assert "/session/login" in paths


def test_mixed_depth_prefixed_paths_all_reachable_over_http():
    router = APIRouter()

    @route_viewset(router, base_path="")
    class AccountViewSet:
        __router = APIRouter()

        @__router.post("account/register")
        async def register(self) -> dict:
            return {"which": "register"}

        @__router.post("account/register/verify")
        async def register_verify(self) -> dict:
            return {"which": "register_verify"}

        @__router.post("account/register/verify/resend")
        async def register_resend(self) -> dict:
            return {"which": "register_resend"}

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    assert client.post("/account/register").json() == {"which": "register"}
    assert client.post("/account/register/verify").json() == {"which": "register_verify"}
    assert client.post("/account/register/verify/resend").json() == {"which": "register_resend"}


def test_route_sort_key_is_a_valid_total_order_for_varying_depths():
    """Directly exercise route_sort_key with routes of varying path depth - must be sortable."""
    router = APIRouter()

    @router.post("account/register")
    async def a():  # pragma: no cover - never called
        ...

    @router.post("account/register/verify/resend")
    async def b():  # pragma: no cover - never called
        ...

    routes = list(router.routes)
    sorted(routes, key=route_sort_key, reverse=True)  # must not raise
