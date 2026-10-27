"""
A viewset backed by the Django ORM, pushing filter, sort and page down into SQL.

The backend must precede the list mixin in the bases: `pk_field_name` is a property here and a plain
class attribute on `ListMixin`. Requires the `django` extra; every ORM call uses the async API or
`sync_to_async`.
"""

import operator

from functools import reduce
from typing import Any, ClassVar, Generic, TYPE_CHECKING

from asgiref.sync import sync_to_async
from django.db import router, transaction
from django.db.models import BooleanField, F, Func, Q, Value
from django.db.models.fields import AutoFieldMixin
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError
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
    """
    Viewset over `model`, converting rows into `schema`.

    A relation field appears in `schema` under its name and carries the related row's key (`owner: int`);
    declaring the column name (`owner_id`) raises `TypeError` at class definition.
    """

    model: ClassVar[type["Model"]]
    """The Django model this viewset reads and writes."""

    schema: ClassVar[type[BaseModel]]
    """The pydantic model rows are converted into on the way out."""

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        model = getattr(cls, "model", None)
        declared = getattr(getattr(cls, "schema", None), "model_fields", None)
        if model is None or declared is None:
            return
        columns = [field for field in _model_columns(model) if field.attname != field.name]
        for field in columns:
            if field.attname in declared:
                raise TypeError(
                    f"{cls.__name__}: {cls.schema.__name__} declares `{field.attname}`, the column of "
                    f"{model.__name__}.{field.name}. Declare the relation as `{field.name}`; it carries "
                    f"the related row's key."
                )

    pk_field: ClassVar[str] = "pk"
    """The lookup `_get_or_404` hands to `aget()`; `pk` is Django's alias for the primary key."""

    @property
    def pk_field_name(self) -> str:
        """
        `Model._meta.pk.name` when `schema` carries it, else the inherited default.

        The cursor filters on this name in SQL and reads it off the response record, so it must exist in both.
        A composite primary key raises `TypeError`. Read-only: assign it on the class to override.
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
        if declared is None or pk.name in declared:
            return pk.name
        return super().pk_field_name

    def get_queryset(self, context: Context) -> "QuerySet":  # noqa: ARG002 - for overrides to narrow per user
        """The base queryset. Override to scope it (tenant, permission); every operation narrows it."""
        return self.model.objects.all()

    # -----------------------------------------------------------------------
    # Conversion
    # -----------------------------------------------------------------------

    def to_record(self, raw: Any) -> T:
        """
        Django row to `schema`; a relation field is read from its column, never fetching the related row.

        A declined filter or sort converts every row reaching that stage, all held in memory at once.
        """
        if isinstance(raw, self.schema):
            return raw
        if isinstance(raw, self.model):
            raw = _ColumnView(raw, self._columns())
        return self.schema.model_validate(raw, from_attributes=True)

    def _columns(self) -> dict[str, str]:
        """Relation field name to the name of its column: `{"owner": "owner_id"}`."""
        return {field.name: field.attname for field in _model_columns(self.model) if field.attname != field.name}

    def _column(self, name: str) -> str:
        """The column a query uses for the schema field `name`."""
        return self._columns().get(name, name)

    # -----------------------------------------------------------------------
    # List pipeline
    # -----------------------------------------------------------------------

    async def perform_list(self, context: Context) -> ListRecords:
        """The queryset, unevaluated, so later stages can add filter, ordering and slice to the SQL."""
        return self.get_queryset(context)

    async def apply_filter(self, context: Context, query: ListQuery, records: ListRecords) -> ListRecords:
        """
        Translates the filter set into `.filter(...)`, or leaves all of it to the in-memory default.

        Pushed down only when every filter names a concrete field and has a compiler: a partial push-down
        would return too many rows.
        """
        queryset = _as_queryset(records)
        if queryset is None or not query.has_filter or not query.needs("filter"):
            return await super().apply_filter(context, query, records)

        filter_set = filter_set_for(query)
        if not filter_set and filters_from(query.fltr) is None:
            # A hand-written filter model: nothing declarative to translate.
            return await super().apply_filter(context, query, records)

        concrete = self._concrete_fields()
        translatable = all(all(name in concrete for name in fltr.fields()) for fltr in filter_set)
        if not translatable or not can_compile_all(type(self), filter_set):
            return await super().apply_filter(context, query, records)

        queryset = compile_all(type(self), self, filter_set, queryset)
        query.mark_applied("filter")
        return await super().apply_filter(context, query, queryset)

    def _concrete_fields(self) -> set[str]:
        """The schema field names that map to a column: every concrete field's `name`."""
        return {field.name for field in _model_columns(self.model)}

    async def apply_sort(self, context: Context, query: ListQuery, records: ListRecords) -> ListRecords:
        """Translates the sort into `.order_by(...)` with explicit NULL placement, matching the in-memory sort."""
        queryset = _as_queryset(records)
        if queryset is None or not query.has_sort or not query.needs("sort"):
            return await super().apply_sort(context, query, records)

        concrete = self._concrete_fields()
        if not all(column.column_name in concrete for column in query.sort):
            return await super().apply_sort(context, query, records)

        # `nulls` is the ascending placement, so a descending key flips it. Django accepts only one
        # flag, and only True.
        ordering = []
        for column in query.sort:
            descending = column.direction == SortDirection.desc
            first = self.nulls_first != descending
            placement = {"nulls_first": True} if first else {"nulls_last": True}
            field = F(self._column(column.column_name))
            ordering.append(field.desc(**placement) if descending else field.asc(**placement))
        queryset = queryset.order_by(*ordering)
        query.mark_applied("sort")
        return await super().apply_sort(context, query, queryset)

    async def take_page(self, query: ListQuery, records: ListRecords) -> tuple[list, bool]:
        """Slices the queryset into LIMIT/OFFSET, fetching one extra row to answer `has_more`."""
        queryset = _as_queryset(records)
        if queryset is None:
            return await super().take_page(query, records)

        rows = [row async for row in queryset[query.offset : query.offset + query.limit + 1]]
        query.mark_applied("pagination")
        return rows[: query.limit], len(rows) > query.limit

    async def count_records(self, context: Context, query: ListQuery, records: ListRecords) -> int | None:
        """One `SELECT COUNT(*)`."""
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
        instance = await self.model.objects.acreate(**self._writable_fields(data, creating=True))
        return self.to_record(instance)

    async def perform_bulk_create(self, context: Context, data: list[T]) -> list[T]:  # noqa: ARG002
        instances = [self.model(**self._writable_fields(record, creating=True)) for record in data]
        created = await sync_to_async(self.model.objects.bulk_create)(instances)
        return [self.to_record(instance) for instance in created]

    async def perform_update(self, context: Context, pk: K, data: T, partial: bool = True) -> T:
        """
        Writes `data` onto row `pk` under a row lock; see `_update_locked`. A merged record that fails
        `schema` raises `RequestValidationError` with `loc` `("body", *field)`.
        """
        (record,) = await sync_to_async(self._update_locked)(context, {pk: data}, partial, bulk=False)
        return record

    async def perform_bulk_update(self, context: Context, records: dict[K, T], partial: bool = True) -> list[T]:
        """
        Writes every record in one transaction under row locks; one missing or invalid row writes none.
        All failures raise one `RequestValidationError` with `loc` `("body", pk, *field)`.
        """
        return await sync_to_async(self._update_locked)(context, records, partial, bulk=True)

    def _update_locked(self, context: Context, records: dict[K, T], partial: bool, bulk: bool) -> list[T]:
        """
        In one transaction: locks each row (`SELECT ... FOR UPDATE`), sets the record's fields, validates
        as `schema` and saves.

        Rows lock in key order so overlapping bulk updates do not deadlock. The lock goes through
        `_base_manager` because `FOR UPDATE` refuses the `distinct()`, aggregation or nullable
        `select_related` that `get_queryset` may add; `get_queryset` still decides visibility (`NotFoundError`).
        """
        locked = self.model._base_manager.db_manager(router.db_for_write(self.model)).select_for_update()
        errors: list[dict] = []
        rows = {}
        with transaction.atomic(using=locked.db):
            for pk in sorted(records):
                try:
                    instance = locked.get(**{self.pk_field: pk})
                except self.model.DoesNotExist:
                    raise NotFoundError(pk) from None
                if not self.get_queryset(context).filter(pk=instance.pk).exists():
                    raise NotFoundError(pk)
                for name, value in self._writable_fields(records[pk], exclude_unset=partial).items():
                    setattr(instance, name, value)
                try:
                    self.to_record(instance)
                except ValidationError as error:
                    prefix = ("body", pk) if bulk else ("body",)
                    errors.extend(
                        {**entry, "loc": (*prefix, *entry["loc"])}
                        for entry in error.errors(include_url=False, include_input=False)
                    )
                rows[pk] = instance
            if errors:
                raise RequestValidationError(errors)
            for instance in rows.values():
                instance.save()
        return [self.to_record(rows[pk]) for pk in records]

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

    def _writable_fields(self, data: T, exclude_unset: bool = False, creating: bool = False) -> dict[str, Any]:
        """
        The concrete model fields of `data`, keyed by column (`owner_id` for `owner`).

        An update omits the primary key (saving under another key writes that row). A create omits only a
        key the database or Django assigns (auto field, multi-table-inheritance parent link).
        """
        editable = {
            field.name: field.attname
            for field in _model_columns(self.model)
            if not field.primary_key or (creating and not _assigned_key(field))
        }
        dumped = data.model_dump(exclude_unset=exclude_unset) if hasattr(data, "model_dump") else dict(data)
        return {editable[name]: value for name, value in dumped.items() if name in editable}


