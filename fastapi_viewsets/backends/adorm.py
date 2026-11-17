"""
A viewset backed by adorm, the asynchronous ORM interface: filter, sort and page run in the database through adorm
query sets, and every operation is awaited on an adorm session, without threads of the viewset's own.

The backend must precede the list mixin in the bases: `pk_field_name` is a property here and a plain class attribute
on `ListMixin`. Requires adorm with a driver (`pip install "dynamicforms-fastapi-viewsets[adorm]"`) and a configured
adorm connection profile.
"""

import re

from collections.abc import Iterator, Mapping
from contextvars import ContextVar
from typing import Any, ClassVar, Generic

import adorm

from adorm import F, Q
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, TypeAdapter, ValidationError
from typing_extensions import TypeVar

from ..collation import Collation
from ..context import Context
from ..cursor import CursorPredicate, segment_condition
from ..error_namespace import _namespaced
from ..error_responses import _validation_errors
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
from ..middleware import ActionExecution, Middleware
from ..mixins import filter_set_for, ImplMixin, LookupItem, SortDirection

K = TypeVar("K")
T = TypeVar("T", bound=BaseModel)


class _CodePointCollation:
    """Strings compare by code point."""

    __slots__ = ()

    def sort_key(self, text: str) -> str:
        return text


_CODE_POINT = _CodePointCollation()


