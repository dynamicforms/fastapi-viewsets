"""
A viewset backed by the Django ORM.

This is the second real backend, and it exists as much to constrain the design as to be useful: an
abstraction built only against an in-memory dict inevitably assumes the records are already in
memory, and you find out when the second backend arrives. A QuerySet is lazy, iterates
asynchronously, and can absorb a filter, an ordering and a slice into SQL - so it exercises every
push-down path the pipeline offers.

    class TrackViewSet(DjangoORMViewSet[int, Track], PaginatedListMixin[Track, TrackFilter]):
        model = TrackModel
        schema = Track

The backend comes before the list mixin, and that order is required: `pk_field_name` is a property
here and a plain class attribute on `ListMixin`, so a viewset that lists the mixin first finds the
mixin's `"id"` and never asks the model.

Requires the `django` extra. Django's ORM is synchronous underneath; every call here uses either
its native async API (`aget`, `acount`, `__aiter__`, added in Django 4.1) or `sync_to_async`, so
nothing blocks the event loop.
"""

import operator

from functools import reduce
from typing import Any, ClassVar, Generic, TYPE_CHECKING

from asgiref.sync import sync_to_async
from django.db.models import BooleanField, F, Func, Q, Value
from pydantic import BaseModel
from typing_extensions import TypeVar

from ..context import Context
from ..cursor import CursorPredicate, wants_greater
from ..exceptions import NotFoundError
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
from ..mixins import filter_set_for, ImplMixin, SortDirection

try:
    from django.db.models import CompositePrimaryKey

    _COMPOSITE_PK: tuple[type, ...] = (CompositePrimaryKey,)
