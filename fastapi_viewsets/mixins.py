from abc import ABC, abstractmethod
from enum import Enum
from typing import Annotated, Any, final, Generic, get_args, get_origin, Union

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, create_model
from pydantic.alias_generators import to_camel
from typing_extensions import TypeVar

from fastapi_viewsets.conf import settings
from fastapi_viewsets.context import Context
from fastapi_viewsets.cursor import (
    compare_in_order,
    cursor_keys,
    CursorError,
    CursorKeys,
    CursorPage,
    CursorState,
    decode_cursor,
    fingerprint,
    make_predicate,
    position_of,
)
from fastapi_viewsets.filters import filters_from, FilterSet
from fastapi_viewsets.list_query import (
    build_list_query,
    ListQuery,
    ListRecords,
    materialize,
    PaginatedList,
    take_page,
)
from fastapi_viewsets.list_shapes import ListOf, resolve_shapes
from fastapi_viewsets.response_classes import ErrorDetail, NOT_FOUND_RESPONSE

T = TypeVar("T")
K = TypeVar("K")
TFilter = TypeVar("TFilter", default=None)


class ImplMixin(Generic[K, T], ABC):
    @abstractmethod
    async def perform_create(self, context: Context, data: T) -> T:
        """
        Subclasses should implement this method to perform the actual creation.
        """
        raise NotImplementedError("Method 'perform_create' must be implemented.")

    @abstractmethod
    async def perform_bulk_create(self, context: Context, data: list[T]) -> list[T]:
        """
        Subclasses should implement this method to perform the actual bulk creation.
        """
        raise NotImplementedError("Method 'perform_bulk_create' must be implemented.")

    @abstractmethod
    async def perform_list(self, context: Context) -> ListRecords:
        """
        Subclasses should implement this method to perform the actual listing.

        A lazy source - a generator, an unevaluated QuerySet - is what an overridden `apply_filter`,
        `apply_sort` or `take_page` pushes its work into, so returning one is what lets a backend
        answer part of the query in its own store. The defaults push into nothing: they land the
        source and work in memory, which is what a materialised list gets either way.

        The return type is bare, which is `ListRecords[Any]`: what a backend yields is its own row
        type, and records are `T` only once `to_record` has run over them.
        """
        raise NotImplementedError("Method 'perform_list' must be implemented.")

    @abstractmethod
    async def perform_retrieve(self, context: Context, pk: K) -> T:
        """
        Subclasses should implement this method to perform the actual retrieval.
        """
        raise NotImplementedError("Method 'perform_retrieve' must be implemented.")

    @abstractmethod
    async def perform_update(self, context: Context, pk: K, data: T, partial: bool = True) -> T:
        """
        Subclasses should implement this method to perform the actual update.
        """
        raise NotImplementedError("Method 'perform_update' must be implemented.")

    @abstractmethod
    async def perform_bulk_update(self, context: Context, records: dict[K, T], partial: bool = True) -> list[T]:
        """
        Subclasses should implement this method to perform the actual bulk update.
        """
        raise NotImplementedError("Method 'perform_bulk_update' must be implemented.")

    @abstractmethod
    async def perform_destroy(self, context: Context, pk: K) -> dict[K, Any]:
        """
        Subclasses should implement this method to perform the actual destruction.
        """
        raise NotImplementedError("Method 'perform_destroy' must be implemented.")

    @abstractmethod
    async def perform_bulk_destroy(self, context: Context, pk: list[K]) -> list[dict[K, Any]]:
        """
        Subclasses should implement this method to perform the actual bulk destruction.
        """
        raise NotImplementedError("Method 'perform_bulk_destroy' must be implemented.")


###################################################################################################
# CREATE
###################################################################################################
class CreateMixin(Generic[K, T], ABC):
    """
    Create a model instance.
    """

    __router = APIRouter()

    @final
    @__router.post("")
    async def create(self: "ImplMixin[K, T] | CreateMixin[K ,T]", context: Context, data: T) -> T:
        return await self.perform_create(context, data)