class AdormViewSet(ImplMixin[K, T], Generic[K, T]):
    """
    Viewset over the adorm model `model`, answering records of `schema`, which is `model` unless the class sets
    another pydantic model. A generated adorm model serves as both: its fields are the columns (`club_id` for the
    relation `club`), and its relations are not part of the schema.
    """

    model: ClassVar[type[adorm.Model]]
    """The adorm model this viewset reads and writes."""

    schema: ClassVar[type[BaseModel]]
    """The pydantic model of the records; `model` when the class does not set it."""

    using: ClassVar[str | None] = None
    """The connection profile of the viewset's session; None for the current session."""

    session_name: ClassVar[str] = "default"
    """The session name of the viewset's session when `using` is set."""

    clean_on_save: ClassVar[bool] = False
    """Whether creates and updates run `full_clean()` (Django's `Model.full_clean()` on the target) before saving."""

    lookup_title: ClassVar[str | None] = None
    """The model field a lookup choice's `title` is read from; with it the default `LookupFilter` filters in SQL."""

    lookup_group: ClassVar[str | None] = None
    """The model field a lookup choice's `group` is read from; None for ungrouped choices."""

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if getattr(cls, "model", None) is not None and getattr(cls, "schema", None) is None:
            cls.schema = cls.model

    @property
    def pk_field_name(self) -> str:
        """
        The model's primary key field when `schema` has it, else the inherited default. The cursor filters on this
        name in SQL and reads it off the response record, so it must exist in both. Read-only: assign it on the class
        to override.
        """
        model = getattr(self, "model", None)
        if model is None:
            return super().pk_field_name
        pk = model._meta.pk
        declared = getattr(getattr(self, "schema", None), "model_fields", None)
        if declared is None or pk in declared:
            return pk
        return super().pk_field_name

    async def get_session(self, context: Context) -> adorm.Session:  # noqa: ARG002 - for overrides to choose per call
        """
        The session every operation of a call runs on: `create_session(using, session_name)`, or the current session
        when `using` is None. Override to choose it per call from the context's values, e.g. a profile per user.
        """
        if self.using is None:
            return adorm.current_session()
        return adorm.create_session(self.using, self.session_name)

    async def get_queryset(self, context: Context) -> adorm.QuerySet:
        """
        The base query set of model instances on the call's session; building it runs no query. Override to scope it
        (tenant, permission) from the context's values; every operation narrows it, and a retrieve, update or destroy
        of a row outside it raises `NotFoundError`.
        """
        return (await self.get_session(context)).bind(self.model).all()

    # -----------------------------------------------------------------------
    # Conversion
    # -----------------------------------------------------------------------

    def to_record(self, raw: Any) -> T:
        """
        A row (a model instance, or a dict of a `values()` query set) as `schema`. An instance of `schema` is
        returned as it is.
        """
        if isinstance(raw, self.schema):
            return raw
        if isinstance(raw, Mapping):
            return self.schema.model_validate(raw)
        return self.schema.model_validate(raw, from_attributes=True)

    def _model_field(self, name: str) -> str | None:
        """
        The field of `model` a client's field name addresses: the field itself, or the field whose alias it is (the
        generated `copy_` for `copy`). None when no field matches.
        """
        fields = self.model.model_fields
        if name in fields:
            return name
        return next((field for field, info in fields.items() if info.alias == name), None)

    def _api_name(self, field: str) -> str:
        """The name clients know model field `field` by: its alias in `schema` (or in `model`), else the field."""
        info = self.schema.model_fields.get(field) or self.model.model_fields.get(field)
        return info.alias if info is not None and info.alias else field

    # -----------------------------------------------------------------------
    # List pipeline
    # -----------------------------------------------------------------------

    async def perform_list(self, context: Context) -> ListRecords:
        """The query set, unevaluated, so later stages add filter, ordering and slice to its SQL."""
        return await self.get_queryset(context)

    async def apply_filter(self, context: Context, query: ListQuery, records: ListRecords) -> ListRecords:
        """
        Translates the filter set into `filter(...)`, or leaves all of it to the in-memory default. Pushed down only
        when every filter names a model field and has a compiler: a partial push-down would return too many rows.
        """
        queryset = _as_queryset(records)
        if queryset is not None and query.has_filter and query.needs("filter"):
            narrowed = self._compile_filters(query, queryset)
            if narrowed is not None:
                query.mark_applied("filter")
                records = narrowed
        return await super().apply_filter(context, query, records)

    def _compile_filters(self, query: ListQuery, queryset: adorm.QuerySet) -> adorm.QuerySet | None:
        """`queryset` narrowed by the query's declared filters; None when they cannot all run in SQL."""
        filter_set = filter_set_for(query)
        if not filter_set and filters_from(query.fltr) is None:
            return None  # a hand-written filter model: nothing declarative to translate
        if not all(self._model_field(name) is not None for fltr in filter_set for name in fltr.fields()):
            return None
        if not can_compile_all(type(self), filter_set):
            return None
        return compile_all(type(self), self, filter_set, queryset)

    async def collation_for(self, context: Context | None, column_name: str) -> Collation:  # noqa: ARG002
        """
        Code point order for the in-memory sort and cursor, which run when a filter or sort column has no SQL
        translation; `collation` does not apply.
        """
        return _CODE_POINT

    async def apply_sort(self, context: Context, query: ListQuery, records: ListRecords) -> ListRecords:
        """Translates the sort into `order_by(...)` with explicit NULL placement, matching the in-memory sort."""
        queryset = _as_queryset(records)
        if queryset is None or not query.has_sort or not query.needs("sort"):
            return await super().apply_sort(context, query, records)

        names = [self._model_field(column.column_name) for column in query.sort]
        if None in names:
            return await super().apply_sort(context, query, records)

        # `nulls` is the ascending placement, so a descending key flips it.
        ordering = []
        for column, name in zip(query.sort, names, strict=True):
            descending = column.direction == SortDirection.desc
            first = self.nulls_first != descending
            placement = {"nulls_first": True} if first else {"nulls_last": True}
            field = F(name)
            ordering.append(field.desc(**placement) if descending else field.asc(**placement))
        query.mark_applied("sort")
        return await super().apply_sort(context, query, queryset.order_by(*ordering))

    async def take_page(self, query: ListQuery, records: ListRecords) -> tuple[list, bool]:
        """Slices the query set into LIMIT/OFFSET, reading one extra row to answer `has_more`."""
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
        return await queryset.count()

    # -----------------------------------------------------------------------
    # Lookup (with LookupMixin)
    # -----------------------------------------------------------------------

    async def perform_lookup(self, context: Context) -> ListRecords:
        """`get_queryset`; with `lookup_title`, ordered by `lookup_group`, `lookup_title` and the primary key."""
        queryset = await self.get_queryset(context)
        ordering = [name for name in (self.lookup_group, self.lookup_title, self.model._meta.pk) if name]
        return queryset.order_by(*ordering) if self.lookup_title else queryset

    async def apply_lookup_filter(self, context: Context, query: ListQuery, records: ListRecords) -> ListRecords:
        """
        Filters in SQL: a `make_filter_model` declaration as `apply_filter` does, and the default `LookupFilter`
        (`q` against `lookup_title`, case-insensitive; `ids` against the primary key) when `lookup_title` is set.
        Anything else is left to the in-memory default.
        """
        queryset = _as_queryset(records)
        if queryset is None or not query.has_filter or not query.needs("filter"):
            return await super().apply_lookup_filter(context, query, records)

        narrowed = self._compile_filters(query, queryset)
        if narrowed is None and self.lookup_title and _is_default_lookup_filter(query.fltr):
            narrowed = queryset
            if query.fltr.q is not None:
                narrowed = narrowed.filter(**{f"{self.lookup_title}__icontains": query.fltr.q})
            if query.fltr.ids is not None:
                keys = self._primary_keys(query.fltr.ids)
                narrowed = narrowed.filter(pk__in=keys) if keys else narrowed.none()
        if narrowed is None:
            return await super().apply_lookup_filter(context, query, records)
        query.mark_applied("filter")
        return narrowed

    def to_lookup_item(self, raw: Any) -> Any:
        """A `LookupItem` of the row's `lookup_title` and `lookup_group`; the row as it is without `lookup_title`."""
        if self.lookup_title is None:
            return raw
        group = getattr(raw, self.lookup_group) if self.lookup_group else None
        return LookupItem(group=group, pk=raw.pk, title=str(getattr(raw, self.lookup_title)))

    def _primary_keys(self, values: list[Any]) -> list[Any]:
        """`values` as values of the model's primary key; a value that does not convert is left out."""
        adapter = TypeAdapter(self.model.model_fields[self.model._meta.pk].annotation)
        keys = []
        for value in values:
            try:
                key = adapter.validate_python(value)
            except ValidationError:
                continue
            if key is not None:
                keys.append(key)
        return keys

    # -----------------------------------------------------------------------
    # Single-record operations
    # -----------------------------------------------------------------------

    async def perform_retrieve(self, context: Context, pk: K) -> T:
        return self.to_record(await self._get_or_404(context, pk))

    async def perform_create(self, context: Context, data: T) -> T:
        """
        Inserts `data` and answers the row as stored. A record that fails `model`, or whose save violates a constraint
        or fails `full_clean()`, raises `RequestValidationError` with `loc` `("body", *field)`.
        """
        (record,) = await self._create(context, [data], bulk=False)
        return record

    async def perform_bulk_create(self, context: Context, data: list[T]) -> list[T]:
        """
        Inserts every record in one transaction; one invalid record inserts none. The errors of all records raise one
        `RequestValidationError` with `loc` `("body", index, *field)`.
        """
        return await self._create(context, data, bulk=True)

    async def _create(self, context: Context, data: list[T], bulk: bool) -> list[T]:
        session = await self.get_session(context)
        errors: list[dict] = []
        saved: dict[tuple, adorm.Model] = {}
        with session.activate():
            try:
                async with session.atomic():
                    for index, record in enumerate(data):
                        prefix = ("body", index) if bulk else ("body",)
                        try:
                            instance = self.model.model_validate(self._writable_fields(record, creating=True))
                        except ValidationError as error:
                            errors.extend(_prefixed(entry, prefix) for entry in error.errors(include_url=False))
                            continue
                        if await self._save(session, instance, prefix, errors, force_insert=True):
                            saved[prefix] = instance
                    if errors:
                        raise RequestValidationError(errors)
                    # Read back: the database fills in what the request left to its defaults.
                    stored = await session.bind(self.model).in_bulk([instance.pk for instance in saved.values()])
                _record_written(saved)
            except adorm.IntegrityError as error:
                raise RequestValidationError(self._deferred_errors(error, saved, bulk)) from error
        return [self.to_record(stored[instance.pk]) for instance in saved.values()]

    async def perform_update(self, context: Context, pk: K, data: T, partial: bool = True) -> T:
        """
        Writes `data` onto row `pk` under a row lock; see `_update_locked`. A merged record that fails `schema`, or
        whose save violates a constraint or fails `full_clean()`, raises `RequestValidationError` with `loc`
        `("body", *field)`.
        """
        (record,) = await self._update_locked(context, {pk: data}, partial, bulk=False)
        return record

    async def perform_bulk_update(self, context: Context, records: dict[K, T], partial: bool = True) -> list[T]:
        """
        Writes every record in one transaction under row locks; one missing or invalid row writes none. All `schema`
        failures raise one `RequestValidationError` with `loc` `("body", pk, *field)` before any row is saved; if every
        row passes, the save errors of all rows are raised the same way.
        """
        return await self._update_locked(context, records, partial, bulk=True)

    async def _update_locked(self, context: Context, records: dict[K, T], partial: bool, bulk: bool) -> list[T]:
        """
        In one transaction: checks that every row is in `get_queryset` (`NotFoundError`), locks each row
        (`SELECT ... FOR UPDATE`), sets the record's fields and validates the result as `schema`, then saves each row
        in a savepoint, so the other rows are still checked after one row's save fails. The errors of all rows are
        raised as one `RequestValidationError`, which rolls back every save. The answer is the rows as stored, with
        what the model's `save()` and the database set.

        Rows lock in key order, so overlapping bulk updates do not deadlock, and only rows `get_queryset` lets the
        call see are locked. The lock is taken on the model's own query set, as `FOR UPDATE` refuses the
        `distinct()` or aggregation `get_queryset` may add; visibility is checked again under the lock.
        """
        session = await self.get_session(context)
        locked = session.bind(self.model).select_for_update()
        errors: list[dict] = []
        rows: dict[Any, adorm.Model] = {}
        keys = sorted(records)
        with session.activate():
            try:
                async with session.atomic():
                    await self._check_visible(context, keys)
                    for pk in keys:
                        try:
                            instance = await locked.get(pk=pk)
                        except adorm.ObjectDoesNotExist:
                            raise NotFoundError(pk) from None
                        for name, value in self._writable_fields(records[pk], exclude_unset=partial).items():
                            setattr(instance, name, value)
                        try:
                            self._validate_merged(instance)
                        except ValidationError as error:
                            prefix = ("body", pk) if bulk else ("body",)
                            errors.extend(_prefixed(entry, prefix) for entry in error.errors(include_url=False))
                        rows[pk] = instance
                    await self._check_visible(context, keys)
                    if errors:
                        raise RequestValidationError(errors)
                    for pk, instance in rows.items():
                        await self._save(session, instance, ("body", pk) if bulk else ("body",), errors)
                    if errors:
                        raise RequestValidationError(errors)
                    stored = await session.bind(self.model).in_bulk(keys)
                _record_written({("body", pk) if bulk else ("body",): instance for pk, instance in rows.items()})
            except adorm.IntegrityError as error:
                written = {("body", pk) if bulk else ("body",): instance for pk, instance in rows.items()}
                raise RequestValidationError(self._deferred_errors(error, written, bulk)) from error
        return [self.to_record(stored[pk]) for pk in records]

    async def _check_visible(self, context: Context, keys: list[Any]) -> None:
        """Raises `NotFoundError` for the first of `keys` that is not a primary key of `get_queryset`."""
        scope = (await self.get_queryset(context)).filter(pk__in=keys)
        visible = {key async for key in scope.values_list("pk", flat=True)}
        for pk in keys:
            if pk not in visible:
                raise NotFoundError(pk)

    async def _save(
        self, session: adorm.Session, instance: adorm.Model, prefix: tuple, errors: list, **options
    ) -> bool:
        """
        Saves `instance` in a savepoint, after `full_clean()` when `clean_on_save`. A failed validation or a violated
        constraint adds its errors under `prefix` to `errors` and returns False.
        """
        try:
            async with session.atomic():
                if self.clean_on_save:
                    await instance.full_clean()
                await instance.save(**options)
        except adorm.ValidationError as error:
            errors.extend(self._renamed(_validation_errors(error, prefix), len(prefix)))
            return False
        except adorm.IntegrityError as error:
            errors.extend(self._integrity_errors(error, prefix))
            return False
        return True

    def _validate_merged(self, instance: adorm.Model) -> None:
        """Validates the fields of `instance` as `schema`; raises pydantic's `ValidationError`."""
        values = {
            name: getattr(instance, name) for name in self.model.model_fields if name in instance.model_fields_set
        }
        if isinstance(self.schema, type) and issubclass(self.schema, adorm.Model):
            self.schema.model_validate(values)
        else:
            self.schema.model_validate(_FieldView(instance, values), from_attributes=True)

    async def perform_destroy(self, context: Context, pk: K) -> dict[K, None]:
        """
        Deletes row `pk` with the model's delete (cascades included). A reference that forbids it (`PROTECT`,
        `RESTRICT`) or another violated constraint raises `RequestValidationError` with an error of no field.
        """
        session = await self.get_session(context)
        instance = await self._get_or_404(context, pk)
        try:
            with session.activate():
                await instance.delete()
        except adorm.IntegrityError as error:
            raise RequestValidationError(self._integrity_errors(error, ("body",))) from error
        return {pk: None}

    # -----------------------------------------------------------------------

    async def _get_or_404(self, context: Context, pk: K) -> adorm.Model:
        try:
            return await (await self.get_queryset(context)).get(pk=pk)
        except adorm.ObjectDoesNotExist:
            raise NotFoundError(pk) from None

    def _writable_fields(self, data: T, exclude_unset: bool = False, creating: bool = False) -> dict[str, Any]:
        """
        The model fields of `data` by field name. An update omits the primary key (saving under another key writes
        that row); a create omits a primary key of None, which the database assigns.
        """
        fields = self.model.model_fields
        pk = self.model._meta.pk
        if hasattr(data, "model_dump"):
            dumped = data.model_dump(exclude_unset=exclude_unset, by_alias=False)
        else:
            dumped = dict(data)
        writable = {name: value for name, value in dumped.items() if name in fields}
        if not creating or writable.get(pk) is None:
            writable.pop(pk, None)
        return writable

    def _renamed(self, entries: list[dict], depth: int) -> list[dict]:
        """
        `entries` with a relation's name at `loc[depth]` replaced by its column (`club` by `club_id`), when `schema`
        has the column and not the relation: `full_clean()` reports a foreign key's errors under the relation.
        """
        declared = self.schema.model_fields
        columns = {
            name: relation.column
            for name, relation in getattr(self.model, "__adorm_relations__", {}).items()
            if getattr(relation, "column", None) in declared and name not in declared
        }
        renamed = []
        for entry in entries:
            loc = tuple(entry["loc"])
            if len(loc) > depth and loc[depth] in columns:
                entry = {**entry, "loc": (*loc[:depth], columns[loc[depth]], *loc[depth + 1 :])}
            renamed.append(entry)
        return renamed

    def _field_of_column(self, column: str) -> str | None:
        """The model field stored in the database column `column`; None for a column of no field."""
        orm_names = self.model._meta.orm_names
        for name in self.model.model_fields:
            if orm_names.get(name, name) == column:
                return name
        return None

    def _deferred_errors(self, error: adorm.IntegrityError, rows: dict[tuple, adorm.Model], bulk: bool) -> list[dict]:
        """
        A constraint checked at commit (PostgreSQL defers the foreign keys Django creates) as `_integrity_errors` of
        each row in `rows` (prefix -> instance) whose value matches the reported key. Without a match a bulk action
        gets the error on the form.
        """
        detail, _ = _violation_report(error)
        match = _KEY_VALUES.search(detail or "")
        field = self._field_of_column(match[1].strip('"')) if match and "," not in match[1] else None
        matching = [
            prefix for prefix, instance in rows.items() if field and str(getattr(instance, field, None)) == match[2]
        ]
        if matching:
            return [entry for prefix in matching for entry in self._integrity_errors(error, prefix)]
        entries = self._integrity_errors(error, ("body",))
        return [{**entry, "loc": ("body",)} for entry in entries] if bulk else entries

    def _integrity_errors(self, error: adorm.IntegrityError, prefix: tuple) -> list[dict]:
        """
        A violated constraint as pydantic-style errors under `prefix`: a unique violation as `unique` with the fields
        in `fields`, under the field when it is one; a NOT NULL violation as `required` and a missing related row as
        `invalid`, under the field; a reference forbidding a delete as `protected` or `restricted`; anything else as
        `invalid` of no field. The fields come from the database's report (PostgreSQL's `Key (...)` detail and
        column name), read from the driver's exception, and are named as clients know them (`_api_name`).
        """
        if isinstance(error, (adorm.ProtectedError, adorm.RestrictedError)):
            code = _namespaced("protected") if isinstance(error, adorm.ProtectedError) else _namespaced("restricted")
            return [_error(code, prefix, "Other records refer to this record.")]

        detail, column = _violation_report(error)
        columns = list(_KEY_COLUMNS.findall(detail or "")[:1])
        columns = [part.strip().strip('"') for part in columns[0].split(",")] if columns else []
        if column and not columns:
            columns = [column]
        fields = [self._field_of_column(name) for name in columns]
        names = [self._api_name(field) if field else name for field, name in zip(fields, columns, strict=True)]
        field_loc = (*prefix, names[0]) if len(fields) == 1 and fields[0] is not None else prefix

        if error.sqlstate == _UNIQUE_VIOLATION:
            many = len(names) > 1
            message = f"A record with {'these values' if many else 'this value'} already exists."
            return [_error(_namespaced("unique"), field_loc, message, fields=names)]
        if error.sqlstate == _NOT_NULL_VIOLATION:
            return [_error(_namespaced("required"), field_loc, "This field cannot be null.")]
        if error.sqlstate == _FOREIGN_KEY_VIOLATION:
            return [_error(_namespaced("invalid"), field_loc, "The related record does not exist.")]
        return [_error(_namespaced("invalid"), prefix, "The record violates a database constraint.")]


