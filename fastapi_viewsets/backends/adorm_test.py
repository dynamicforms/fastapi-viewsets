"""
The adorm backend on PostgreSQL, on each adorm driver: absorbed stages are not redone in memory, writes answer
`ApiErrorBody` field errors for the database's constraints, and concurrent, cancelled and failed requests leave
neither locks nor slots behind.
"""

import asyncio
import datetime

from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("adorm")

import adorm  # noqa: E402

from adorm import Concat, F, Value  # noqa: E402
from fastapi import APIRouter, FastAPI, HTTPException  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from httpx2 import ASGITransport, AsyncClient  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from ..action_configuration import action_configuration  # noqa: E402
from ..conf import settings  # noqa: E402
from ..context import Context  # noqa: E402
from ..decorators import route_viewset, ViewSetRouter  # noqa: E402
from ..events import ChangeEvents, MemoryEventStore  # noqa: E402
from ..events.store import get_store  # noqa: E402
from ..exceptions import NotFoundError  # noqa: E402
from ..filters import make_filter_model  # noqa: E402
from ..list_query import ListQuery, ListRecords  # noqa: E402
from ..middleware import Middleware  # noqa: E402
from ..mixins import (  # noqa: E402
    BulkViewSetMixin,
    LookupFilter,
    LookupMixin,
    make_all_optional,
    PaginatedListMixin,
    SortDirection,
    SortStateColumn,
)
from .adorm import AdormSession, AdormViewSet  # noqa: E402
from .adorm_test_models.backends import AdormClub as Club, AdormMember as Member  # noqa: E402

MemberFilter = make_filter_model(
    Member,
    {
        "name": ["exact", "icontains"],
        "email": ["iexact"],
        "rating": ["gte", "lt", "isnull", "in"],
        "club_id": ["exact", "in"],
    },
)


class MemberViewSet(
    AdormViewSet[int, Member],
    BulkViewSetMixin[int, Member, MemberFilter],
    LookupMixin,
):
    """Club members."""

    model = Member
    list_shape = "paginated"
    list_shapes = ("paginated", "cursor", "plain")
    default_page_size = 2
    lookup_title = "name"


class CleanedMemberViewSet(MemberViewSet):
    clean_on_save = True


class ClubViewSet(AdormViewSet[int, Club], BulkViewSetMixin[int, Club]):
    model = Club


class ScopedMemberViewSet(MemberViewSet):
    """Only the members of club 1."""

    async def get_queryset(self, context: Context) -> adorm.QuerySet:
        return (await super().get_queryset(context)).filter(club_id=1)


class MemberRow(BaseModel):
    """A row of a members list, read from a `values()` query set with annotations."""

    id: int
    list_name: str
    club_name: str | None
    rated: bool


class MemberListViewSet(AdormViewSet[int, MemberRow], PaginatedListMixin[MemberRow, make_all_optional(MemberRow)]):
    """The shape of a members list over another schema: its own query set, its own order, rows from dicts."""

    model = Member
    schema = MemberRow
    default_page_size = 2

    async def perform_list(self, context: Context) -> ListRecords:
        return (
            (await self.get_queryset(context))
            .annotate(list_name=Concat(F("name"), Value(" <"), F("email"), Value(">")), club_name=F("club__name"))
            .order_by("list_name", "id")
            .values("id", "list_name", "club_name", "rating")
        )

    async def apply_sort(self, context: Context, query: ListQuery, records: ListRecords) -> ListRecords:
        query.mark_applied("sort")
        return await super().apply_sort(context, query, records)

    def to_record(self, raw: Any) -> MemberRow:
        if isinstance(raw, MemberRow):
            return raw
        return MemberRow(
            id=raw["id"], list_name=raw["list_name"], club_name=raw["club_name"], rated=raw["rating"] is not None
        )


class SessionProbeViewSet(AdormViewSet[int, Member], BulkViewSetMixin[int, Member]):
    """Creates two members from two tasks, then fails when the second name is `fail`."""

    __router = APIRouter()
    model = Member

    @__router.post("pair")
    async def pair(self, context: Context, first: str, second: str) -> list[str]:
        session = await self.get_session(context)

        async def add(name: str) -> str:
            assert adorm.current_session() is session
            await Member.objects.create(name=name, email=f"{name}@example.com")
            return name

        names = await asyncio.gather(add(first), add(second))
        if second == "fail":
            raise RequestValidationError([{"type": "value_error", "loc": ("query", "second"), "msg": "failed"}])
        return list(names)