def _model_columns(model: type["Model"]) -> list:
    """The model's fields that are columns, the composite primary key excluded."""
    return [field for field in model._meta.concrete_fields if not isinstance(field, _COMPOSITE_PK)]


def _assigned_key(field) -> bool:
    """Whether a primary key `field` gets its value from the database or from Django on save."""
    remote = getattr(field, "remote_field", None)
    return isinstance(field, AutoFieldMixin) or bool(remote and getattr(remote, "parent_link", False))


class _ColumnView:
    """A model instance whose relation field names answer with their column's value."""

    __slots__ = ("_columns", "_row")

    def __init__(self, row: "Model", columns: dict[str, str]):
        self._row = row
        self._columns = columns

    def __getattr__(self, name: str) -> Any:
        return getattr(self._row, self._columns.get(name, name))


def _as_queryset(records: ListRecords) -> "QuerySet | None":
    """`records` if it is still an unevaluated QuerySet, else None, which makes every push-down decline."""
    try:
        from django.db.models import QuerySet
    except ImportError:  # pragma: no cover - the django extra is not installed
        return None
    return records if isinstance(records, QuerySet) else None


# ---------------------------------------------------------------------------
# Filter compilers (registered per backend; see fastapi_viewsets/filters/registry.py)
# ---------------------------------------------------------------------------

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
Filter type to Django lookup. `Overlaps` is absent: its Django equivalent is Postgres-only, so it runs in memory.
"""


def _compile_lookup(viewset, fltr, queryset):
    return queryset.filter(**{f"{viewset._column(fltr.field)}__{_LOOKUPS[type(fltr)]}": fltr.value})


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

    A row-value comparison (`(year, id) > (2003, 42)`, index-seekable) when all keys share a direction and
    none is nullable, since SQL compares NULL as unknown; otherwise a union of per-key segments that spells
    out NULL placement and mixed directions.
    """
    keys = [(viewset._column(name), descending) for name, descending in fltr.keys]
    position = {viewset._column(name): value for name, value in fltr.position.items()}
    nullable = {field.attname for field in _model_columns(viewset.model) if field.null}
    uniform = len({descending for _, descending in keys}) == 1
    if uniform and not fltr.inclusive and not any(name in nullable for name, _ in keys):
        return _row_value_condition(keys, position, fltr.backwards)
    return _segment_condition(
        keys,
        position,
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
        # Whether NULLs come first as this page is read: `nulls`, flipped by descending and by backwards.
        nulls_lead = (nulls_first != descending) != backwards

        if value is None:
            if not nulls_lead:
                # Nothing lies past a NULL when the NULLs are at the far end already.
                continue
            segment &= Q(**{f"{name}__isnull": False})
        else:
            beyond = Q(**{f"{name}__gt" if greater else f"{name}__lt": value})
            # SQL comparisons never match NULL, so NULLs ordered past the anchor are added explicitly.
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
