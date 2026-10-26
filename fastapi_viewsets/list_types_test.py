"""
`MultiTypeListMixin`: list row types a client selects with `X-List-Type`.
"""

import types

from dataclasses import dataclass
from typing import Annotated, Any, Generic, TypeVar

import pytest

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from muxws.frames import ABSENT
from pydantic import BaseModel, create_model

from fastapi_viewsets.collection_viewset import CollectionViewSet
from fastapi_viewsets.decorators import route_viewset
from fastapi_viewsets.exceptions import UnsupportedListTypeError
from fastapi_viewsets.filters import make_filter_model
from fastapi_viewsets.list_query import build_list_query, ListQuery
from fastapi_viewsets.mixins import (
    BulkViewSetMixin,
    CursorListMixin,
    ListMixin,
    ListTypes,
    MultiTypeListMixin,
    PaginatedListMixin,
    ReadOnlyViewSetMixin,
    ViewSetMixin,
)
from fastapi_viewsets.mux_ws.registry import reset_registry
from fastapi_viewsets.mux_ws.server import process_command


class Member(BaseModel):
    id: int
    name: str
    email: str
    rank: int


class MemberRow(BaseModel):
    id: int
    name: str


class MemberName(BaseModel):
    name: str


@dataclass
class DataclassRow:
    id: int


MemberFilter = make_filter_model(Member, {"email": ["icontains"], "rank": ["gte"]})


def members() -> dict[int, Member]:
    return {
        n: Member(id=n, name=f"Member {n:02d}", email=f"m{n}@{'a' if n % 2 else 'b'}.com", rank=n % 3)
        for n in range(1, 8)
    }


class _Members:
    def __init__(self):
        super().__init__(container=members(), pk_field="id")


class MultiFirstViewSet(
    _Members,
    CollectionViewSet[int, Member],
    MultiTypeListMixin[Annotated[MemberRow, "compact"], MemberName],
    CursorListMixin[Member, MemberFilter],
):
    default_page_size = 3
    list_shapes = ("cursor", "paginated", "plain")


class MultiLastViewSet(
    _Members,
    CollectionViewSet[int, Member],
    CursorListMixin[Member, MemberFilter],
    MultiTypeListMixin[Annotated[MemberRow, "compact"], MemberName],
):
    default_page_size = 3
    list_shapes = ("cursor", "paginated", "plain")


class MultiOnlyViewSet(
    _Members, CollectionViewSet[int, Member], MultiTypeListMixin[Annotated[MemberRow, "compact"], MemberName]
):
    list_shape = "paginated"
    default_page_size = 3


class SingleTypeViewSet(_Members, CollectionViewSet[int, Member], MultiTypeListMixin[MemberRow]):
    pass


class PlainViewSet(_Members, CollectionViewSet[int, Member], ViewSetMixin[int, Member]):
    pass


def client_for(viewset_cls, **route_kwargs) -> TestClient:
    app = FastAPI()
    router = APIRouter()
    route_viewset(router, base_path="/members", pk_field_name="id", **route_kwargs)(viewset_cls)
    app.include_router(router)
    return TestClient(app)


def list_operation(document: dict) -> dict:
    return document["paths"]["/members"]["get"]


def header(operation: dict, name: str) -> dict | None:
    return next((p for p in operation.get("parameters", []) if p["name"] == name), None)


def response_schema(operation: dict) -> dict:
    return operation["responses"]["200"]["content"]["application/json"]["schema"]


def response_refs(operation: dict) -> set[str]:
    schema = response_schema(operation)
    return {member["$ref"].rsplit("/", 1)[-1] for member in schema.get("anyOf", [schema])}


# ---------------------------------------------------------------------------
# Type resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("viewset_cls", [MultiFirstViewSet, MultiLastViewSet])
def test_a_composed_list_mixin_puts_the_record_type_first_in_either_base_order(viewset_cls):
    resolved = viewset_cls.resolve_list_types()
    assert resolved == ListTypes(
        default="Member",
        types={"Member": Member, "compact": MemberRow, "MemberName": MemberName},
        record="Member",
        record_type=Member,
    )


def test_without_a_list_mixin_the_first_added_type_is_the_default():
    resolved = MultiOnlyViewSet.resolve_list_types()
    assert resolved == ListTypes(
        default="compact", types={"compact": MemberRow, "MemberName": MemberName}, record=None, record_type=Member
    )