def _error(code: str, loc: tuple, message: str, **params: Any) -> dict:
    """A pydantic-style error of the field error code `code` with `params`."""
    return {"type": code, "loc": loc, "msg": message, "ctx": params}


_UNIQUE_VIOLATION = "23505"
_NOT_NULL_VIOLATION = "23502"
_FOREIGN_KEY_VIOLATION = "23503"

_KEY_COLUMNS = re.compile(r"Key \(([^)]*)\)=")
"""The column list of PostgreSQL's detail of a unique or foreign key violation: `Key (club_id, name)=(...)`."""

_KEY_VALUES = re.compile(r"Key \(([^)]*)\)=\((.*)\) is not present")
"""Columns and values of PostgreSQL's detail of a foreign key violation: `Key (club_id)=(9) is not present ...`."""


def _causes(error: BaseException | None) -> Iterator[BaseException]:
    """`error` and the exceptions it was raised from or during, each once."""
    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        yield error
        error = error.__cause__ or error.__context__


def _violation_report(error: adorm.IntegrityError) -> tuple[str | None, str | None]:
    """
    The detail and column name PostgreSQL reported for the violation: psycopg's `diag` (the Django driver) or
    asyncpg's attributes (the native driver). (None, None) when the driver's exception has neither.
    """
    for cause in _causes(getattr(error, "original", None)):
        diag = getattr(cause, "diag", None)
        source = diag if diag is not None else cause
        detail = getattr(source, "message_detail", None) if diag is not None else getattr(source, "detail", None)
        column = getattr(source, "column_name", None)
        if detail or column:
            return detail, column
    return None, None