class BulkOnlyCreateMixin(Generic[K, T], ABC):
    """
    Create model instances in bulk.
    """

    __router = APIRouter()

    @final
    @__router.post("bulk")
    async def bulk_create(
        self: "ImplMixin[K, T] | BulkOnlyCreateMixin[K ,T]", context: Context, data: list[T]
    ) -> list[T]:
        return await self.perform_bulk_create(context, data)


class BulkCreateMixin(CreateMixin[K, T], BulkOnlyCreateMixin[K, T]):
    """
    Create model instances (single or bulk).
    """


###################################################################################################
# LIST
###################################################################################################
def make_all_optional(model: type[BaseModel]) -> type[BaseModel]:
    """
    Adjusts all fields of the provided BaseModel class to be optional, effectively creating
    a new model where all attributes are nullable and default to None. This can be useful
    when creating filter models or scenarios where optional attributes are required.

    :param model: The input Pydantic BaseModel class to process.
    :type model: type[BaseModel]
    :return: A new Pydantic model with all fields converted to optional.
    :rtype: type[BaseModel]
    """
    fields = {}
    for field_name, field_info in model.model_fields.items():
        ann = field_info.annotation
        if not (get_origin(ann) is Union and type(None) in get_args(ann)):
            ann = ann | None
        fields[field_name] = (ann, None)
    return create_model(f"{model.__name__}Filter", **fields)


class FilterParam:
    pass


def filter_set_for(query: ListQuery) -> FilterSet:
    """
    Every filter this request implies: what the client declared, plus what the pipeline added -
    the cursor predicate. One set, so a backend compiles them together or declines them together.
    """
    declared = filters_from(query.fltr)
    return FilterSet(list(declared or []) + list(query.extra_filters))


###################################################################################################
# SORT
###################################################################################################
class SortDirection(str, Enum):
    asc = "asc"
    desc = "desc"


class SortStateColumn(BaseModel):
    """
    Mirrors the FE SortStateColumn interface: one column in the current sort order.
    Python attribute names use snake_case; JSON serialization uses camelCase (columnName).
    """

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)

    column_name: str
    direction: SortDirection = SortDirection.asc


SortState = list[SortStateColumn]


def parse_sort_param(sort_csv: str | None) -> SortState:
    """
    Parse the 'sort' query parameter (comma-separated) into a SortState.

    The value must be a comma-separated list of 'columnName:direction' or just 'columnName'
    (direction defaults to asc). Entries with an unrecognised direction are silently skipped.

    Example: 'title:asc,artist:desc' → [SortStateColumn(column_name='title', direction=asc), ...]
    """
    if not sort_csv:
        return []
    result: SortState = []
    for param in sort_csv.split(","):
        param = param.strip()
        if not param:
            continue
        column, _, raw_direction = param.rpartition(":")
        if not column:
            column, raw_direction = param, SortDirection.asc.value
        try:
            result.append(SortStateColumn(column_name=column, direction=SortDirection(raw_direction)))
        except ValueError:
            pass  # skip entries with an unrecognised direction
    return result


