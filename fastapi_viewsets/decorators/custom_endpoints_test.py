"""
Tests for custom endpoint registration via __router on a ViewSet class,
as described in docs/guide/custom-endpoints.md.
"""

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from fastapi_viewsets.collection_viewset import CollectionViewSet
from fastapi_viewsets.context import Context
from fastapi_viewsets.decorators import route_viewset
from fastapi_viewsets.mixins import BulkViewSetMixin, LookupItem, LookupMixin

from ..mux_ws.registry import reset_registry, schema_for
from .conftest import iter_routes

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


class Item(BaseModel):
    id: int = Field(default=0, json_schema_extra={"autoinc_int": True})
    name: str
    description: str | None = None


class CloneRequest(BaseModel):
    source_id: int
    new_name: str


def make_database() -> dict[int, Item]:
    return {
        1: Item(id=1, name="Widget", description="A widget"),
        2: Item(id=2, name="Gadget", description="A gadget"),
    }


# ---------------------------------------------------------------------------
# 1. Custom GET endpoint (search)
# ---------------------------------------------------------------------------


def make_search_app():
    database = make_database()
    app = FastAPI()
    router = APIRouter()

    @route_viewset(router, base_path="/items", pk_field_name="id")
    class ItemViewSet(CollectionViewSet[int, Item], BulkViewSetMixin[int, Item], LookupMixin):
        __router = APIRouter()

        def __init__(self):
            super().__init__(container=database, pk_field="id")

        async def perform_lookup(self, context: Context) -> list[LookupItem]:
            return [
                LookupItem(group=None, pk=item.id, title=item.name, icon=None)
                for item in await self.perform_list(context)
            ]

        @__router.get("search")
        async def search(self, context: Context, q: str) -> list[Item]:
            all_items = await self.perform_list(context)
            return [item for item in all_items if q.lower() in item.name.lower()]

    app.include_router(router)
    return app


def test_custom_search_endpoint_is_registered():
    """Custom __router GET route is picked up by route_viewset."""
    app = make_search_app()
    paths = {route.path for route in iter_routes(app.routes)}
    assert "/items/search" in paths


def test_custom_search_endpoint_returns_filtered_results():
    """GET /items/search?q=widget returns only matching items."""
    client = TestClient(make_search_app())
    response = client.get("/items/search", params={"q": "widget"})
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["name"] == "Widget"


def test_custom_search_endpoint_case_insensitive():
    """Search is case-insensitive."""
    client = TestClient(make_search_app())
    response = client.get("/items/search", params={"q": "GADGET"})
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["name"] == "Gadget"


def test_custom_search_endpoint_no_results():
    """Search returns empty list when nothing matches."""
    client = TestClient(make_search_app())
    response = client.get("/items/search", params={"q": "nonexistent"})
    assert response.status_code == 200
    assert response.json() == []


def test_standard_routes_still_present_with_custom_router():
    """Standard mixin routes are still registered alongside the custom one."""
    app = make_search_app()
    paths = {route.path for route in iter_routes(app.routes)}
    for expected in ["/items", "/items/{pk}", "/items/bulk", "/items/lookup"]:
        assert expected in paths, f"Expected path {expected!r} not found"


# ---------------------------------------------------------------------------
# 2. Custom POST endpoint with request body (clone)
# ---------------------------------------------------------------------------


def make_clone_app():
    database = make_database()
    app = FastAPI()
    router = APIRouter()

    @route_viewset(router, base_path="/items", pk_field_name="id")
    class ItemViewSet(CollectionViewSet[int, Item], BulkViewSetMixin[int, Item]):
        __router = APIRouter()

        def __init__(self):
            super().__init__(container=database, pk_field="id")

        @__router.post("clone")
        async def clone(self, context: Context, body: CloneRequest) -> Item:
            source = await self.perform_retrieve(context, body.source_id)
            return await self.perform_create(context, Item(id=0, name=body.new_name, description=source.description))

    app.include_router(router)
    return app


def test_custom_clone_endpoint_is_registered():
    """Custom __router POST route is picked up by route_viewset."""
    app = make_clone_app()
    paths = {route.path for route in iter_routes(app.routes)}
    assert "/items/clone" in paths


def test_custom_clone_endpoint_creates_copy():
    """POST /items/clone creates a new item with the source's description."""
    client = TestClient(make_clone_app())
    response = client.post("/items/clone", json={"source_id": 1, "new_name": "Widget copy"})
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Widget copy"
    assert data["description"] == "A widget"
    assert data["id"] > 0  # autoinc assigned