def _prefixed(entry: dict, prefix: tuple) -> dict:
    """A pydantic error under `prefix`; its input is kept only when null."""
    error = {key: value for key, value in entry.items() if key != "input"}
    if entry.get("input", ...) is None:
        error["input"] = None
    return {**error, "loc": (*prefix, *entry["loc"])}


def _is_default_lookup_filter(fltr: Any) -> bool:
    from ..mixins import LookupFilter

    return type(fltr) is LookupFilter


class _FieldView:
    """The fields of a model instance as attributes, for `from_attributes` validation of another schema."""

    __slots__ = ("_instance", "_values")

    def __init__(self, instance: adorm.Model, values: dict[str, Any]):
        self._instance = instance
        self._values = values

    def __getattr__(self, name: str) -> Any:
        if name in self._values:
            return self._values[name]
        return getattr(self._instance, name)


def _as_queryset(records: ListRecords) -> adorm.QuerySet | None:
    """`records` if it is still an unevaluated query set, else None, which makes every push-down decline."""
    return records if isinstance(records, adorm.QuerySet) else None


_WRITTEN: ContextVar[dict[tuple, adorm.Model] | None] = ContextVar("adorm_viewset_written", default=None)
"""The rows the viewset's writes saved in the current `AdormSession(atomic=True)` block, by error prefix."""


