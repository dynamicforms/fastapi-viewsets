"""
A viewset backed by the Django ORM.

This is the second real backend, and it exists as much to constrain the design as to be useful: an
abstraction built only against an in-memory dict inevitably assumes the records are already in
memory, and you find out when the second backend arrives. A QuerySet is lazy, iterates
asynchronously, and can absorb a filter, an ordering and a slice into SQL - so it exercises every
push-down path the pipeline offers, and one it cannot (see `apply_sort` on descending NULL
placement).

    class TrackViewSet(DjangoORMViewSet[int, Track], PaginatedListMixin[Track, TrackFilter]):
        model = TrackModel
        schema = Track

Requires the `django` extra. Django's ORM is synchronous underneath; every call here uses either
its native async API (`aget`, `acount`, `__aiter__`, added in Django 4.1) or `sync_to_async`, so
nothing blocks the event loop.
"""

from typing import Any, ClassVar, Generic, TYPE_CHECKING

from asgiref.sync import sync_to_async
from fastapi import HTTPException
from pydantic import BaseModel
from typing_extensions import TypeVar

from ..context import Context
from ..filters import (
    can_compile_all,
    compile_all,
    compiles,
    Contains,
    Exact,
    filters_from,
    Gt,
    Gte,
    IContains,
    IExact,
    In,
    IsNull,
    Lt,
    Lte,
    StartsWith,
)
from ..list_query import ListQuery, ListRecords
from ..mixins import ImplMixin, SortDirection

if TYPE_CHECKING:
    from django.db.models import Model, QuerySet

K = TypeVar("K")
T = TypeVar("T", bound=BaseModel)


class DjangoORMViewSet(ImplMixin[K, T], Generic[K, T]):
    model: ClassVar[type["Model"]]
    """The Django model this viewset reads and writes."""

    schema: ClassVar[type[BaseModel]]
    """The pydantic model rows are converted into on the way out."""

    pk_field: ClassVar[str] = "pk"
    """
    Name of the primary key field. `pk` is Django's own alias and works whatever the field is
    really called, which is why it is the default rather than a literal `id`.
    """

    def get_queryset(self, context: Context) -> "QuerySet":  # noqa: ARG002 - for overrides to narrow per user
        """
        The base queryset. Override to scope it - by tenant, by permission, by anything on the
        context - and everything downstream narrows what you return here rather than replacing it.
        """
        return self.model.objects.all()

    # -----------------------------------------------------------------------
    # Conversion
    # -----------------------------------------------------------------------

    def to_record(self, raw: Any) -> T:
        """
        Django row to pydantic model. Runs on the page only, never on the whole queryset - see
        `ListMixin.apply_pagination`.
        """
        if isinstance(raw, self.schema):
            return raw
        return self.schema.model_validate(raw, from_attributes=True)

    # -----------------------------------------------------------------------
    # List pipeline
    # -----------------------------------------------------------------------

    async def perform_list(self, context: Context) -> ListRecords:
        """
        Returns the queryset itself, unevaluated. Nothing is read until a later stage asks for
        rows, and by then the filter, the ordering and the slice are all part of the SQL.
        """
        return self.get_queryset(context)

    async def apply_filter(self, context: Context, query: ListQuery, records: ListRecords) -> ListRecords:
        """
        Translates the filter model into `.filter(...)` where it can, and leaves the rest to the
        in-memory default.

        Only fields that map to a concrete model field are pushed down, and the stage is marked
        applied only when *every* set field was - a filter half-done in SQL and half-forgotten
        would silently return too many rows.
        """
        queryset = _as_queryset(records)
        if queryset is None or not query.has_filter or not query.needs("filter"):
            return await super().apply_filter(context, query, records)

        filter_set = filters_from(query.fltr)
        if filter_set is None:
            # A hand-written filter model, on the older filter_list path. Nothing declarative to
            # translate, so the in-memory pass takes it.
            return await super().apply_filter(context, query, records)

        concrete = self._concrete_fields()
        translatable = all(fltr.field in concrete for fltr in filter_set)
        if not translatable or not can_compile_all(type(self), filter_set):
            return await super().apply_filter(context, query, records)

        queryset = compile_all(type(self), self, filter_set, queryset)
        query.mark_applied("filter")
        return await super().apply_filter(context, query, queryset)

    def _concrete_fields(self) -> set[str]:
        return {field.name for field in self.model._meta.get_fields() if getattr(field, "concrete", False)}

    async def apply_sort(self, context: Context, query: ListQuery, records: ListRecords) -> ListRecords:
        """
        Translates the sort order into `.order_by(...)`.

        Not pushed down when any column is descending, because this library defines NULL as the
        smallest value in *both* directions while SQL's DESC puts NULLs first. Django can express
        the fix (`F(col).desc(nulls_last=True)`), but only per backend and not on every version, so
        the honest thing is to decline and let the in-memory sort - which already implements the
        intended semantics - do it. That fallback is not a wart in the design; it is the design
        working.
        """
        queryset = _as_queryset(records)
        if queryset is None or not query.has_sort or not query.needs("sort"):
            return await super().apply_sort(context, query, records)

        if any(column.direction == SortDirection.desc for column in query.sort):
            return await super().apply_sort(context, query, records)

        concrete = {field.name for field in self.model._meta.get_fields() if getattr(field, "concrete", False)}
        if not all(column.column_name in concrete for column in query.sort):
            return await super().apply_sort(context, query, records)

        queryset = queryset.order_by(*(column.column_name for column in query.sort))
        query.mark_applied("sort")
        return await super().apply_sort(context, query, queryset)

    async def take_page(self, query: ListQuery, records: ListRecords) -> tuple[list, bool]:
        """
        Slices in SQL rather than in Python.

        The default walks the source and stops once the page is full - which for a queryset means
        streaming every skipped row across the connection before throwing it away. Slicing the
        queryset turns that into LIMIT/OFFSET, so page 500 costs the same as page 1. One row beyond
        the page is still fetched, and it is what answers `has_more`.
        """
        queryset = _as_queryset(records)
        if queryset is None:
            return await super().take_page(query, records)

        rows = [row async for row in queryset[query.offset : query.offset + query.limit + 1]]
        query.mark_applied("pagination")
        return rows[: query.limit], len(rows) > query.limit

    async def count_records(self, context: Context, query: ListQuery, records: ListRecords) -> int | None:
        """One extra SELECT COUNT(*), rather than the null a lazy source would otherwise report."""
        queryset = _as_queryset(records)
        if queryset is None:
            return await super().count_records(context, query, records)
        return await queryset.acount()

    # -----------------------------------------------------------------------
    # Single-record operations
    # -----------------------------------------------------------------------

    async def perform_retrieve(self, context: Context, pk: K) -> T:
        return self.to_record(await self._get_or_404(context, pk))

    async def perform_create(self, context: Context, data: T) -> T:  # noqa: ARG002
        instance = await self.model.objects.acreate(**self._writable_fields(data))
        return self.to_record(instance)

    async def perform_bulk_create(self, context: Context, data: list[T]) -> list[T]:  # noqa: ARG002
        instances = [self.model(**self._writable_fields(record)) for record in data]
        created = await sync_to_async(self.model.objects.bulk_create)(instances)
        return [self.to_record(instance) for instance in created]

    async def perform_update(self, context: Context, pk: K, data: T, partial: bool = True) -> T:
        instance = await self._get_or_404(context, pk)
        fields = self._writable_fields(data, exclude_unset=partial)
        for name, value in fields.items():
            setattr(instance, name, value)
        await instance.asave()
        return self.to_record(instance)

    async def perform_bulk_update(self, context: Context, records: dict[K, T], partial: bool = True) -> list[T]:
        return [await self.perform_update(context, pk, data, partial=partial) for pk, data in records.items()]

    async def perform_destroy(self, context: Context, pk: K) -> dict:
        instance = await self._get_or_404(context, pk)
        await instance.adelete()
        return {"id": pk}

    async def perform_bulk_destroy(self, context: Context, pk: list[K]) -> list[dict]:
        return [await self.perform_destroy(context, one) for one in pk]

    # -----------------------------------------------------------------------

    async def _get_or_404(self, context: Context, pk: K) -> "Model":
        from django.core.exceptions import ObjectDoesNotExist

        try:
            return await self.get_queryset(context).aget(**{self.pk_field: pk})
        except ObjectDoesNotExist:
            raise HTTPException(status_code=404, detail=f"Item with pk {pk} not found") from None

    def _writable_fields(self, data: T, exclude_unset: bool = False) -> dict[str, Any]:
        """
        The model's own fields, minus the primary key - the database assigns that, and letting a
        request body set it is how you overwrite somebody else's row.
        """
        editable = {
            field.name
            for field in self.model._meta.get_fields()
            if getattr(field, "concrete", False) and not getattr(field, "primary_key", False)
        }
        dumped = data.model_dump(exclude_unset=exclude_unset) if hasattr(data, "model_dump") else dict(data)
        return {name: value for name, value in dumped.items() if name in editable}


