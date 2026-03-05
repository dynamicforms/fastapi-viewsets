from abc import ABC, abstractmethod
from enum import Enum
from typing import Annotated, Any, final, Generic, get_args, get_origin, Union

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, create_model
from pydantic.alias_generators import to_camel
from typing_extensions import TypeVar

from fastapi_viewsets.response_classes import NOT_FOUND_RESPONSE

T = TypeVar("T")
K = TypeVar("K")
TFilter = TypeVar("TFilter", default=None)


class ImplMixin(Generic[K, T], ABC):
    @abstractmethod
    async def perform_create(self, data: T) -> T:
        """
        Subclasses should implement this method to perform the actual creation.
        """
        raise NotImplementedError("Method 'perform_create' must be implemented.")

    @abstractmethod
    async def perform_bulk_create(self, data: list[T]) -> list[T]:
        """
        Subclasses should implement this method to perform the actual bulk creation.
        """
        raise NotImplementedError("Method 'perform_bulk_create' must be implemented.")

    @abstractmethod
    async def perform_list(self) -> list[T]:
        """
        Subclasses should implement this method to perform the actual listing.
        """
        raise NotImplementedError("Method 'perform_list' must be implemented.")

    @abstractmethod
    async def perform_retrieve(self, pk: K) -> T:
        """
        Subclasses should implement this method to perform the actual retrieval.
        """
        raise NotImplementedError("Method 'perform_retrieve' must be implemented.")

    @abstractmethod
    async def perform_update(self, pk: K, data: T, partial: bool = True) -> T:
        """
        Subclasses should implement this method to perform the actual update.
        """
        raise NotImplementedError("Method 'perform_update' must be implemented.")

    @abstractmethod
    async def perform_bulk_update(self, records: dict[K, T], partial: bool = True) -> list[T]:
        """
        Subclasses should implement this method to perform the actual bulk update.
        """
        raise NotImplementedError("Method 'perform_bulk_update' must be implemented.")

    @abstractmethod
    async def perform_destroy(self, pk: K) -> dict[K, Any]:
        """
        Subclasses should implement this method to perform the actual destruction.
        """
        raise NotImplementedError("Method 'perform_destroy' must be implemented.")

    @abstractmethod
    async def perform_bulk_destroy(self, pk: list[K]) -> list[dict[K, Any]]:
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
    async def create(self: "ImplMixin[K, T] | CreateMixin[K ,T]", data: T) -> T:
        return await self.perform_create(data)


class BulkOnlyCreateMixin(Generic[K, T], ABC):
    """
    Create model instances in bulk.
    """
    __router = APIRouter()

    @final
    @__router.post("bulk")
    async def bulk_create(self: "ImplMixin[K, T] | BulkOnlyCreateMixin[K ,T]", data: list[T]) -> list[T]:
        return await self.perform_bulk_create(data)


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
    List a queryset.
    """
    __router = APIRouter()

    @final
    @__router.get("")
    async def list_items(
        self: "ImplMixin[Any, T] | ListMixin[T]",
        fltr: Annotated[TFilter, Query()] = None,
        sort: str | None = None,
    ) -> list[T]:
        has_filter = (
            fltr is not None
            and hasattr(fltr, "model_dump")
            and any(v is not None for v in fltr.model_dump().values())
        )
        sort_state: SortState = parse_sort_param(sort)

        if has_filter:
            await self.setup_filter(fltr)
        if sort_state:
            await self.setup_sort(sort_state)
        res = await self.perform_list()
        if has_filter:
            res = await self.filter_list(fltr, res)
        if sort_state:
            res = await self.sort_list(sort_state, res)
        return res

    async def setup_filter(self, fltr: TFilter) -> None:
        """
        Optional pre-filter hook called before perform_list when a filter is active.
        Subclasses can override to set up server-side filtering (e.g., build a DB query).
        """

    async def filter_list(self, fltr: TFilter, records: list[T]) -> list[T]:
        """
        Post-filter hook called after perform_list when a filter is active.
        Subclasses implement this to filter records in-memory.
        """

    async def setup_sort(self, sort: SortState) -> None:
        """
        Optional pre-sort hook called before perform_list when a sort order is active.
        Subclasses can override to apply server-side ordering (e.g., add ORDER BY to a DB query).
        """

    async def sort_list(self, sort: SortState, records: list[T]) -> list[T]:
        """
        Post-sort hook called after perform_list (and filter_list) when a sort order is active.
        Default implementation performs a stable in-memory multi-key sort. Null values sort last
        for asc, first for desc. Override for custom behaviour.
        """
        import functools

        def compare(a: T, b: T) -> int:
            for col in sort:
                val_a = getattr(a, col.column_name, None)
                val_b = getattr(b, col.column_name, None)
                if val_a is None and val_b is None:
                    continue
                if val_a is None:
                    return 1 if col.direction == SortDirection.asc else -1
                if val_b is None:
                    return -1 if col.direction == SortDirection.asc else 1
                try:
                    cmp = (val_a > val_b) - (val_a < val_b)
                except TypeError:
                    continue
                if col.direction == SortDirection.desc:
                    cmp = -cmp
                if cmp != 0:
                    return cmp
            return 0

        return sorted(records, key=functools.cmp_to_key(compare))


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
    async def retrieve(self: "ImplMixin[K, T] | RetrieveMixin[K, T]", pk: K) -> T:
        return await self.perform_retrieve(pk)


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
    async def update(self: "ImplMixin[K, T] | UpdateMixin[K, T]", pk: K, data: T) -> T:
        return await self.perform_update(pk, data, partial=False)

    @final
    @__router.patch("/{pk}", name="partial_update", responses=NOT_FOUND_RESPONSE)
    async def partial_update(self: "ImplMixin[K, T] | UpdateMixin[K, T]", pk: K, data: T) -> T:
        return await self.perform_update(pk, data, partial=True)


class BulkOnlyUpdateMixin(Generic[K, T], ABC):
    """
    Update model instances in bulk.
    """
    __router = APIRouter()

    @final
    @__router.put("bulk")
    async def bulk_update(self: "ImplMixin[K, T] | BulkOnlyUpdateMixin[K, T]", records: dict[K, T]) -> list[T]:
        return await self.perform_bulk_update(records, partial=False)

    @final
    @__router.patch("bulk", name="bulk_partial_update")
    async def bulk_partial_update(self: "ImplMixin[K, T] | BulkOnlyUpdateMixin[K, T]", records: dict[K, T]) -> list[T]:
        return await self.perform_bulk_update(records, partial=True)


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
    async def destroy(self: "ImplMixin[K, T] | DestroyMixin[K, T]", pk: K) -> dict[K, Any]:
        return await self.perform_destroy(pk)


class BulkOnlyDestroyMixin(Generic[K, T], ABC):
    """
    Destroy model instances in bulk.
    """
    __router = APIRouter()

    @final
    @__router.delete("bulk")
    async def bulk_destroy(self: "ImplMixin[K, T] | BulkOnlyDestroyMixin[K, T]", pk: list[K]) -> list[dict[K, Any]]:
        return await self.perform_bulk_destroy(pk)


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
    async def perform_lookup(self) -> list[LookupItem]:
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
        fltr: Annotated[TLookupFilter, Query()] = None,
    ) -> list[LookupItem]:
        has_filter = (
            fltr is not None
            and hasattr(fltr, "model_dump")
            and any(v is not None for v in fltr.model_dump().values())
        )
        if has_filter:
            await self.setup_lookup_filter(fltr)
        res = await self.perform_lookup()
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