def _record_written(rows: dict[tuple, adorm.Model]) -> None:
    """Adds `rows` to the rows of the enclosing `AdormSession` block, whose commit may still fail on them."""
    written = _WRITTEN.get()
    if written is not None:
        written.update(rows)


class AdormSession(Middleware):
    """
    Runs each action of an `AdormViewSet` with the viewset's session (`get_session(context)`) current, so adorm code
    in the action, in overridden methods and in tasks they start uses it. With `atomic` the action runs in one
    `atomic()` block of that session: an exception rolls back everything the action wrote. The value of
    `@action_configuration({AdormSession: True | False})` replaces `atomic` for an action. Other viewsets' actions
    run unchanged.

    adorm's `ObjectDoesNotExist` raised in the action answers `NotFoundError` (with the action's `pk`), and
    `IntegrityError` a `RequestValidationError` as the viewset's own writes report it. A constraint the database
    checks at the commit (a foreign key) is reported on the row the viewset wrote with the offending value.
    """

    def __init__(self, atomic: bool = False):
        self.atomic = atomic

    async def __call__(self, request, viewset, context, call_next):  # noqa: ARG002 - the chain's signature
        return await call_next()

    async def execute(self, execution: ActionExecution, call_next) -> Any:
        viewset = execution.viewset
        if not isinstance(viewset, AdormViewSet):
            return await call_next()
        configured = self.config_from(execution.context)
        atomic = self.atomic if configured is None else bool(configured)
        session = await viewset.get_session(execution.context)
        written: dict[tuple, adorm.Model] = {}
        token = _WRITTEN.set(written if atomic else None)
        try:
            with session.activate():
                if not atomic:
                    return await call_next()
                async with session.atomic():
                    return await call_next()
        except adorm.ObjectDoesNotExist:
            raise NotFoundError(execution.kwargs.get("pk")) from None
        except adorm.IntegrityError as error:
            bulk = any(len(prefix) > 1 for prefix in written)
            raise RequestValidationError(viewset._deferred_errors(error, written, bulk)) from error
        finally:
            _WRITTEN.reset(token)


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
"""Filter type to adorm lookup. `Overlaps` has no adorm lookup and runs in memory."""


def _compile_lookup(viewset, fltr, queryset):
    value = bool(fltr.value) if isinstance(fltr, IsNull) else fltr.value
    return queryset.filter(**{f"{viewset._model_field(fltr.field)}__{_LOOKUPS[type(fltr)]}": value})


for _filter_type in _LOOKUPS:
    compiles(AdormViewSet, _filter_type)(_compile_lookup)


@compiles(AdormViewSet, CursorPredicate)
def _compile_cursor(viewset, fltr, queryset):
    """
    "Everything past this row" as a union of per-key conditions, which spell out NULL placement and mixed
    directions.
    """
    fields = {name: viewset._model_field(name) for name, _ in fltr.keys}
    keys = tuple((fields[name], descending) for name, descending in fltr.keys)
    position = {fields.get(name, name): value for name, value in fltr.position.items()}
    condition = segment_condition(keys, position, fltr.backwards, fltr.inclusive, fltr.nulls_first, Q)
    return queryset.none() if condition is None else queryset.filter(condition)