@pytest.mark.parametrize(
    "list_base",
    [
        ListMixin[Member],
        PaginatedListMixin[Member],
        CursorListMixin[Member],
        ReadOnlyViewSetMixin[int, Member],
        ViewSetMixin[int, Member],
        BulkViewSetMixin[int, Member],
    ],
)
def test_every_list_mixin_counts_as_composed(list_base):
    first = types.new_class("First", (CollectionViewSet[int, Member], MultiTypeListMixin[MemberRow], list_base))
    last = types.new_class("Last", (CollectionViewSet[int, Member], list_base, MultiTypeListMixin[MemberRow]))
    for cls in (first, last):
        assert list(cls.resolve_list_types().types) == ["Member", "MemberRow"]


def test_a_subclass_inherits_the_declaration():
    class Derived(MultiLastViewSet):
        pass

    assert list(Derived.resolve_list_types().types) == ["Member", "compact", "MemberName"]


def test_a_viewset_without_added_types_has_its_record_type_only():
    assert PlainViewSet.resolve_list_types() == ListTypes(
        default="Member", types={"Member": Member}, record="Member", record_type=Member
    )


@pytest.mark.parametrize(
    ("added", "message"),
    [
        (MultiTypeListMixin[()], "declares no list types"),
        (MultiTypeListMixin, "declares no list types"),
        (MultiTypeListMixin[MemberRow, Annotated[MemberName, "MemberRow"]], "two list types are named 'MemberRow'"),
        (MultiTypeListMixin[Annotated[MemberRow, "Member"]], "two list types are named 'Member'"),
        (MultiTypeListMixin[list[MemberRow]], "is not a pydantic model"),
        (MultiTypeListMixin[MemberRow | None], "is not a pydantic model"),
        (MultiTypeListMixin[DataclassRow], "is not a pydantic model"),
    ],
)
def test_a_declaration_route_viewset_refuses(added, message):
    cls = types.new_class("Refused", (_Members, CollectionViewSet[int, Member], added, ListMixin[Member]))
    with pytest.raises(ValueError, match=message):
        route_viewset(APIRouter(), base_path="/members")(cls)


# ---------------------------------------------------------------------------
# OpenAPI and /schema
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/openapi.json", "/members/schema"])
def test_the_response_is_every_shape_of_every_type(path):
    operation = list_operation(client_for(MultiFirstViewSet).get(path).json())
    assert response_refs(operation) == {
        f"{envelope}_{model}_"
        for envelope in ("CursorPage", "PaginatedList", "ListOf")
        for model in ("Member", "MemberRow", "MemberName")
    }
    examples = operation["responses"]["200"]["content"]["application/json"]["examples"]
    assert len(examples) == 9
    assert examples["paginated, compact"]["summary"] == "X-List-Shape: paginated, X-List-Type: compact"
    assert set(examples["plain, compact"]["value"][0]) == {"id", "name"}
    assert set(examples["cursor, Member"]["value"]["results"][0]) == {"id", "name", "email", "rank"}


@pytest.mark.parametrize("path", ["/openapi.json", "/members/schema"])
def test_the_type_header_lists_the_names_and_the_default(path):
    operation = list_operation(client_for(MultiFirstViewSet).get(path).json())
    type_header = header(operation, "X-List-Type")
    assert type_header["schema"]["enum"] == ["Member", "compact", "MemberName", None]
    assert "(Member)" in type_header["description"]


@pytest.mark.parametrize("path", ["/openapi.json", "/members/schema"])
def test_the_response_schema_publishes_each_name_with_its_model(path):
    document = client_for(MultiFirstViewSet).get(path).json()
    published = response_schema(list_operation(document))["x-list-types"]
    assert published == {
        "default": "Member",
        "types": {
            "Member": {"$ref": "#/components/schemas/Member"},
            "compact": {"$ref": "#/components/schemas/MemberRow"},
            "MemberName": {"$ref": "#/components/schemas/MemberName"},
        },
    }
    for ref in published["types"].values():
        assert ref["$ref"].rsplit("/", 1)[-1] in document["components"]["schemas"]


def test_types_alone_vary_the_examples_by_type():
    operation = list_operation(client_for(MultiOnlyViewSet).get("/openapi.json").json())
    examples = operation["responses"]["200"]["content"]["application/json"]["examples"]
    assert {key: example["summary"] for key, example in examples.items()} == {
        "compact": "X-List-Type: compact",
        "MemberName": "X-List-Type: MemberName",
    }
    assert response_refs(operation) == {"PaginatedList_MemberRow_", "PaginatedList_MemberName_"}
    assert response_schema(operation)["x-list-types"]["default"] == "compact"


