import functools

from abc import ABC, abstractmethod
from enum import Enum
from typing import Annotated, Any, final, Generic, get_args, get_origin, Union

from fastapi import APIRouter, Header, Query
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
from fastapi_viewsets.exceptions import CursorRequestError, UnsupportedListShapeError
from fastapi_viewsets.filters import filters_from, FilterSet
from fastapi_viewsets.list_query import (
    build_list_query,
    ListQuery,
    ListRecords,
    materialize,
    PaginatedList,
    take_page,
)
from fastapi_viewsets.list_shapes import resolve_shapes
from fastapi_viewsets.response_classes import NOT_FOUND_RESPONSE

T = TypeVar("T")
K = TypeVar("K")
TFilter = TypeVar("TFilter", default=None)


@functools.cache
def _item_type(cls: type) -> Any:
    """The viewset's list item type `T`; `Any` when unbound. A page of it encodes as the declared result."""
    from fastapi_viewsets.decorators.route_viewset import build_type_map

    item = build_type_map(cls).get(T, Any)
    return Any if isinstance(item, TypeVar) else item


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
        Returns the list source. A lazy one (generator, unevaluated QuerySet) lets overridden stages push
        filtering, sorting and paging into the backend. Rows are the backend's own type until `to_record`.
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
    """Returns a copy of `model`, named `<Model>Filter`, with every field nullable and defaulting to None."""
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
    The request's declared filters plus the pipeline's own (the cursor predicate), as one set that a
    backend compiles or declines as a whole.
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
    """One column of a sort order; mirrors the TS `SortStateColumn` (camelCase in JSON)."""

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)

    column_name: str
    direction: SortDirection = SortDirection.asc


SortState = list[SortStateColumn]


SORT_DESCRIPTION = (
    "Ordering: comma-separated `column:direction` entries in priority order (`name:asc,score:desc`)."
    " The direction is `asc` or `desc`, `asc` when omitted; an entry with any other direction is"
    " ignored. Without the parameter the list is ordered by its default value, where the endpoint"
    " declares one; an empty value leaves the list unordered."
)
"""The `sort` parameter's description on every list endpoint."""