@action_configuration({AdormSession: False})
class UnwrappedProbeViewSet(SessionProbeViewSet):
    """`SessionProbeViewSet` with the session's transaction switched off for its actions."""


class RaisingViewSet(AdormViewSet[int, Member], BulkViewSetMixin[int, Member]):
    """Actions whose own code raises adorm's exceptions."""

    __router = APIRouter()
    model = Member

    @__router.get("lookup-missing")
    async def lookup_missing(self) -> str:
        return (await Member.objects.get(pk=99)).name

    @__router.post("duplicate")
    async def duplicate(self) -> int:
        return (await Member.objects.create(name="Ana II", email="ana@example.com")).pk


class Gate(Middleware):
    """Refuses the viewset classes the `x-refuse` header names."""

    async def depends(self, request, cls, context) -> None:  # noqa: ARG002 - the hook's signature
        if cls.__name__ in request.headers.get("x-refuse", "").split(","):
            raise HTTPException(403, f"{cls.__name__} refuses")

    async def __call__(self, _request, _viewset, _context, call_next):
        return await call_next()


class ClubMemberListViewSet(MemberListViewSet):
    """`MemberListViewSet` on a session of its own, checking that the session is current in its action."""

    using = "default"
    session_name = "member-list"

    async def perform_list(self, context: Context) -> ListRecords:
        assert adorm.current_session().session_name == "member-list"
        return await super().perform_list(context)


class AccountViewSet(AdormViewSet[int, Club], BulkViewSetMixin[int, Club]):
    """Clubs, with the members list mounted under them, as a members list is mounted under its accounts."""

    __router = ViewSetRouter()
    __router.sub_viewset("members", ClubMemberListViewSet, pk_field_name="id", register_muxws=True)
    model = Club


router = APIRouter()
route_viewset(router, base_path="/members", pk_field_name="id")(MemberViewSet)
route_viewset(router, base_path="/cleaned-members", pk_field_name="id")(CleanedMemberViewSet)
route_viewset(router, base_path="/clubs", pk_field_name="id")(ClubViewSet)
route_viewset(router, base_path="/scoped-members", pk_field_name="id")(ScopedMemberViewSet)
route_viewset(router, base_path="/member-list", pk_field_name="id")(MemberListViewSet)
route_viewset(router, base_path="/probe", pk_field_name="id")(SessionProbeViewSet)
route_viewset(router, base_path="/unwrapped-probe", pk_field_name="id")(UnwrappedProbeViewSet)
route_viewset(router, base_path="/raising", pk_field_name="id")(RaisingViewSet)
route_viewset(router, base_path="/accounts", pk_field_name="id")(AccountViewSet)
app = FastAPI()
app.include_router(router)


@pytest_asyncio.fixture
async def client(adorm_driver):  # noqa: ARG001 - needs adorm configured
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


@pytest_asyncio.fixture
async def members(database):
    """Two clubs and five members; ids follow the order below."""
    await database.execute("INSERT INTO backends_adormclub (name) VALUES ('Alpine'), ('Coastal')")
    await database.execute(
        """
        INSERT INTO backends_adormmember (name, email, rating, club_id, copy) VALUES
            ('Ana', 'ana@example.com', 5, 1, 'a'),
            ('Bor', 'bor@example.com', NULL, 1, ''),
            ('Cene', 'cene@example.com', 3, 2, ''),
            ('Dana', 'dana@example.com', NULL, NULL, ''),
            ('Eva', 'eva@example.com', 5, 2, '')
        """
    )


async def row(database, pk: int) -> dict | None:
    record = await database.fetchrow("SELECT * FROM backends_adormmember WHERE id = $1", pk)
    return None if record is None else dict(record)


async def waiting_for_locks(database, count: int) -> None:
    """Returns once `count` backends of the test database wait for a lock."""
    query = """
        SELECT count(*) FROM pg_stat_activity
        WHERE datname = current_database() AND wait_event_type = 'Lock' AND pid <> pg_backend_pid()
    """
    async with asyncio.timeout(10):
        while await database.fetchval(query) != count:
            await asyncio.sleep(0.01)