def test_one_type_keeps_the_header_out_of_the_schema():
    operation = list_operation(client_for(SingleTypeViewSet).get("/openapi.json").json())
    assert header(operation, "X-List-Type") is None
    assert response_refs(operation) == {"ListOf_MemberRow_"}
    assert response_schema(operation)["x-list-types"] == {
        "default": "MemberRow",
        "types": {"MemberRow": {"$ref": "#/components/schemas/MemberRow"}},
    }


def test_a_viewset_without_added_types_publishes_no_types():
    operation = list_operation(client_for(PlainViewSet).get("/openapi.json").json())
    assert header(operation, "X-List-Type") is None
    assert "x-list-types" not in response_schema(operation)
    assert response_refs(operation) == {"ListOf_Member_"}


TItem = TypeVar("TItem")


class Boxed(BaseModel, Generic[TItem]):
    value: TItem


@pytest.mark.parametrize("path", ["/openapi.json", "/members/schema"])
def test_each_ref_names_the_component_fastapi_generates(path):
    first = create_model("Card", id=(int, ...), __module__="clubs.cards")
    second = create_model("Card", name=(str, ...), __module__="shop.cards")

    class CardsViewSet(
        _Members,
        CollectionViewSet[int, Member],
        ListMixin[Member],
        MultiTypeListMixin[Annotated[first, "one"], Annotated[second, "two"], Annotated[Boxed[int], "generic"]],
    ):
        pass

    document = client_for(CardsViewSet).get(path).json()
    refs = {
        name: ref["$ref"].rsplit("/", 1)[-1]
        for name, ref in response_schema(list_operation(document))["x-list-types"]["types"].items()
    }
    assert refs["one"] != refs["two"]
    for component in refs.values():
        assert component in document["components"]["schemas"]
    assert document["components"]["schemas"][refs["one"]]["properties"].keys() == {"id"}
    assert document["components"]["schemas"][refs["two"]]["properties"].keys() == {"name"}
    assert document["components"]["schemas"][refs["generic"]]["properties"].keys() == {"value"}


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("viewset_cls", [MultiFirstViewSet, MultiLastViewSet])
@pytest.mark.parametrize("shape", ["cursor", "paginated", "plain"])
def test_the_header_selects_the_row_type_in_every_shape(viewset_cls, shape):
    client = client_for(viewset_cls)

    def rows(**headers) -> list[dict]:
        body = client.get("/members", headers={"X-List-Shape": shape, **headers}).json()
        return body if shape == "plain" else body["results"]

    assert set(rows()[0]) == {"id", "name", "email", "rank"}
    assert set(rows(**{"X-List-Type": "Member"})[0]) == {"id", "name", "email", "rank"}
    assert set(rows(**{"X-List-Type": "compact"})[0]) == {"id", "name"}
    assert set(rows(**{"X-List-Type": "MemberName"})[0]) == {"name"}


def test_without_a_list_mixin_an_absent_header_selects_the_first_added_type():
    client = client_for(MultiOnlyViewSet)
    assert client.get("/members").json()["results"][0] == {"id": 1, "name": "Member 01"}
    assert client.get("/members", headers={"X-List-Type": "MemberName"}).json()["results"][0] == {"name": "Member 01"}


def test_an_unknown_type_is_a_422():
    response = client_for(MultiFirstViewSet).get("/members", headers={"X-List-Type": "full"})
    assert response.status_code == 422
    assert "X-List-Type" in response.json()["field_errors"]


@pytest.mark.asyncio
async def test_an_unknown_type_called_directly_is_refused():
    with pytest.raises(UnsupportedListTypeError) as raised:
        await MultiFirstViewSet().list_items(None, x_list_type="full")
    assert raised.value.code == "fastapi-viewsets.unsupported_list_type"
    assert raised.value.params == {"type": "full", "allowed": ["Member", "compact", "MemberName"]}


@pytest.mark.asyncio
async def test_a_direct_get_list_answers_in_the_record_type():
    page = await MultiFirstViewSet().get_list(None, build_list_query(None, None, limit=2))
    assert [type(row) for row in page.results] == [Member, Member]


# ---------------------------------------------------------------------------
# Pipeline: records in, selected type out
# ---------------------------------------------------------------------------