except ImportError:  # Django below 5.2 has no composite primary keys; the empty tuple matches nothing
    _COMPOSITE_PK = ()

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
    The lookup `_get_or_404` hands to `aget()` when a record is fetched by its key. `pk` is Django's
    alias for whichever field carries the primary key, so a renamed primary key needs no change
    here; what that field is *called* is `pk_field_name`.
    """

    @property
    def pk_field_name(self) -> str:
        """
        The primary key's name, taken from `Model._meta.pk`.

        The cursor appends it to the ordering and filters on it - and this backend pushes a filter
        set down only when every name in it is a concrete model field - while the position the
        cursor emits is read off the converted *response* record. The name therefore has to be one
        both the model and `schema` carry, or the cursor's own key would take the whole set, the
        client's own filters with it, to the in-memory pass, and emit a position read off a field
        that is not there.

        `_meta.pk` offers two such names, and the first that `schema` declares wins: `name`, and
        `attname` for a relation primary key, which is `owner` on the model and `owner_id` on the
        response model. Neither on the schema means the schema names the key something else - a
        multi-table-inheritance child, whose `_meta.pk` is the parent link - and the default
        declared further along the MRO stands.

        Assigning `pk_field_name` on a subclass overrides this at any depth; assigning it on an
        instance raises `AttributeError`, because this is a read-only property. A subclass with no
        `model` yet takes the same default, so an abstract base is free to leave the model to its
        own subclasses.
        """
        model = getattr(self, "model", None)
        if model is None:
            return super().pk_field_name

        pk = model._meta.pk
        if isinstance(pk, _COMPOSITE_PK):
            raise TypeError(
                f"{type(self).__name__}: {model.__name__} has a composite primary key, which no single "
                f"`pk_field_name` can name. Assign `pk_field_name` on the class, naming one field that is "
                f"unique per row."
            )

        declared = getattr(getattr(self, "schema", None), "model_fields", None)
        if declared is None:
            return pk.name
        for name in (pk.name, pk.attname):
            if name in declared:
                return name
        return super().pk_field_name

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
        Django row to pydantic model. Runs on the page alone for as long as no stage has landed the
        source, which means the filter and the sort each pushed down or had nothing to do; a
        declined `take_page` still cuts its page out of the queryset. A declined filter or sort runs
        this on whatever reached that stage instead - the whole table where the filter declined -
        because the in-memory pass goes through `land()`, which converts what it materialises. Every
        one of those rows and its pydantic model are held at once, before the page is cut, so
        `limit=10` against a table of hundreds of thousands of rows can exhaust memory.
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

        filter_set = filter_set_for(query)
        if not filter_set and filters_from(query.fltr) is None:
            # A hand-written filter model, on the older filter_list path. Nothing declarative to
            # translate, so the in-memory pass takes it.
            return await super().apply_filter(context, query, records)

        concrete = self._concrete_fields()
        translatable = all(all(name in concrete for name in fltr.fields()) for fltr in filter_set)
        if not translatable or not can_compile_all(type(self), filter_set):
            return await super().apply_filter(context, query, records)

        queryset = compile_all(type(self), self, filter_set, queryset)
        query.mark_applied("filter")
        return await super().apply_filter(context, query, queryset)

    def _concrete_fields(self) -> set[str]:
        """
        Every name that names a column in a query, both halves of a relation's pair: a foreign key
        is `author` and `author_id`, and both are lookups Django accepts. The response model
        carries the second, so leaving it out would decline push-down for a schema field that maps
        to a column perfectly well.
        """
        names = set()
        for field in self.model._meta.get_fields():
            if getattr(field, "concrete", False):
                names.add(field.name)
                names.add(field.attname)
        return names

    async def apply_sort(self, context: Context, query: ListQuery, records: ListRecords) -> ListRecords:
        """
        Translates the sort order into `.order_by(...)`, with the viewset's NULL placement.

        `F(col).asc(nulls_first=...)` states the placement outright, which is the same thing
        `nulls` means and the same thing the in-memory comparison does - so both directions push
        down and the two agree about where NULLs went.
        """
        queryset = _as_queryset(records)
        if queryset is None or not query.has_sort or not query.needs("sort"):
            return await super().apply_sort(context, query, records)

        concrete = self._concrete_fields()
        if not all(column.column_name in concrete for column in query.sort):
            return await super().apply_sort(context, query, records)

        # `nulls` says where NULLs go when ascending, so a descending key needs the opposite SQL
        # placement to mean the same thing. One flag or the other, and only ever True: Django
        # rejects False and refuses both at once.
        ordering = []
        for column in query.sort:
            descending = column.direction == SortDirection.desc
            first = self.nulls_first != descending
            placement = {"nulls_first": True} if first else {"nulls_last": True}
            field = F(column.column_name)
            ordering.append(field.desc(**placement) if descending else field.asc(**placement))
        queryset = queryset.order_by(*ordering)
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
            raise NotFoundError(pk) from None

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


# ---------------------------------------------------------------------------
# Cursor predicate
# ---------------------------------------------------------------------------


class RowValues(Func):
    """`(a, b, c)` - a SQL row constructor."""

    template = "(%(expressions)s)"
    arg_joiner = ", "


class _RowCompare(Func):
    template = "%(expressions)s"
    output_field = BooleanField()


class RowGreater(_RowCompare):
    arg_joiner = " > "


class RowLess(_RowCompare):
    arg_joiner = " < "


def _cursor_condition(viewset: "DjangoORMViewSet", fltr: CursorPredicate) -> Q:
    """
    "Everything past this row", as SQL.

    Two forms, because one is faster and the other is always correct:

    * A **row-value comparison** - `WHERE (year, id) > (2003, 42)` - is a single predicate that a
      composite index on exactly those columns can seek with, so page 500 costs what page 1 costs.
      It is only usable when every key sorts the same way (a row constructor cannot express
      `a ASC, b DESC`) and no key is nullable, because SQL evaluates any comparison involving NULL
      as unknown while this library defines NULL as the smallest value there is. The two rules
      disagree, and SQL's wins inside the database - so where they could differ, this form is not
      used.
    * A **union of segments** - key₁ past the anchor, OR key₁ equal and key₂ past, OR … - which
      spells the NULL handling out and copes with mixed directions. The planner usually degrades
      it to a scan, which is the price of being right.

    Verified against Django + SQLite; `Func(..., function='ROW')`, the recipe usually cited for
    this, is Postgres-only, and a row constructor fails outright if annotated rather than used
    directly as a condition.
    """
    keys = fltr.keys
    nullable = {field.name for field in viewset.model._meta.get_fields() if getattr(field, "null", False)}
    uniform = len({descending for _, descending in keys}) == 1
    if uniform and not fltr.inclusive and not any(name in nullable for name, _ in keys):
        return _row_value_condition(keys, fltr.position, fltr.backwards)
    return _segment_condition(
        keys,
        fltr.position,
        fltr.backwards,
        fltr.inclusive,
        fltr.nulls_first,
    )


def _row_value_condition(keys, position, backwards: bool) -> Q:
    left = RowValues(*(F(name) for name, _ in keys))
    right = RowValues(*(Value(position[name]) for name, _ in keys))
    comparison = RowGreater if wants_greater(keys[0][1], backwards) else RowLess
    return Q(comparison(left, right))


def _equals(name: str, value: Any) -> Q:
    """`IS NULL` rather than `= NULL`, which is never true."""
    return Q(**{f"{name}__isnull": True}) if value is None else Q(**{name: value})


def _segment_condition(keys, position, backwards: bool, inclusive: bool, nulls_first: bool = True) -> Q:
    segments: list[Q] = []

    for index, (name, descending) in enumerate(keys):
        segment = Q()
        for earlier, _ in keys[:index]:
            segment &= _equals(earlier, position[earlier])

        value = position[name]
        greater = wants_greater(descending, backwards)
        # Whether NULLs come before the values as this page is read. Two reversals on top of where
        # `nulls` puts them ascending: a descending key, and reading the page backwards.
        nulls_lead = (nulls_first != descending) != backwards

        if value is None:
            if not nulls_lead:
                # Nothing lies past a NULL when the NULLs are at the far end already.
                continue
            segment &= Q(**{f"{name}__isnull": False})
        else:
            beyond = Q(**{f"{name}__gt" if greater else f"{name}__lt": value})
            # SQL drops NULLs from every comparison - they are unknown, not true - so when the
            # ordering puts them past this anchor they have to be added back by hand.
            segment &= beyond if nulls_lead else beyond | Q(**{f"{name}__isnull": True})
        segments.append(segment)

    if inclusive:
        anchor = Q()
        for name, _ in keys:
            anchor &= _equals(name, position[name])
        segments.append(anchor)

    if not segments:
        return Q(pk__in=[])  # matches nothing, and says so without a special case downstream
    return reduce(operator.or_, segments)


@compiles(DjangoORMViewSet, CursorPredicate)
def _compile_cursor(viewset, fltr, queryset):
    return queryset.filter(_cursor_condition(viewset, fltr))