def _as_queryset(records: ListRecords) -> "QuerySet | None":
    """
    `records` if it is still an unevaluated QuerySet, else None.

    Once an earlier stage has fallen back to the in-memory path, what flows on is a list, and every
    push-down below has to decline - which is exactly what returning None makes them do.
    """
    try:
        from django.db.models import QuerySet
    except ImportError:  # pragma: no cover - the django extra is not installed
        return None
    return records if isinstance(records, QuerySet) else None


# ---------------------------------------------------------------------------
# Filter compilers
# ---------------------------------------------------------------------------
# Registered here rather than on the filters themselves. A filter knows only how to answer in
# memory; how it becomes a queryset is this backend's business, and keeping it here means a new
# operator needs no change to this file's imports being complete, while a new backend needs no
# change to any filter. See fastapi_viewsets/filters/registry.py.

_LOOKUPS = {
    Exact: "exact",
    IExact: "iexact",
    Contains: "contains",
    IContains: "icontains",
    StartsWith: "startswith",
    Gt: "gt",
    Gte: "gte",
    Lt: "lt",
    Lte: "lte",
    In: "in",
    IsNull: "isnull",
}
"""
Every one of these happens to be spelled the same way in Django's own lookup language, so the
compilers are one shared function rather than eleven near-identical ones. `Overlaps` is absent on
purpose: it has no portable Django equivalent (ArrayField's `overlap` is Postgres-only), so a
viewset using it falls back to the in-memory pass - which is the contract working, not a gap.
"""


def _compile_lookup(_viewset, fltr, queryset):
    return queryset.filter(**{f"{fltr.field}__{_LOOKUPS[type(fltr)]}": fltr.value})


for _filter_type in _LOOKUPS:
    compiles(DjangoORMViewSet, _filter_type)(_compile_lookup)