def test_custom_clone_endpoint_404_for_missing_source():
    """POST /items/clone returns 404 when source_id does not exist."""
    client = TestClient(make_clone_app())
    response = client.post("/items/clone", json={"source_id": 999, "new_name": "Ghost"})
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# 3. Both custom endpoints on the same ViewSet
# ---------------------------------------------------------------------------


def make_combined_app():
    database = make_database()
    app = FastAPI()
    router = APIRouter()

    @route_viewset(router, base_path="/items", pk_field_name="id")
    class ItemViewSet(CollectionViewSet[int, Item], BulkViewSetMixin[int, Item]):
        __router = APIRouter()

        def __init__(self):
            super().__init__(container=database, pk_field="id")

        @__router.get("search")
        async def search(self, context: Context, q: str) -> list[Item]:
            all_items = await self.perform_list(context)
            return [item for item in all_items if q.lower() in item.name.lower()]

        @__router.post("clone")
        async def clone(self, context: Context, body: CloneRequest) -> Item:
            source = await self.perform_retrieve(context, body.source_id)
            return await self.perform_create(context, Item(id=0, name=body.new_name, description=source.description))

    app.include_router(router)
    return app


def test_combined_both_custom_routes_registered():
    """Both search and clone are registered when declared on the same __router."""
    app = make_combined_app()
    paths = {route.path for route in iter_routes(app.routes)}
    assert "/items/search" in paths
    assert "/items/clone" in paths


def test_combined_search_and_clone_work_together():
    """search and clone both work correctly on the same ViewSet instance."""
    client = TestClient(make_combined_app())

    # clone first
    clone_resp = client.post("/items/clone", json={"source_id": 2, "new_name": "Gadget Jr"})
    assert clone_resp.status_code == 200
    cloned = clone_resp.json()
    assert cloned["name"] == "Gadget Jr"

    # search should now find the clone too
    search_resp = client.get("/items/search", params={"q": "gadget"})
    assert search_resp.status_code == 200
    names = [item["name"] for item in search_resp.json()]
    assert "Gadget" in names
    assert "Gadget Jr" in names


# ---------------------------------------------------------------------------
# 4. A ViewSet with custom endpoints only
# ---------------------------------------------------------------------------


class Report(BaseModel):
    name: str
    approved: bool = False


def make_custom_only_app():
    app = FastAPI()
    router = APIRouter()

    @route_viewset(router, base_path="/reports", pk_field_name="id")
    class ReportViewSet:
        __router = APIRouter()

        @__router.get("summary")
        async def summary(self) -> Report:
            return Report(name="summary")

        @__router.get("top-rated")
        async def top_rated(self) -> list[Report]:
            return [Report(name="top")]

        @__router.post("{pk}/approve")
        async def approve(self, pk: int) -> Report:
            return Report(name=f"report {pk}", approved=True)

    app.include_router(router)
    return app


def test_custom_only_viewset_registers_its_endpoints_and_no_standard_ones():
    paths = {route.path for route in iter_routes(make_custom_only_app().routes)}
    assert {"/reports/summary", "/reports/top-rated", "/reports/{pk}/approve", "/reports/schema"} <= paths
    assert not paths & {"/reports", "/reports/{pk}", "/reports/bulk", "/reports/lookup"}


def test_custom_only_viewset_serves_its_endpoints():
    client = TestClient(make_custom_only_app())
    assert client.get("/reports/summary").json() == {"name": "summary", "approved": False}
    assert client.get("/reports/top-rated").json() == [{"name": "top", "approved": False}]
    assert client.post("/reports/7/approve").json() == {"name": "report 7", "approved": True}


def test_schema_names_the_method_serving_each_custom_endpoint():
    """`x-viewset-action` is the method name, whatever the path: a parameter segment, a hyphen."""
    paths = TestClient(make_custom_only_app()).get("/reports/schema").json()["paths"]
    assert paths["/reports/summary"]["get"]["x-viewset-action"] == "summary"
    assert paths["/reports/top-rated"]["get"]["x-viewset-action"] == "top_rated"
    assert paths["/reports/{pk}/approve"]["post"]["x-viewset-action"] == "approve"


def test_schema_names_the_method_serving_each_mixin_endpoint():
    paths = TestClient(make_search_app()).get("/items/schema").json()["paths"]
    assert paths["/items/search"]["get"]["x-viewset-action"] == "search"
    assert paths["/items/{pk}"]["get"]["x-viewset-action"] == "retrieve"


def test_muxws_schema_names_the_method_serving_each_custom_endpoint():
    reset_registry()
    try:
        make_custom_only_app()
        paths = schema_for("/reports")["paths"]
        assert paths["/reports/top-rated"]["get"]["x-viewset-action"] == "top_rated"
        assert paths["/reports/{pk}/approve"]["post"]["x-viewset-action"] == "approve"
    finally:
        reset_registry()