def test_filter_and_sort_read_fields_the_selected_type_lacks():
    client = client_for(MultiFirstViewSet)
    body = client.get(
        "/members",
        params={"email__icontains": "@a.", "sort": "rank:desc,email:asc"},
        headers={"X-List-Shape": "plain", "X-List-Type": "MemberName"},
    ).json()
    expected = sorted(
        (member for member in members().values() if "@a." in member.email), key=lambda m: (-m.rank, m.email)
    )
    assert body == [{"name": member.name} for member in expected]


@pytest.mark.parametrize("list_type", ["compact", "MemberName"])
def test_a_cursor_walks_on_keys_the_selected_type_lacks(list_type):
    client = client_for(MultiFirstViewSet)
    names, cursor = [], None
    while True:
        params = {"sort": "rank:asc,email:desc", "limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = client.get("/members", params=params, headers={"X-List-Type": list_type}).json()
        names += [row["name"] for row in page["results"]]
        cursor = page["next"]
        if not cursor:
            break
    expected = sorted(sorted(members().values(), key=lambda m: m.email, reverse=True), key=lambda m: m.rank)
    assert names == [member.name for member in expected]

    back = client.get("/members", params={"cursor": page["previous"], "limit": 2, "sort": "rank:asc,email:desc"})
    assert [row["name"] for row in back.json()["results"]] == names[-3:-1]


class ConvertingViewSet(
    CollectionViewSet[int, Member],
    MultiTypeListMixin[Annotated[MemberRow, "compact"]],
    PaginatedListMixin[Member, MemberFilter],
):
    """Stores rows as dicts, so `to_record` and `to_list_item` both have work to do."""

    def __init__(self):
        super().__init__(container={pk: member.model_dump() for pk, member in members().items()}, pk_field="id")
        self.to_record_calls = 0

    def to_record(self, raw: Any) -> Member:
        if isinstance(raw, Member):
            return raw
        self.to_record_calls += 1
        return Member.model_validate(raw)


@pytest.mark.asyncio
async def test_the_default_conversion_goes_through_the_record_type():
    viewset = ConvertingViewSet()
    query = ListQuery(limit=3)
    query.list_type = MemberRow
    page = await viewset.get_list(None, query)
    assert [type(row) for row in page.results] == [MemberRow] * 3
    assert viewset.to_record_calls == 3


@pytest.mark.asyncio
async def test_a_landed_page_is_not_converted_to_records_again():
    viewset = ConvertingViewSet()
    query = ListQuery(limit=3, fltr=MemberFilter(email__icontains="@b."))
    query.list_type = MemberRow
    page = await viewset.get_list(None, query)
    assert [row.id for row in page.results] == [2, 4, 6]
    assert viewset.to_record_calls == 7  # the whole collection, once, by land()


class MemberNote(BaseModel):
    id: int
    note: str = "none"


class MemberId(BaseModel):
    id: int


class OverlappingViewSet(
    _Members, CollectionViewSet[int, Member], ListMixin[Member], MultiTypeListMixin[MemberNote, MemberId]
):
    """`{"id": 1}` validates as `MemberNote` too."""

    list_shapes = ("plain", "paginated", "cursor")


@pytest.mark.parametrize("shape", ["plain", "paginated", "cursor"])
def test_the_response_is_the_selected_type_when_another_type_accepts_its_rows(shape):
    body = client_for(OverlappingViewSet).get("/members", headers={"X-List-Shape": shape, "X-List-Type": "MemberId"})
    rows = body.json() if shape == "plain" else body.json()["results"]
    assert rows[:2] == [{"id": 1}, {"id": 2}]


class Profile(BaseModel):
    id: int
    name: str
    photo: str | None = None


class Account(BaseModel):
    id: int
    name: str
    internal: str = "s"


class AccountViewSet(CollectionViewSet[int, Account], ListMixin[Account], MultiTypeListMixin[Profile]):
    """`Account` validates as `Profile`, and `Profile`'s rows validate as `Account`, with its default."""

    def __init__(self):
        super().__init__(container={1: Account(id=1, name="a")}, pk_field="id")

    def to_list_item(self, raw: Any, item_type: type) -> Any:
        return item_type(id=raw.id, name=raw.name, photo="p.png")


def test_a_plain_list_keeps_the_fields_of_the_selected_type():
    client = client_for(AccountViewSet)
    assert client.get("/members", headers={"X-List-Type": "Profile"}).json() == [
        {"id": 1, "name": "a", "photo": "p.png"}
    ]
    assert client.get("/members").json() == [{"id": 1, "name": "a", "internal": "s"}]


class Person(MemberRow):
    internal: str


class PersonViewSet(CollectionViewSet[int, Person], ListMixin[Person], MultiTypeListMixin[MemberRow]):
    """The record type subclasses the added type."""

    def __init__(self):
        super().__init__(container={1: Person(id=1, name="a", internal="s")}, pk_field="id")


@pytest.mark.asyncio
async def test_a_record_that_subclasses_the_selected_type_is_converted_into_it():
    query = ListQuery(shape="plain")
    query.list_type = MemberRow
    rows = await PersonViewSet().get_list(None, query)
    assert [type(row) for row in rows] == [MemberRow]
    response = client_for(PersonViewSet).get("/members", headers={"X-List-Type": "MemberRow"})
    assert response.json() == [{"id": 1, "name": "a"}]


class StoredMember(BaseModel):
    pk: int
    full_name: str


class StoredViewSet(CollectionViewSet[int, Member], PaginatedListMixin[Member], MultiTypeListMixin[MemberName]):
    """The source yields a pydantic model of its own, which `to_record` maps to `Member`."""

    def __init__(self):
        super().__init__(container={}, pk_field="id")

    async def perform_list(self, context) -> list[StoredMember]:  # noqa: ARG002
        return [StoredMember(pk=1, full_name="Ann")]

    def to_record(self, raw: Any) -> Member:
        if isinstance(raw, Member):
            return raw
        return Member(id=raw.pk, name=raw.full_name, email="", rank=0)


def test_a_source_model_goes_through_to_record_before_the_selected_type():
    client = client_for(StoredViewSet)
    assert client.get("/members", headers={"X-List-Type": "MemberName"}).json()["results"] == [{"name": "Ann"}]
    assert client.get("/members").json()["results"][0]["name"] == "Ann"


class CompactRows(MultiTypeListMixin[Annotated[MemberRow, "compact"]]):
    """A reusable mixin that adds a row type."""


@pytest.mark.parametrize(
    "bases",
    [
        (CollectionViewSet[int, Member], ListMixin[Member], CompactRows),
        (CollectionViewSet[int, Member], CompactRows, ListMixin[Member]),
        (CollectionViewSet[int, Member], ViewSetMixin[int, Member], CompactRows),
    ],
)
def test_a_reusable_subclass_composes_in_either_base_order(bases):
    cls = types.new_class("Reused", (_Members, *bases))
    assert list(cls.resolve_list_types().types) == ["Member", "compact"]
    response = client_for(cls).get("/members", headers={"X-List-Type": "compact"})
    assert response.json()[0] == {"id": 1, "name": "Member 01"}


# ---------------------------------------------------------------------------
# muxws
# ---------------------------------------------------------------------------


class _Stream:
    def __init__(self, headers: dict[str, Any]):
        self.headers = headers
        self.payload: Any = None
        self.reply_headers: dict[str, Any] = {}

    async def reply(self, payload: Any, *, headers: dict[str, Any] | None = None, trailers=None) -> None:  # noqa: ARG002
        self.payload = payload
        self.reply_headers = headers or {}


@pytest.mark.asyncio
async def test_the_type_header_travels_in_a_muxws_command():
    reset_registry()
    try:
        app = client_for(MultiFirstViewSet, register_muxws=True).app
        stream = _Stream({":method": "GET", ":path": "/members", "x-list-type": "compact", "x-list-shape": "plain"})
        await process_command(ABSENT, stream, app=app)
        assert stream.reply_headers[":status"] == 200
        assert stream.payload[0] == {"id": 1, "name": "Member 01"}

        stream = _Stream({":method": "GET", ":path": "/members", "x-list-type": "full"})
        await process_command(ABSENT, stream, app=app)
        assert stream.reply_headers[":status"] == 422
    finally:
        reset_registry()


@pytest.mark.asyncio
async def test_a_muxws_response_is_the_selected_type_when_another_type_accepts_its_rows():
    reset_registry()
    try:
        app = client_for(OverlappingViewSet, register_muxws=True).app
        stream = _Stream({":method": "GET", ":path": "/members", "x-list-type": "MemberId", "x-list-shape": "plain"})
        await process_command(ABSENT, stream, app=app)
        assert stream.payload[:2] == [{"id": 1}, {"id": 2}]
    finally:
        reset_registry()


@pytest.mark.asyncio
async def test_a_direct_get_list_without_a_list_mixin_answers_in_the_default_type():
    page = await MultiOnlyViewSet().get_list(None, ListQuery(limit=2))
    assert [type(row) for row in page.results] == [MemberRow, MemberRow]