class RowLock:
    """`FOR UPDATE` locks taken in a transaction of a connection of the test's own, until `release()`."""

    def __init__(self, connection):
        self.connection = connection
        self.transaction = None

    async def hold(self, *pks: int) -> None:
        self.transaction = self.connection.transaction()
        await self.transaction.start()
        await self.connection.execute("SELECT id FROM backends_adormmember WHERE id = ANY($1) FOR UPDATE", list(pks))

    async def release(self) -> None:
        await self.transaction.rollback()
        self.transaction = None


@pytest_asyncio.fixture
async def row_lock(adorm_database):
    import asyncpg

    connection = await asyncpg.connect(**adorm_database)
    lock = RowLock(connection)
    try:
        yield lock
    finally:
        if lock.transaction is not None:
            await lock.release()
        await connection.close()


def field_errors(response) -> dict:
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["detail_code"] == "fastapi-viewsets.validation_failed"
    return {key: [(error["code"], error["params"]) for error in errors] for key, errors in body["field_errors"].items()}


# ---------------------------------------------------------------------------
# List pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("adorm_driver")
async def test_perform_list_returns_an_unevaluated_query_set():
    records = await MemberViewSet().perform_list(None)
    assert isinstance(records, adorm.QuerySet)


@pytest.mark.asyncio
@pytest.mark.usefixtures("adorm_driver", "members")
async def test_filter_sort_and_page_run_in_the_database():
    query = ListQuery(
        fltr=MemberFilter(rating__gte=3),
        sort=[SortStateColumn(column_name="rating", direction=SortDirection.desc)],
        offset=1,
        limit=2,
    )
    page = await MemberViewSet().get_list(None, query)
    assert {"filter", "sort", "pagination"} <= query.applied
    assert "conversion" not in query.applied
    assert [member.name for member in page.results] == ["Eva", "Cene"]
    assert (page.count, page.has_more, page.has_previous) == (3, False, True)