###################################################################################################
# LIST
###################################################################################################
class ListMixin(Generic[T, TFilter], ABC):
    """
    List a queryset, in whichever shape the viewset declares - see fastapi_viewsets/list_shapes.py.

        class TrackViewSet(CollectionViewSet[int, Track], ListMixin[Track, TrackFilter]):
            list_shape = "cursor"                 # what this endpoint answers in
            list_shapes = ("cursor", "plain")     # what a client may ask for instead

    Declaring one shape - the default - keeps the schema down to that single model and the endpoint
    down to the parameters it actually uses. Listing more adds an `X-List-Shape` header and turns
    the response into a union of exactly those models; `route_viewset` computes both from these two
    attributes, so nothing here is written twice.
    """

    __router = APIRouter()

    list_shape: str | None = None
    """This endpoint's shape. None defers to `settings.default_list_shape`."""

    list_shapes: tuple[str, ...] | None = None
    """
    Shapes a client may request with `X-List-Shape`. None means "only the default", which is the
    case worth optimising for: a union nothing asked for is worse documentation than no union.
    """

    default_page_size: int = 100
    """Page size when a paged shape is asked for without one."""

    max_page_size: int = 1000
    """Ceiling on `limit`, so paging cannot be turned back into "fetch everything"."""

    pk_field_name: str = "id"
    """
    Appended to the ordering by the cursor shape, to make every key tuple unique.

    A backend that can read its store's schema supplies the real name instead: `DjangoORMViewSet`
    takes the model's primary key. This default is what a store the library cannot introspect uses,
    and what such a backend falls back to when the response model names the key something the store
    does not.
    """

    nulls: str = "first"
    """
    Where NULL rows sit **when sorting ascending**: `"first"` or `"last"`.

    Descending reverses it, because NULL is treated as a value below or above every other rather
    than as a row pinned to an edge - `"last"` means "the largest thing there is", so descending
    puts it in front. SQL's `NULLS LAST` is the other reading, fixed regardless of direction; a
    backend translating this flips the placement it emits per key.

    The in-memory sort, the cursor's comparison and the SQL a backend emits all use it, which is
    what stops a cursor walking straight past the NULL rows.
    """

    @property
    def nulls_first(self) -> bool:
        return self.nulls != "last"

    @classmethod
    def resolve_shapes(cls) -> tuple:
        return resolve_shapes(cls.list_shape, cls.list_shapes, settings.default_list_shape)

    @final
    @__router.get("")
    async def list_items(
        self: "ImplMixin[Any, T] | ListMixin[T]",
        context: Context,
        fltr: Annotated[TFilter, Query()] = None,
        sort: str | None = None,
        offset: int = 0,
        limit: int | None = None,
        cursor: str | None = None,
        x_list_shape: Annotated[str | None, Header()] = None,
    ) -> Union[ListOf[T], PaginatedList[T], CursorPage[T]]:  # noqa: UP007 - see list_shapes.response_model
        """List the collection."""
        # Declares every parameter and every shape any viewset could ask for; route_viewset narrows
        # both before FastAPI sees them, dropping the parameters this viewset's shapes cannot use
        # and shrinking the return type to the models it can actually produce. Spelled out rather
        # than left as `Any` so that reading the method tells you what it can return.
        #
        # The docstring is deliberately one plain line: FastAPI publishes it as the endpoint's
        # description, identically on every viewset in an application. Anything worth saying about
        # a particular one belongs in @endpoint_docs.
        default, allowed = self.resolve_shapes()
        shape = x_list_shape or default
        if shape not in allowed:
            detail = ErrorDetail(
                message=f"unsupported list shape {shape!r}; this endpoint offers {', '.join(allowed)}",
                code="unsupported_list_shape",
                params={"shape": shape, "allowed": list(allowed)},
            )
            raise HTTPException(status_code=422, detail=detail.model_dump())

        query = build_list_query(
            fltr,
            sort,
            offset=offset,
            limit=(
                None
                if shape == "plain"
                else min(limit if limit is not None else self.default_page_size, self.max_page_size)
            ),
        )
        query.shape = shape
        query.cursor = cursor if shape == "cursor" else None
        return await self.get_list(context, query)

    list_items.__list_shape_aware__ = True
    """Tells route_viewset to prune this signature and compute its response model per viewset."""

    async def get_list(self: "ImplMixin[Any, T] | ListMixin[T]", context: Context, query: ListQuery) -> Any:
        """
        The list pipeline, in one place and overridable as a whole.

        Each stage is a separate method so a subclass can replace just the one it cares about and
        call `super()` for the rest - a viewset that can sort in its database overrides
        `apply_sort`, marks the stage applied, and everything else keeps working unchanged.
        """
        if query.shape is None:
            query.shape = self.resolve_shapes()[0]
        if query.shape == "cursor":
            if query.limit is None:
                query.limit = self.default_page_size
            self._prepare_cursor(query)
        records = await self.perform_list(context)
        records = await self.apply_filter(context, query, records)
        records = await self.apply_sort(context, query, records)
        return await self.apply_pagination(context, query, records)

    async def apply_filter(
        self: "ImplMixin[Any, T] | ListMixin[T]",
        context: Context,  # noqa: ARG002 - part of the hook signature, for overrides to use
        query: ListQuery,
        records: ListRecords,
    ) -> ListRecords:
        """
        Narrows `records` to those the filter accepts. Override to filter in the backend; mark the
        stage applied so this default does not redo the work:

            async def apply_filter(self, context, query, records):
                if query.has_filter:
                    records = self.db.where(...)
                    query.mark_applied("filter")
                return await super().apply_filter(context, query, records)

        The default delegates to the older `filter_list` hook, which stays supported.
        """
        if not query.has_filter or not query.needs("filter"):
            return records

        declared = filters_from(query.fltr)
        combined = filter_set_for(query)
        if combined:
            records = combined.apply(await self.land(query, records))

        # A filter model built by make_filter_model says which operators it stands for and needs no
        # code; a hand-made one does not, and still goes through filter_list.
        if query.fltr is not None and declared is None:
            materialized = await self.land(query, records)
            filtered = await self.filter_list(query.fltr, materialized)
            # filter_list has no default implementation and so returns None when a viewset declares
            # a filter type but never implements filtering. Returning that verbatim would answer
            # the request with null; keeping the unfiltered records is wrong too, but visibly so.
            records = materialized if filtered is None else filtered
        return records

    async def apply_sort(
        self: "ImplMixin[Any, T] | ListMixin[T]",
        context: Context,  # noqa: ARG002 - part of the hook signature, for overrides to use
        query: ListQuery,
        records: ListRecords,
    ) -> ListRecords:
        """
        Orders `records`. Override to sort in the backend and mark the stage applied; the default
        delegates to the older `sort_list` hook.
        """
        if not query.has_sort or not query.needs("sort"):
            return records
        return await self.sort_list(query.sort, await self.land(query, records))

    async def land(self: "ImplMixin[Any, T] | ListMixin[T]", query: ListQuery, records: ListRecords) -> list:
        """
        Ends the lazy part: materialises the source and converts it to the response model.

        Every in-memory stage goes through this, because a filter declaration is written against
        the *response* model - `make_filter_model(Track, ...)` names Track's fields - while the
        source yields whatever the backend stores. Where the two shapes differ the filter would
        otherwise be reading the wrong thing: the demo's SQLite backend keeps `genres` as a
        comma-joined string, and `genres__overlaps` against that matched nothing at all while
        looking like it worked.

        Idempotent, so a later stage does not convert twice, and `apply_pagination` knows from
        `query.applied` whether the page still needs it. `to_record` implementations must tolerate
        being handed an already-converted record for the same reason.
        """
        materialized = await materialize(records)
        if query.needs("conversion"):
            query.mark_applied("conversion")
            return self._to_records(materialized)
        return materialized

    def _prepare_cursor(self, query: ListQuery) -> None:
        """
        Sets the cursor up before the pipeline runs.

        The ordering is rewritten to the full key list - the client's sort plus the primary key -
        because the position tuple is read against exactly those keys, and reversed outright when
        reading backwards so that both the backend and the in-memory sort produce the direction the
        page is actually read in.
        """
        keys = cursor_keys(query.sort, self.pk_field_name)
        fingerprint_value = fingerprint(keys, query.fltr)
        state = None
        if query.cursor:
            try:
                state = decode_cursor(query.cursor, keys, fingerprint_value, self._cursor_annotations())
            except CursorError as error:
                # The client sent something wrong, not the server - a stale cursor after a sort
                # change is the ordinary case, and it deserves a message rather than a traceback.
                detail = ErrorDetail(message=str(error), code=error.code, params=error.params)
                raise HTTPException(status_code=400, detail=detail.model_dump()) from None
            query.extra_filters.append(make_predicate(state, keys, self.nulls_first))

        backwards = bool(state and state.backwards)
        query.sort = [
            SortStateColumn(
                column_name=name,
                direction=SortDirection.desc if descending != backwards else SortDirection.asc,
            )
            for name, descending in keys
        ]
        query._cursor_keys = keys
        query._cursor_query = fingerprint_value
        query._cursor_backwards = backwards

    def _cursor_annotations(self) -> dict[str, Any]:
        """
        The response model's field types, used to coerce cursor values back from JSON. Without them
        a numeric key would be compared as a string and match nothing.
        """
        model = getattr(self, "schema", None)
        if model is None or not hasattr(model, "model_fields"):
            return {}
        return {name: field_info.annotation for name, field_info in model.model_fields.items()}

    def _cursor_anchors(self, results: list, keys: CursorKeys, fingerprint_value: str) -> dict[str, str | None]:
        """
        Four cursors from the page's own two edge rows.

        `next`/`previous` are exclusive so they never repeat a row. `first`/`last` are the same
        anchors read inclusively - they return their own row again, and in exchange survive rows
        being inserted at that edge, which is what a client polling a live list needs.
        """
        if not results or not keys:
            return dict.fromkeys(("next", "previous", "first", "last"))

        head = position_of(results[0], keys)
        tail = position_of(results[-1], keys)
        return {
            "next": CursorState(tail, backwards=False, inclusive=False, query=fingerprint_value).encode(),
            "previous": CursorState(head, backwards=True, inclusive=False, query=fingerprint_value).encode(),
            "first": CursorState(head, backwards=True, inclusive=True, query=fingerprint_value).encode(),
            "last": CursorState(tail, backwards=False, inclusive=True, query=fingerprint_value).encode(),
        }

    async def _cursor_page(self, query: ListQuery, records: ListRecords) -> "CursorPage[T]":
        page, has_more = await self.take_page(query, records)
        backwards = getattr(query, "_cursor_backwards", False)
        if backwards:
            # The query ran in reversed order to read backwards; the page is handed back the way
            # the client reads it.
            page = list(reversed(page))
        results = page if not query.needs("conversion") else self._to_records(page)

        keys = getattr(query, "_cursor_keys", ())
        anchors = self._cursor_anchors(results, keys, getattr(query, "_cursor_query", ""))

        # Reading backwards, "more in the reading direction" means more *before* the page.
        has_more_forward = has_more if not backwards else bool(query.cursor)
        has_previous = bool(query.cursor) if not backwards else has_more

        return CursorPage[Any](
            results=results,
            limit=query.limit,
            has_more=has_more_forward,
            has_previous=has_previous,
            next=anchors["next"] if has_more_forward else None,
            previous=anchors["previous"] if has_previous else None,
            first=anchors["first"],
            last=anchors["last"],
        )

    async def apply_pagination(
        self: "ImplMixin[Any, T] | ListMixin[T]",
        context: Context,  # noqa: ARG002 - part of the hook signature, for overrides to use
        query: ListQuery,
        records: ListRecords,
    ) -> "list[T] | PaginatedList[T]":
        """
        Cuts one page out of `records`, or returns the lot when the client asked for no page.

        Only reads as far as the page needs, so paging a lazy source stays lazy. Conversion to the
        response model happens here, on the page and nowhere else: the stages before this one carry
        whatever the backend's own source is - a QuerySet, a cursor - because that is the only
        thing they can push a filter or an ordering into. Converting earlier would hand them a bag
        of records with nothing left to push into.
        """
        if query.shape == "cursor":
            return await self._cursor_page(query, records)
        if not query.is_paginated:
            # A plain list, not a ListOf: FastAPI validates it into the declared RootModel on the
            # way out, and everything that calls get_list() directly - tests, other endpoints -
            # keeps getting something it can index and iterate.
            return await self.land(query, records)

        count = await self.count_records(context, query, records)
        page, has_more = await self.take_page(query, records)
        # Already converted if an in-memory stage had to land the source earlier; see land().
        results = page if not query.needs("conversion") else self._to_records(page)
        return PaginatedList[Any](
            results=results,
            offset=query.offset,
            limit=query.limit,
            count=count,
            has_more=has_more,
            has_previous=query.offset > 0,
        )

    async def take_page(
        self: "ImplMixin[Any, T] | ListMixin[T]", query: ListQuery, records: ListRecords
    ) -> tuple[list, bool]:
        """
        Reads one page out of `records`, and whether anything follows it.

        The default walks the source and stops once the page is full, which keeps a generator from
        being drained but still costs `offset` steps to get there - and, against a database, streams
        every skipped row across the wire before discarding it. A backend with a real LIMIT/OFFSET
        should override this and mark the "pagination" stage applied.
        """
        return await take_page(records, query.offset, query.limit)

    async def count_records(
        self: "ImplMixin[Any, T] | ListMixin[T]",
        context: Context,  # noqa: ARG002 - part of the hook signature, for overrides to use
        query: ListQuery,  # noqa: ARG002 - part of the hook signature, for overrides to use
        records: ListRecords,
    ) -> int | None:
        """
        How many records there are in total, or None when finding out would cost too much.

        A list knows its length; a generator does not, and draining it to count would undo the
        point of paging it. A backend that can answer cheaply should override this - a Django
        viewset returns `await queryset.acount()`, one extra SELECT COUNT(*) rather than a full
        read - and a client then gets a real total instead of a null.
        """
        if query.shape == "cursor":
            # Never counted: a total costs a second full pass on every request and is stale by the
            # time it is read, which is most of why a cursor exists.
            return None
        return len(records) if isinstance(records, (list, tuple)) else None

    def to_record(self, raw: Any) -> T:
        """
        Converts one row from whatever the backend produced into the response model.

        The default is identity, for viewsets whose source already yields the model. An ORM-backed
        one overrides it to build the pydantic model from a row. Deliberately synchronous: this is
        CPU work, not I/O, and it runs once per record - anything needing I/O per row wants
        prefetching in `perform_list` instead.
        """
        return raw

    def _to_records(self, rows: list) -> list:
        """
        Skips the conversion pass entirely for viewsets that never override `to_record`, which is
        most of them - the in-memory ones already hold the response model.
        """
        if type(self).to_record is ListMixin.to_record:
            return rows
        return [self.to_record(row) for row in rows]

    async def filter_list(self, fltr: TFilter, records: list[T]) -> list[T]:
        """
        Post-filter hook called after perform_list when a filter is active.
        Subclasses implement this to filter records in-memory.
        """

    async def sort_list(self, sort: SortState, records: list[T]) -> list[T]:
        """
        Post-sort hook called after perform_list (and filter_list) when a sort order is active.
        Default implementation performs a stable in-memory multi-key sort, placing NULLs where
        `nulls` says - through the same comparison the cursor uses. They share it because a cursor
        walking a nullable column against a differently-ordered sort loses every NULL row without
        saying so.
        """
        import functools

        def compare(a: T, b: T) -> int:
            for col in sort:
                order = compare_in_order(
                    getattr(a, col.column_name, None),
                    getattr(b, col.column_name, None),
                    col.direction == SortDirection.desc,
                    self.nulls_first,
                )
                if order != 0:
                    return order
            return 0

        return sorted(records, key=functools.cmp_to_key(compare))