def parse_sort_param(sort_csv: str | None) -> SortState:
    """
    Parses the `sort` query parameter (`title:asc,artist:desc`; direction defaults to asc) into a
    SortState. Entries with an unrecognised direction are skipped.
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


def check_declared_sort(sort_csv: str, model: Any = None) -> SortState:
    """
    Parses `ListMixin.default_sort`. Raises ValueError for an empty entry, an unrecognised direction,
    and, when `model` is a pydantic model, a column that is neither a field nor a computed field.
    """
    entries = [entry.strip() for entry in sort_csv.split(",")]
    if not all(entries):
        raise ValueError(f"default_sort {sort_csv!r} has an empty entry")
    result: SortState = []
    for entry in entries:
        column, _, raw_direction = entry.rpartition(":")
        if not column:
            column, raw_direction = entry, SortDirection.asc.value
        if raw_direction not in SortDirection.__members__:
            raise ValueError(f"default_sort {sort_csv!r}: direction of {column!r} is not 'asc' or 'desc'")
        fields = getattr(model, "model_fields", None)
        if isinstance(fields, dict) and column not in fields and column not in model.model_computed_fields:
            raise ValueError(f"default_sort {sort_csv!r}: {model.__name__} has no field {column!r}")
        result.append(SortStateColumn(column_name=column, direction=SortDirection(raw_direction)))
    return result


###################################################################################################
# LIST
###################################################################################################
class ListMixin(Generic[T, TFilter], ABC):
    """
    List endpoint in the shape(s) set by `list_shape` / `list_shapes` (see list_shapes.py). More than one
    shape adds the `X-List-Shape` header and makes the response a union of those models.
    """

    __router = APIRouter()

    list_shape: str | None = None
    """This endpoint's shape. None defers to `settings.default_list_shape`."""

    list_shapes: tuple[str, ...] | None = None
    """Shapes a client may request with `X-List-Shape`. None means only the default."""

    default_page_size: int = 100
    """Page size when a paged shape is asked for without one."""

    max_page_size: int = 1000
    """Ceiling on `limit`."""

    pk_field_name: str = "id"
    """
    Appended to the cursor ordering to make every key tuple unique. `DjangoORMViewSet` uses the model's
    primary key instead, unless the response model does not have that field.
    """

    nulls: str = "first"
    """
    Where NULLs sit when sorting ascending: `"first"` or `"last"`; descending reverses it (unlike SQL
    `NULLS LAST`). The in-memory sort, the cursor comparison and backend SQL must all agree on it, or a
    cursor skips the NULL rows.
    """

    default_sort: str | None = None
    """
    Ordering of a request without `sort`, in its syntax (`"category:asc,score:desc"`); None, or an empty
    `sort=`, leaves it unordered. Published as the parameter default in the schema; `route_viewset` validates it
    with `check_declared_sort`.
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
        sort: Annotated[str | None, Query(description=SORT_DESCRIPTION)] = None,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int | None, Query(ge=1)] = None,
        cursor: str | None = None,
        x_list_shape: Annotated[str | None, Header()] = None,
    ) -> Union[list[T], PaginatedList[T], CursorPage[T]]:  # noqa: UP007 - see list_shapes.response_model
        """List the collection."""
        # route_viewset prunes these parameters and the return union to what this viewset's shapes use.
        # The docstring is the OpenAPI description on every viewset; per-viewset text goes in @endpoint_docs.
        default, allowed = self.resolve_shapes()
        shape = x_list_shape or default
        if shape not in allowed:
            raise UnsupportedListShapeError(shape, list(allowed))

        query = build_list_query(
            fltr,
            self.default_sort if sort is None else sort,
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
        """The list pipeline: source, filter, sort, pagination. Each stage is overridable on its own."""
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
        Narrows `records` to those the filter accepts, in memory: by a `make_filter_model` declaration,
        otherwise through `filter_list`. An override filtering in the backend calls
        `query.mark_applied("filter")` so this default skips the work.
        """
        if not query.has_filter or not query.needs("filter"):
            return records

        declared = filters_from(query.fltr)
        combined = filter_set_for(query)
        if combined:
            records = combined.apply(await self.land(query, records))

        # A hand-made filter model carries no declaration and goes through filter_list.
        if query.fltr is not None and declared is None:
            materialized = await self.land(query, records)
            filtered = await self.filter_list(query.fltr, materialized)
            # The default filter_list returns None; answering with null would hide the missing override.
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
        delegates to the `sort_list` hook.
        """
        if not query.has_sort or not query.needs("sort"):
            return records
        return await self.sort_list(query.sort, await self.land(query, records))

    async def land(self: "ImplMixin[Any, T] | ListMixin[T]", query: ListQuery, records: ListRecords) -> list:
        """
        Materialises the source and converts it to the response model, which in-memory filters are
        declared against. Idempotent (marks the "conversion" stage); `to_record` must therefore accept
        an already-converted record.
        """
        materialized = await materialize(records)
        if query.needs("conversion"):
            query.mark_applied("conversion")
            return self._to_records(materialized)
        return materialized

    def _prepare_cursor(self, query: ListQuery) -> None:
        """
        Rewrites the ordering to the cursor keys (client sort plus primary key), reversed when reading
        backwards, and adds the cursor predicate to the query's filters.
        """
        keys = cursor_keys(query.sort, self.pk_field_name)
        fingerprint_value = fingerprint(keys, query.fltr)
        state = None
        if query.cursor:
            try:
                state = decode_cursor(query.cursor, keys, fingerprint_value, self._cursor_annotations())
            except CursorError as error:
                # A client error (typically a stale cursor after a sort change), not a server one.
                raise CursorRequestError(error) from None
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
        """The response model's field types, to coerce cursor values from JSON (e.g. numbers, not strings)."""
        model = getattr(self, "schema", None)
        if model is None or not hasattr(model, "model_fields"):
            return {}
        return {name: field_info.annotation for name, field_info in model.model_fields.items()}

    def _cursor_anchors(self, results: list, keys: CursorKeys, fingerprint_value: str) -> dict[str, str | None]:
        """
        Cursors from the page's edge rows: exclusive `next`/`previous`, and inclusive `first`/`last`,
        which repeat their row but survive inserts at that edge (for polling a live list).
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
            # The query ran reversed; return the page in reading order.
            page = list(reversed(page))
        results = page if not query.needs("conversion") else self._to_records(page)

        keys = getattr(query, "_cursor_keys", ())
        anchors = self._cursor_anchors(results, keys, getattr(query, "_cursor_query", ""))

        # Reading backwards, "more in the reading direction" means more *before* the page.
        has_more_forward = has_more if not backwards else bool(query.cursor)
        has_previous = bool(query.cursor) if not backwards else has_more

        return CursorPage[_item_type(type(self))](
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
        Cuts one page out of `records` (or returns all for the plain shape) and converts only that page
        to the response model, unless an earlier in-memory stage already did.
        """
        if query.shape == "cursor":
            return await self._cursor_page(query, records)
        if not query.is_paginated:
            # A plain list: FastAPI validates it into the declared RootModel; direct callers get a list.
            return await self.land(query, records)

        count = await self.count_records(context, query, records)
        page, has_more = await self.take_page(query, records)
        # Already converted if an in-memory stage had to land the source earlier; see land().
        results = page if not query.needs("conversion") else self._to_records(page)
        return PaginatedList[_item_type(type(self))](
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
        Reads one page out of `records`, and whether anything follows it. The default walks past `offset`
        rows; a backend with LIMIT/OFFSET overrides it and marks the "pagination" stage applied.
        """
        return await take_page(records, query.offset, query.limit)

    async def count_records(
        self: "ImplMixin[Any, T] | ListMixin[T]",
        context: Context,  # noqa: ARG002 - part of the hook signature, for overrides to use
        query: ListQuery,  # noqa: ARG002 - part of the hook signature, for overrides to use
        records: ListRecords,
    ) -> int | None:
        """
        Total record count, or None when unknown without draining the source (a generator) or for the
        cursor shape. Override where counting is cheap (e.g. `queryset.acount()`).
        """
        if query.shape == "cursor":
            return None
        return len(records) if isinstance(records, (list, tuple)) else None

    def to_record(self, raw: Any) -> T:
        """
        Converts one backend row into the response model; identity by default. Synchronous: per-row I/O
        belongs in `perform_list` as prefetching.
        """
        return raw

    def _to_records(self, rows: list) -> list:
        """Applies `to_record` to `rows`, skipping the pass when `to_record` is not overridden."""
        if type(self).to_record is ListMixin.to_record:
            return rows
        return [self.to_record(row) for row in rows]

    async def filter_list(self, fltr: TFilter, records: list[T]) -> list[T]:
        """In-memory filter hook for a hand-made filter model. No default implementation (returns None)."""

    async def sort_list(self, sort: SortState, records: list[T]) -> list[T]:
        """
        In-memory sort hook. The default is a stable multi-key sort using the cursor's comparison
        (`compare_in_order`), so NULL placement matches the cursor's.
        """

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
    """Shorthand for `ListMixin` with `list_shape = "paginated"`."""

    list_shape = "paginated"


class CursorListMixin(ListMixin[T, TFilter], ABC):
    """Shorthand for `ListMixin` with `list_shape = "cursor"`."""

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
TLookupItem = TypeVar("TLookupItem", default=LookupItem)


class LookupMixin(Generic[TLookupFilter, TLookupItem], ABC):
    """
    `GET /lookup`: TLookupItem rows for select/autocomplete widgets, filtered by TLookupFilter's fields
    as query parameters. Pipeline: `perform_lookup` -> `apply_lookup_filter` -> `to_lookup_item`. The
    filter travels in `query.fltr`, never on the (singleton) viewset; answers in source order.
    """

    __router = APIRouter()

    @abstractmethod
    async def perform_lookup(self, context: Context) -> ListRecords:
        """
        Returns the lookup source ordered by group, then text, in the source's collation. A lazy source
        lets an overridden `apply_lookup_filter` filter in the backend.
        """
        raise NotImplementedError("Method 'perform_lookup' must be implemented.")

    async def get_lookup(self, context: Context, query: ListQuery) -> list[TLookupItem]:
        """The lookup pipeline: source, filter, conversion. Overridable as a whole."""
        records = await self.perform_lookup(context)
        records = await self.apply_lookup_filter(context, query, records)
        return await self.land_lookup(query, records)

    async def apply_lookup_filter(
        self,
        context: Context,  # noqa: ARG002 - part of the hook signature, for overrides to use
        query: ListQuery,
        records: ListRecords,
    ) -> ListRecords:
        """
        Narrows `records` in memory: by a `make_filter_model` declaration, otherwise through
        `filter_lookup`. An override filtering in the backend calls `query.mark_applied("filter")`.
        """
        if not query.has_filter or not query.needs("filter"):
            return records

        items = await self.land_lookup(query, records)
        declared = filters_from(query.fltr)
        if declared is not None:
            return declared.apply(items)
        filtered = await self.filter_lookup(query.fltr, items)
        return items if filtered is None else filtered

    async def filter_lookup(self, fltr: TLookupFilter, items: list[TLookupItem]) -> list[TLookupItem]:
        """
        In-memory filter hook of a hand-written filter model, called with the converted items.
        The default filters by `fltr.q`: a case-insensitive substring of the item's `title`.
        """
        q = getattr(fltr, "q", None)
        if q is None:
            return items
        needle = q.lower()
        return [item for item in items if needle in str(getattr(item, "title", "")).lower()]

    def to_lookup_item(self, raw: Any) -> TLookupItem:
        """Converts one source row into TLookupItem; identity by default."""
        return raw

    async def land_lookup(self, query: ListQuery, records: ListRecords) -> list[TLookupItem]:
        """Materialises the source and converts it with `to_lookup_item`; idempotent per request."""
        materialized = await materialize(records)
        if query.needs("conversion"):
            query.mark_applied("conversion")
            if type(self).to_lookup_item is not LookupMixin.to_lookup_item:
                return [self.to_lookup_item(row) for row in materialized]
        return materialized

    @final
    @__router.get("lookup")
    async def lookup(
        self: "LookupMixin",
        context: Context,
        fltr: Annotated[TLookupFilter, Query()] = None,
    ) -> list[TLookupItem]:
        return await self.get_lookup(context, build_list_query(fltr, None))


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