@pytest.mark.asyncio
@pytest.mark.usefixtures("members")
async def test_list_over_http_answers_the_generated_model_with_its_aliases(client):
    response = await client.get("/members", params={"name__icontains": "a", "sort": "name:desc"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert [member["name"] for member in body["results"]] == ["Eva", "Dana"]
    assert body["count"] == 3
    assert body["results"][1] == {
        "id": 4,
        "name": "Dana",
        "email": "dana@example.com",
        "rating": None,
        "club_id": None,
        "copy": "",
    }


@pytest.mark.asyncio
@pytest.mark.usefixtures("adorm_driver", "members")
async def test_null_placement_in_the_database_matches_the_in_memory_sort():
    viewset = MemberViewSet()
    for direction in SortDirection:
        sort = [SortStateColumn(column_name="rating", direction=direction), SortStateColumn(column_name="id")]
        pushed = await viewset.get_list(None, ListQuery(sort=list(sort), limit=10))
        in_memory = await viewset.sort_list(sort, [member async for member in Member.objects.all()])
        assert [member.id for member in pushed.results] == [member.id for member in in_memory]


@pytest.mark.asyncio
@pytest.mark.usefixtures("members")
@pytest.mark.parametrize(("direction", "expected"), [("asc", [5, 4, 3, 2, 1]), ("desc", [1, 5, 4, 3, 2])])
async def test_a_sort_by_a_fields_alias_runs_in_the_database(client, direction, expected):
    response = await client.get(
        "/members", params={"sort": f"copy:{direction},id:desc", "limit": 10}, headers={"X-List-Shape": "plain"}
    )
    assert response.status_code == 200, response.text
    assert [member["id"] for member in response.json()] == expected

    query = ListQuery(sort=[SortStateColumn(column_name="copy", direction=SortDirection(direction))], limit=10)
    await MemberViewSet().get_list(None, query)
    assert "sort" in query.applied
    assert "conversion" not in query.applied


@pytest.mark.asyncio
@pytest.mark.usefixtures("adorm_driver", "members")
async def test_a_list_and_isnull_filter_run_in_the_database():
    query = ListQuery(fltr=MemberFilter(club_id__in=[2], rating__isnull=False), limit=10)
    page = await MemberViewSet().get_list(None, query)
    assert "filter" in query.applied
    assert [member.name for member in page.results] == ["Cene", "Eva"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("adorm_driver", "members")
async def test_a_hand_written_filter_model_runs_in_memory_and_still_answers():
    class HandWritten(MemberViewSet):
        async def filter_list(self, fltr, records):
            return [record for record in records if record.rating == fltr.rating]

    query = ListQuery(fltr=make_all_optional(Member)(rating=5), limit=10)
    page = await HandWritten().get_list(None, query)
    assert "filter" not in query.applied
    assert [member.name for member in page.results] == ["Ana", "Eva"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("members")
@pytest.mark.parametrize("direction", ["asc", "desc"])
async def test_a_cursor_walk_over_a_nullable_key_visits_every_row_once_both_ways(client, direction):
    headers = {"X-List-Shape": "cursor"}
    params = {"sort": f"rating:{direction}", "limit": 2}

    async def page_at(cursor: str | None) -> dict:
        response = await client.get(
            "/members", params={**params, **({"cursor": cursor} if cursor else {})}, headers=headers
        )
        assert response.status_code == 200, response.text
        return response.json()

    forward, page = [], await page_at(None)
    while True:
        forward += [member["id"] for member in page["results"]]
        if page["next"] is None:
            break
        page = await page_at(page["next"])
    assert sorted(forward) == [1, 2, 3, 4, 5]

    backward = [member["id"] for member in page["results"]]
    while page["previous"] is not None:
        page = await page_at(page["previous"])
        backward = [member["id"] for member in page["results"]] + backward
    assert backward == forward


@pytest.mark.asyncio
@pytest.mark.usefixtures("members")
async def test_a_members_list_over_a_values_query_set_keeps_its_own_order(client):
    response = await client.get("/member-list", params={"offset": 1, "limit": 3, "sort": "id:desc"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 5
    assert body["results"] == [
        {"id": 2, "list_name": "Bor <bor@example.com>", "club_name": "Alpine", "rated": False},
        {"id": 3, "list_name": "Cene <cene@example.com>", "club_name": "Coastal", "rated": True},
        {"id": 4, "list_name": "Dana <dana@example.com>", "club_name": None, "rated": False},
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("members")
async def test_a_members_list_mounted_under_its_parent_runs_after_the_parents_check(client, monkeypatch):
    monkeypatch.setattr(settings, "viewsets_command_middleware", [Gate(), AdormSession()])
    response = await client.get("/accounts/members", params={"limit": 2})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 5
    assert [row["list_name"] for row in body["results"]] == ["Ana <ana@example.com>", "Bor <bor@example.com>"]

    response = await client.get("/accounts/members", headers={"x-refuse": "AccountViewSet"})
    assert response.status_code == 403
    assert response.json()["detail"] == "AccountViewSet refuses"

    # the parent's own `{pk}` route still answers
    assert (await client.get("/accounts/1")).json()["name"] == "Alpine"


@pytest.mark.asyncio
@pytest.mark.usefixtures("members")
async def test_a_lookup_searches_and_resolves_ids_in_the_database(client):
    response = await client.get("/members/lookup", params={"q": "E"})
    assert [item["title"] for item in response.json()] == ["Cene", "Eva"]

    response = await client.get("/members/lookup", params=[("ids", "5"), ("ids", "1"), ("ids", "x")])
    assert [(item["pk"], item["title"]) for item in response.json()] == [(1, "Ana"), (5, "Eva")]

    viewset = MemberViewSet()
    query = ListQuery(fltr=LookupFilter(q="a"))
    records = await viewset.apply_lookup_filter(None, query, await viewset.perform_lookup(None))
    assert isinstance(records, adorm.QuerySet)
    assert "filter" in query.applied


# ---------------------------------------------------------------------------
# Single-record operations and their errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("members")
async def test_a_missing_record_is_not_found(client):
    for method in ("GET", "PATCH", "DELETE"):
        response = await client.request(method, "/members/99", json={"name": "X"} if method == "PATCH" else None)
        assert response.status_code == 404, response.text
        assert response.json()["detail_code"] == "fastapi-viewsets.not_found"


@pytest.mark.asyncio
@pytest.mark.usefixtures("members")
async def test_create_writes_the_record_and_answers_it_as_stored(client, database):
    response = await client.post("/members", json={"name": "Fran", "email": "fran@example.com", "copy": "c"})
    assert response.status_code == 200, response.text
    assert response.json() == {
        "id": 6,
        "name": "Fran",
        "email": "fran@example.com",
        "rating": None,
        "club_id": None,
        "copy": "c",
    }
    assert (await row(database, 6))["copy"] == "c"


@pytest.mark.asyncio
@pytest.mark.usefixtures("members")
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param(
            {"name": "Ana II", "email": "ana@example.com"},
            {"email": [("fastapi-viewsets.unique", {"fields": ["email"]})]},
            id="unique",
        ),
        pytest.param(
            {"name": "Fran", "email": "fran@example.com", "club_id": 99},
            {"club_id": [("fastapi-viewsets.invalid", {})]},
            id="foreign key",
        ),
        pytest.param(
            {"name": "Fran", "email": "fran@example.com", "rating": -1},
            {"": [("fastapi-viewsets.invalid", {})]},
            id="check constraint",
        ),
    ],
)
async def test_a_violated_constraint_is_a_field_error(client, database, body, expected):
    response = await client.post("/members", json=body)
    assert field_errors(response) == expected
    assert await database.fetchval("SELECT count(*) FROM backends_adormmember") == 5


class _Diagnostics:
    def __init__(self, message_detail=None, column_name=None):
        self.message_detail = message_detail
        self.column_name = column_name


class _PsycopgError(Exception):
    """psycopg's error: the report in `diag`."""

    def __init__(self, **diag):
        super().__init__("psycopg")
        self.diag = _Diagnostics(**diag)


class _AsyncpgError(Exception):
    """asyncpg's error: the report in attributes of its own."""

    def __init__(self, detail=None, column_name=None):
        super().__init__("asyncpg")
        self.detail = detail
        self.column_name = column_name


def _wrapped(original: Exception) -> Exception:
    """`original` as the cause of the exception Django raises from it, as the Django driver's `original` is."""
    try:
        raise RuntimeError("django") from original
    except RuntimeError as error:
        return error


@pytest.mark.parametrize(
    ("sqlstate", "report", "expected"),
    [
        pytest.param(
            "23502",
            {"column_name": "email"},
            {"email": [("fastapi-viewsets.required", {})]},
            id="not null",
        ),
        pytest.param(
            "23505",
            {"detail": 'Key (club_id, "copy")=(1, a) already exists.'},
            {"": [("fastapi-viewsets.unique", {"fields": ["club_id", "copy"]})]},
            id="unique together",
        ),
        pytest.param(
            "23505",
            {"detail": "Key (lower(email::text))=(a) already exists."},
            {"": [("fastapi-viewsets.unique", {"fields": []})]},
            id="unique expression",
        ),
        pytest.param("23514", {}, {"": [("fastapi-viewsets.invalid", {})]}, id="check"),
    ],
)
@pytest.mark.parametrize("driver", ["psycopg", "asyncpg"])
def test_a_violation_is_reported_under_the_fields_the_database_names(sqlstate, report, expected, driver):
    from ..error_responses import field_errors as to_field_errors

    if driver == "psycopg":
        original = _wrapped(_PsycopgError(message_detail=report.get("detail"), column_name=report.get("column_name")))
    else:
        original = _AsyncpgError(**report)
    error = adorm.IntegrityError("violation", original=original, sqlstate=sqlstate)
    entries = MemberViewSet()._integrity_errors(error, ("body",))
    assert {
        key: [(one["code"], one["params"]) for one in errors] for key, errors in to_field_errors(entries).items()
    } == expected


@pytest.mark.asyncio
@pytest.mark.usefixtures("members")
async def test_a_bulk_create_with_one_bad_row_writes_none_and_names_the_row(client, database):
    response = await client.post(
        "/members/bulk",
        json=[{"name": "Fran", "email": "fran@example.com"}, {"name": "Ana II", "email": "ana@example.com"}],
    )
    assert field_errors(response) == {"1.email": [("fastapi-viewsets.unique", {"fields": ["email"]})]}
    assert await database.fetchval("SELECT count(*) FROM backends_adormmember") == 5

    response = await client.post(
        "/members/bulk",
        json=[{"name": "Fran", "email": "fran@example.com"}, {"name": "Gal", "email": "gal@example.com"}],
    )
    assert response.status_code == 200, response.text
    assert [member["name"] for member in response.json()] == ["Fran", "Gal"]
    assert await database.fetchval("SELECT count(*) FROM backends_adormmember") == 7


@pytest.mark.asyncio
@pytest.mark.usefixtures("members")
async def test_a_partial_update_writes_only_the_stated_fields(client, database):
    response = await client.patch("/members/1", json={"rating": 4})
    assert response.status_code == 200, response.text
    assert response.json()["rating"] == 4
    assert (await row(database, 1))["copy"] == "a"


@pytest.mark.asyncio
@pytest.mark.usefixtures("members")
@pytest.mark.parametrize("path", ["/clubs/1", "/clubs/bulk"])
async def test_an_update_answers_the_row_as_stored_with_its_auto_now_field(client, database, path):
    body = {"name": "Alpine II"}
    if path.endswith("bulk"):
        response = await client.patch(path, json={"1": body})
        (answered,) = response.json()
    else:
        response = await client.patch(path, json=body)
        answered = response.json()
    assert response.status_code == 200, response.text
    stored = await database.fetchrow("SELECT name, touched FROM backends_adormclub WHERE id = 1")
    assert stored["touched"] is not None
    assert answered["name"] == stored["name"] == "Alpine II"
    assert datetime.datetime.fromisoformat(answered["touched"]) == stored["touched"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("members")
async def test_a_bulk_update_with_one_bad_row_writes_none(client, database):
    response = await client.patch(
        "/members/bulk", json={"1": {"name": "Anja"}, "2": {"email": "ana@example.com"}, "3": {"name": "C"}}
    )
    assert field_errors(response) == {"2.email": [("fastapi-viewsets.unique", {"fields": ["email"]})]}
    assert [(await row(database, pk))["name"] for pk in (1, 2, 3)] == ["Ana", "Bor", "Cene"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("members")
async def test_an_update_outside_get_queryset_is_not_found_and_writes_nothing(client, database):
    response = await client.patch("/scoped-members/bulk", json={"1": {"name": "Anja"}, "3": {"name": "Cena"}})
    assert response.status_code == 404, response.text
    assert [(await row(database, pk))["name"] for pk in (1, 3)] == ["Ana", "Cene"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("members")
async def test_an_update_outside_get_queryset_does_not_wait_for_the_rows_lock(client, row_lock):
    await row_lock.hold(3)
    async with asyncio.timeout(5):
        response = await client.patch("/scoped-members/bulk", json={"1": {"name": "Anja"}, "3": {"name": "Cena"}})
    assert response.status_code == 404, response.text


@pytest.mark.asyncio
@pytest.mark.usefixtures("adorm_driver", "members")
async def test_a_retrieve_outside_get_queryset_is_not_found():
    with pytest.raises(NotFoundError):
        await ScopedMemberViewSet().perform_retrieve(None, 3)
    assert (await ScopedMemberViewSet().perform_retrieve(None, 1)).name == "Ana"


@pytest.mark.asyncio
@pytest.mark.usefixtures("adorm_driver", "members")
async def test_a_merged_record_that_fails_the_schema_is_a_field_error():
    data = Member.model_construct(_fields_set={"name"}, name="x" * 101)
    with pytest.raises(RequestValidationError) as caught:
        await MemberViewSet().perform_update(None, 1, data)
    (error,) = caught.value.errors()
    assert (error["type"], error["loc"]) == ("string_too_long", ("body", "name"))


@pytest.mark.asyncio
@pytest.mark.usefixtures("members")
async def test_destroying_a_referenced_record_is_a_form_error(client, database):
    response = await client.delete("/clubs/1")
    assert field_errors(response) == {"": [("fastapi-viewsets.protected", {})]}
    assert await database.fetchval("SELECT count(*) FROM backends_adormclub") == 2

    await database.execute("INSERT INTO backends_adormclub (name) VALUES ('Empty')")
    response = await client.request("DELETE", "/clubs/bulk", json=[3, 1, 99])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["3"] is None
    assert body["1"]["field_errors"][""][0]["code"] == "fastapi-viewsets.protected"
    assert body["99"]["detail_code"] == "fastapi-viewsets.not_found"


@pytest.mark.asyncio
@pytest.mark.usefixtures("members")
async def test_clean_on_save_answers_full_clean_errors_under_the_schema_fields(client, database):
    response = await client.post("/cleaned-members", json={"name": "Ana II", "email": "ana@example.com", "club_id": 99})
    assert set(field_errors(response)) == {"email", "club_id"}
    assert await database.fetchval("SELECT count(*) FROM backends_adormmember") == 5


# ---------------------------------------------------------------------------
# The session of a call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("members")
async def test_an_action_runs_on_the_viewset_session_also_in_its_tasks(client, database, monkeypatch):
    monkeypatch.setattr(settings, "viewsets_command_middleware", [AdormSession(atomic=True)])
    response = await client.post("/probe/pair", params={"first": "Fran", "second": "Gal"})
    assert response.status_code == 200, response.text
    assert await database.fetchval("SELECT count(*) FROM backends_adormmember") == 7


@pytest.mark.asyncio
@pytest.mark.usefixtures("members")
async def test_an_atomic_action_rolls_back_the_writes_of_every_task(client, database, monkeypatch):
    monkeypatch.setattr(settings, "viewsets_command_middleware", [AdormSession(atomic=True)])
    response = await client.post("/probe/pair", params={"first": "Fran", "second": "fail"})
    assert response.status_code == 422, response.text
    assert await database.fetchval("SELECT count(*) FROM backends_adormmember") == 5

    # switched off by the action's configuration: each create commits on its own
    response = await client.post("/unwrapped-probe/pair", params={"first": "Fran", "second": "fail"})
    assert response.status_code == 422, response.text
    assert await database.fetchval("SELECT count(*) FROM backends_adormmember") == 7


@pytest.mark.asyncio
@pytest.mark.usefixtures("members")
@pytest.mark.parametrize("atomic", [False, True])
async def test_adorms_errors_in_an_actions_own_code_answer_as_the_viewsets_do(client, monkeypatch, atomic):
    monkeypatch.setattr(settings, "viewsets_command_middleware", [AdormSession(atomic=atomic)])
    response = await client.get("/raising/lookup-missing")
    assert response.status_code == 404, response.text
    assert response.json()["detail_code"] == "fastapi-viewsets.not_found"

    response = await client.post("/raising/duplicate")
    assert field_errors(response) == {"email": [("fastapi-viewsets.unique", {"fields": ["email"]})]}


@pytest.mark.asyncio
@pytest.mark.usefixtures("members")
async def test_a_foreign_key_checked_at_the_actions_commit_goes_under_the_rows_field(client, database, monkeypatch):
    monkeypatch.setattr(settings, "viewsets_command_middleware", [AdormSession(atomic=True)])
    response = await client.post("/members", json={"name": "Fran", "email": "fran@example.com", "club_id": 99})
    assert field_errors(response) == {"club_id": [("fastapi-viewsets.invalid", {})]}

    response = await client.post(
        "/members/bulk",
        json=[
            {"name": "Fran", "email": "fran@example.com"},
            {"name": "Gal", "email": "gal@example.com", "club_id": 99},
        ],
    )
    assert field_errors(response) == {"1.club_id": [("fastapi-viewsets.invalid", {})]}

    response = await client.patch("/members/bulk", json={"1": {"name": "Anja"}, "2": {"club_id": 99}})
    assert field_errors(response) == {"2.club_id": [("fastapi-viewsets.invalid", {})]}
    assert await database.fetchval("SELECT count(*) FROM backends_adormmember") == 5
    assert (await row(database, 1))["name"] == "Ana"


@pytest.mark.asyncio
@pytest.mark.usefixtures("members")
async def test_a_change_event_is_published_for_a_commit_and_never_for_a_rollback(client, database, monkeypatch):
    monkeypatch.setattr(settings, "viewsets_event_store", MemoryEventStore())
    monkeypatch.setattr(settings, "viewsets_command_middleware", [ChangeEvents(), AdormSession(atomic=True)])

    response = await client.post("/members/bulk", json=[{"name": "Ana II", "email": "ana@example.com"}])
    assert response.status_code == 422
    response = await client.post("/members", json={"name": "Fran", "email": "fran@example.com"})
    assert response.status_code == 200

    entries = await get_store().since("/members", 0)
    created = response.json()["id"]
    assert [(entry.name, entry.payload) for entry in entries] == [
        ("records_changed", {"op": "created", "changes": [{"pk": created}]})
    ]
    assert await database.fetchval("SELECT count(*) FROM backends_adormmember") == 6


@pytest.mark.asyncio
@pytest.mark.usefixtures("adorm_driver", "members")
async def test_using_chooses_the_profile_of_every_operation():
    class Reports(MemberViewSet):
        using = "reports"

    session = await Reports().get_session(None)
    assert (session.using, session.session_name) == ("reports", "default")
    with pytest.raises(adorm.InterfaceError):
        await Reports().perform_retrieve(None, 1)


# ---------------------------------------------------------------------------
# Concurrency, cancellation, timeouts, broken connections
# ---------------------------------------------------------------------------


def rating(value: int) -> Member:
    return Member.model_construct(_fields_set={"rating"}, rating=value)


@pytest.mark.asyncio
@pytest.mark.usefixtures("members")
@pytest.mark.parametrize("_attempt", range(3))
async def test_overlapping_bulk_updates_in_opposite_orders_do_not_deadlock(client, database, row_lock, _attempt):
    await row_lock.hold(1)
    forward = asyncio.create_task(client.patch("/members/bulk", json={"1": {"rating": 1}, "2": {"rating": 1}}))
    backward = asyncio.create_task(client.patch("/members/bulk", json={"2": {"rating": 2}, "1": {"rating": 2}}))
    # Both wait for row 1, which each locks first; neither holds row 2 yet.
    await waiting_for_locks(database, 2)
    await row_lock.release()
    first, second = await asyncio.gather(forward, backward)
    assert (first.status_code, second.status_code) == (200, 200), (first.text, second.text)
    assert (await row(database, 1))["rating"] == (await row(database, 2))["rating"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("members")
@pytest.mark.parametrize("_attempt", range(3))
async def test_concurrent_partial_updates_of_one_row_both_take_effect(client, database, row_lock, _attempt):
    await row_lock.hold(1)
    rename = asyncio.create_task(client.patch("/members/1", json={"name": "Anja"}))
    rerate = asyncio.create_task(client.patch("/members/1", json={"rating": 1}))
    await waiting_for_locks(database, 2)
    await row_lock.release()
    responses = await asyncio.gather(rename, rerate)
    assert [response.status_code for response in responses] == [200, 200]
    stored = await row(database, 1)
    assert (stored["name"], stored["rating"]) == ("Anja", 1)


@pytest.mark.asyncio
@pytest.mark.usefixtures("adorm_driver", "members")
@pytest.mark.parametrize("_attempt", range(3))
async def test_a_cancelled_update_writes_nothing_and_leaks_no_slot(database, row_lock, _attempt):
    viewset = MemberViewSet()
    await row_lock.hold(1)
    update = asyncio.create_task(viewset.perform_update(None, 1, rating(0)))
    await waiting_for_locks(database, 1)
    update.cancel()
    with pytest.raises(asyncio.CancelledError):
        await update
    # The statement was cancelled in the database, not left waiting behind the lock.
    await waiting_for_locks(database, 0)
    await row_lock.release()
    assert (await row(database, 1))["rating"] == 5

    # Both slots of the pool are free again: two updates hold one each at the same time.
    await row_lock.hold(3)
    blocked = [asyncio.create_task(viewset.perform_update(None, 3, rating(value))) for value in (1, 2)]
    await waiting_for_locks(database, 2)
    await row_lock.release()
    await asyncio.gather(*blocked)
    assert (await row(database, 3))["rating"] in (1, 2)


@pytest.mark.asyncio
@pytest.mark.usefixtures("adorm_driver", "members")
async def test_a_lock_timeout_fails_the_update_and_the_next_one_succeeds(row_lock):
    viewset = MemberViewSet()
    await row_lock.hold(1)
    with adorm.connection_options(lock_timeout=0.05), pytest.raises(adorm.OperationalError):
        await viewset.perform_update(None, 1, rating(0))
    await row_lock.release()
    assert (await viewset.perform_update(None, 1, rating(0))).rating == 0


@pytest.mark.asyncio
@pytest.mark.usefixtures("members")
async def test_requests_after_the_server_dropped_the_connections_recover(client, database):
    assert (await client.get("/members/1")).status_code == 200
    await database.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname = current_database() AND pid <> pg_backend_pid()"
    )
    statuses = []
    for _ in range(3):
        try:
            statuses.append((await client.get("/members/1")).status_code)
        except adorm.Error:
            statuses.append("error")
    assert statuses[-2:] == [200, 200]


def test_adorms_validation_error_raised_anywhere_is_answered_as_djangos():
    from ..error_responses import error_body, error_status

    error = adorm.ValidationError(
        {"email": adorm.ValidationError("Too long.", code="max_length", params={"limit_value": 9})}
    )
    assert error_status(error) == 422
    assert error_body(error)["field_errors"] == {
        "email": [{"code": "fastapi-viewsets.max_length", "params": {"maxLength": 9}, "detail": "Too long."}]
    }