class PaginatedListMixin(ListMixin[T, TFilter], ABC):
    """
    Shorthand for `ListMixin` with `list_shape = "paginated"`.

    Kept because it reads well at the point of use and because it was the way to ask for offset
    paging before shapes existed; it adds nothing `list_shape` does not.
    """

    list_shape = "paginated"


class CursorListMixin(ListMixin[T, TFilter], ABC):
    """
    Shorthand for `ListMixin` with `list_shape = "cursor"`.

    Cursor paging neither re-reads the rows it skipped nor drifts when the collection changes
    under the client; it cannot jump to an arbitrary page or report a total. See list_shapes.py.
    """

    list_shape = "cursor"


###################################################################################################
# RETRIEVE
###################################################################################################
class RetrieveMixin(Generic[K, T], ABC):
    """
    Retrieve a model instance.
    """

    __router = APIRouter()

    @final
    @__router.get("/{pk}", responses=NOT_FOUND_RESPONSE)
    async def retrieve(self: "ImplMixin[K, T] | RetrieveMixin[K, T]", context: Context, pk: K) -> T:
        return await self.perform_retrieve(context, pk)


###################################################################################################
# UPDATE
###################################################################################################
class UpdateMixin(Generic[K, T], ABC):
    """
    Update a model instance.
    """

    __router = APIRouter()

    @final
    @__router.put("/{pk}", responses=NOT_FOUND_RESPONSE)
    async def update(self: "ImplMixin[K, T] | UpdateMixin[K, T]", context: Context, pk: K, data: T) -> T:
        return await self.perform_update(context, pk, data, partial=False)

    @final
    @__router.patch("/{pk}", name="partial_update", responses=NOT_FOUND_RESPONSE)
    async def partial_update(self: "ImplMixin[K, T] | UpdateMixin[K, T]", context: Context, pk: K, data: T) -> T:
        return await self.perform_update(context, pk, data, partial=True)


class BulkOnlyUpdateMixin(Generic[K, T], ABC):
    """
    Update model instances in bulk.
    """

    __router = APIRouter()

    @final
    @__router.put("bulk")
    async def bulk_update(
        self: "ImplMixin[K, T] | BulkOnlyUpdateMixin[K, T]", context: Context, records: dict[K, T]
    ) -> list[T]:
        return await self.perform_bulk_update(context, records, partial=False)

    @final
    @__router.patch("bulk", name="bulk_partial_update")
    async def bulk_partial_update(
        self: "ImplMixin[K, T] | BulkOnlyUpdateMixin[K, T]", context: Context, records: dict[K, T]
    ) -> list[T]:
        return await self.perform_bulk_update(context, records, partial=True)


class BulkUpdateMixin(UpdateMixin[K, T], BulkOnlyUpdateMixin[K, T]):
    """
    Update model instances (single or bulk).
    """


###################################################################################################
# DELETE
###################################################################################################
class DestroyMixin(Generic[K, T], ABC):
    """
    Destroy a model instance. Return the destroyed key and any additional data about its destruction
    """

    __router = APIRouter()

    @final
    @__router.delete("/{pk}", responses=NOT_FOUND_RESPONSE)
    async def destroy(self: "ImplMixin[K, T] | DestroyMixin[K, T]", context: Context, pk: K) -> dict[K, Any]:
        return await self.perform_destroy(context, pk)


class BulkOnlyDestroyMixin(Generic[K, T], ABC):
    """
    Destroy model instances in bulk.
    """

    __router = APIRouter()

    @final
    @__router.delete("bulk")
    async def bulk_destroy(
        self: "ImplMixin[K, T] | BulkOnlyDestroyMixin[K, T]", context: Context, pk: list[K]
    ) -> list[dict[K, Any]]:
        return await self.perform_bulk_destroy(context, pk)


class BulkDestroyMixin(DestroyMixin[K, T], BulkOnlyDestroyMixin[K, T]):
    """
    Destroy model instances (single or bulk).
    """


###################################################################################################
# LOOKUP
###################################################################################################
class LookupItem(BaseModel):
    group: Any = None
    pk: object
    title: str
    icon: str | None = None


class LookupFilter(BaseModel):
    """Default filter model for LookupMixin. Provides case-insensitive title search via q."""

    q: str | None = None


TLookupFilter = TypeVar("TLookupFilter", default=LookupFilter)


class LookupMixin(Generic[TLookupFilter], ABC):
    """
    Lookup endpoint with optional search filtering.

    Adds a GET /lookup endpoint that returns a list of LookupItem objects — useful for populating
    select/autocomplete widgets.

    The mixin is generic over TLookupFilter (default: LookupFilter). When no type argument is
    given, the endpoint exposes a single 'q' query parameter and filter_lookup filters by
    case-insensitive title match. Supply a custom filter model as a type argument to add extra
    query parameters and override filter_lookup.

    When the filter is active (any field is non-None), the following two-phase pipeline runs:

    1. Pre-filter: setup_lookup_filter is called before perform_lookup.
       Override to apply server-side filtering (e.g., narrow a DB query).
    2. Post-filter: filter_lookup is called after perform_lookup.
       The default implementation filters by fltr.q (case-insensitive title match).
       Override to customise in-memory filtering.
    """

    __router = APIRouter()

    @abstractmethod
    async def perform_lookup(self, context: Context) -> list[LookupItem]:
        """
        Subclasses should implement this method to return lookup items.
        This method is intentionally NOT in ImplMixin because it's expected to be a simple
        transformation of perform_list.
        """
        raise NotImplementedError("Method 'perform_lookup' must be implemented.")

    async def setup_lookup_filter(self, fltr: TLookupFilter) -> None:
        """
        Optional pre-filter hook called before perform_lookup when the filter is active.
        Subclasses can override to set up server-side filtering (e.g., build a DB query).
        """

    async def filter_lookup(self, fltr: TLookupFilter, items: list[LookupItem]) -> list[LookupItem]:
        """
        Post-filter hook called after perform_lookup when the filter is active.
        Default implementation filters by fltr.q (case-insensitive substring of title).
        Subclasses can override to change the filtering behaviour.
        """
        q = getattr(fltr, "q", None)
        if q is None:
            return items
        return [item for item in items if q.lower() in item.title.lower()]

    @final
    @__router.get("lookup")
    async def lookup(
        self: "ImplMixin[Any, LookupItem] | LookupMixin",
        context: Context,
        fltr: Annotated[TLookupFilter, Query()] = None,
    ) -> list[LookupItem]:
        has_filter = (
            fltr is not None and hasattr(fltr, "model_dump") and any(v is not None for v in fltr.model_dump().values())
        )
        if has_filter:
            await self.setup_lookup_filter(fltr)
        res = await self.perform_lookup(context)
        if has_filter:
            res = await self.filter_lookup(fltr, res)
        return res


###################################################################################################
# COMBINED VIEWSET MIXINS
###################################################################################################
class ReadOnlyViewSetMixin(ListMixin[T], RetrieveMixin[K, T], Generic[K, T], ABC):
    """
    Read-only viewset. Provides 'list' and 'retrieve' actions.
    """


class ViewSetMixin(
    Generic[K, T, TFilter],
    CreateMixin[K, T],
    ListMixin[T, TFilter],
    RetrieveMixin[K, T],
    UpdateMixin[K, T],
    DestroyMixin[K, T],
    ABC,
):
    """
    Standard full viewset. Provides 'create', 'list', 'retrieve', 'update', 'partial_update' and 'destroy' actions.
    """


class BulkViewSetMixin(
    Generic[K, T, TFilter],
    BulkCreateMixin[K, T],
    ListMixin[T, TFilter],
    RetrieveMixin[K, T],
    BulkUpdateMixin[K, T],
    BulkDestroyMixin[K, T],
    ABC,
):
    """
    Full viewset with bulk support. Provides 'create', 'bulk_create', 'list', 'retrieve', 'update', 'partial_update',
    'bulk_update', 'bulk_partial_update', 'destroy' and 'bulk_destroy' actions.
    """
