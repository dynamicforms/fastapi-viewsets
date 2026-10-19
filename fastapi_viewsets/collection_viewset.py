import dataclasses

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, MutableMapping, MutableSequence, MutableSet
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

from .context import Context
from .exceptions import NotFoundError, ReadOnlyCollectionError
from .mixins import ImplMixin

T = TypeVar("T")
K = TypeVar("K")

ReadableCollection = Iterable[T]  # list/retrieve only
MutableCollection = MutableSequence[T] | MutableSet[T] | MutableMapping[K, T]  # + create/update/delete


class AsyncCollectionViewSet(
    Generic[K, T],
    ImplMixin[K, T],
    ABC,
):
    @abstractmethod
    async def container_list(self) -> list[T]:
        raise NotImplementedError

    @abstractmethod
    async def container_retrieve(self, pk: K) -> T:
        raise NotImplementedError

    @abstractmethod
    async def container_add(self, data: T) -> T:
        raise NotImplementedError

    @abstractmethod
    async def container_update(self, pk: K, data: T, partial: bool) -> T:
        raise NotImplementedError

    @abstractmethod
    async def container_delete(self, pk: K) -> None:
        raise NotImplementedError

    async def perform_list(self, _context: Context) -> list[T]:
        return await self.container_list()

    async def perform_retrieve(self, _context: Context, pk: K) -> T:
        return await self.container_retrieve(pk)

    async def perform_create(self, _context: Context, data: T) -> T:
        return await self.container_add(data)

    async def perform_bulk_create(self, _context: Context, data: list[T]) -> list[T]:
        results = []
        for item in data:
            results.append(await self.container_add(item))
        return results

    async def perform_update(self, _context: Context, pk: K, data: T, partial: bool = True) -> T:
        return await self.container_update(pk, data, partial)

    async def perform_bulk_update(self, _context: Context, records: dict[K, T], partial: bool = True) -> list[T]:
        results = []
        for pk, data in records.items():
            results.append(await self.container_update(pk, data, partial))
        return results

    async def perform_destroy(self, _context: Context, pk: K) -> dict[K, Any]:
        await self.container_delete(pk)
        return {pk: None}

    async def perform_bulk_destroy(self, _context: Context, pk: list[K]) -> list[dict[K, Any]]:
        results = []
        for p in pk:
            await self.container_delete(p)
            results.append({p: None})
        return results


class CollectionViewSet(AsyncCollectionViewSet[K, T]):
    def __init__(self, container: ReadableCollection[T] | MutableCollection[K, T], pk_field: str = "id"):
        self.container = container
        self.pk_field = pk_field
        self.is_mutable = isinstance(container, (MutableSequence, MutableSet, MutableMapping))

        self._add_func: Callable | None = None
        self._update_func: Callable | None = None
        self._delete_func: Callable | None = None

        if self.is_mutable:
            if isinstance(container, MutableSequence):
                self._add_func = container.append
                self._update_func = self._update_sequence
                self._delete_func = self._delete_sequence
            elif isinstance(container, MutableSet):
                self._add_func = container.add
                self._update_func = self._update_set
                self._delete_func = self._delete_set
            elif isinstance(container, MutableMapping):
                self._add_func = self._add_mapping
                self._update_func = self._update_mapping
                self._delete_func = container.__delitem__

    @property
    def pk_field_name(self) -> str:
        """`pk_field`, the name the list's cursor shape and `ChangeEvents` read the pk under."""
        return self.pk_field

    def _check_mutable(self):
        if not self.is_mutable:
            raise ReadOnlyCollectionError()

    def _get_pk(self, item: T) -> K:
        if isinstance(item, dict):
            return item[self.pk_field]
        return getattr(item, self.pk_field)

    @staticmethod
    def _stated_fields(data: T) -> dict[str, Any]:
        """The fields a partial update states: those a model has set, or a dict's keys."""
        if isinstance(data, BaseModel):
            return {name: getattr(data, name) for name in data.model_fields_set}
        return dict(data)

    def _apply_partial(self, item: T, data: T) -> T:
        """`item` with the fields `data` states written onto it, in place."""
        fields = self._stated_fields(data)
        if isinstance(item, dict):
            item.update(fields)
        else:
            for key, value in fields.items():
                setattr(item, key, value)
        return item

    # Sequence helpers
    def _update_sequence(self, pk: K, data: T, partial: bool):
        for i, item in enumerate(self.container):
            if self._get_pk(item) == pk:
                if partial:
                    self._apply_partial(item, data)
                else:
                    self.container[i] = data
                return self.container[i]
        raise NotFoundError(pk)

    def _delete_sequence(self, pk: K):
        for i, item in enumerate(self.container):
            if self._get_pk(item) == pk:
                del self.container[i]
                return
        raise NotFoundError(pk)

    # Set helpers
    def _update_set(self, pk: K, data: T, partial: bool):
        # an item of a set is hashable, so it is replaced rather than changed in place
        for item in self.container:
            if self._get_pk(item) == pk:
                if partial:
                    data = _replaced(item, self._stated_fields(data))
                self.container.remove(item)
                self.container.add(data)
                return data
        raise NotFoundError(pk)

    def _delete_set(self, pk: K):
        for item in self.container:
            if self._get_pk(item) == pk:
                self.container.remove(item)
                return
        raise NotFoundError(pk)

    # Mapping helpers
    def _add_mapping(self, data: T):
        pk = self._get_pk(data)
        self.container[pk] = data

    def _update_mapping(self, pk: K, data: T, partial: bool):
        if pk not in self.container:
            raise NotFoundError(pk)
        if partial:
            self._apply_partial(self.container[pk], data)
        else:
            self.container[pk] = data
        return self.container[pk]

    async def container_list(self) -> list[T]:
        if isinstance(self.container, MutableMapping):
            return list(self.container.values())
        return list(self.container)

    async def container_retrieve(self, pk: K) -> T:
        if isinstance(self.container, MutableMapping):
            if pk in self.container:
                return self.container[pk]
        else:
            for item in self.container:
                if self._get_pk(item) == pk:
                    return item
        raise NotFoundError(pk)

    async def container_add(self, data: T) -> T:
        self._check_mutable()
        self._handle_autoinc(data)
        self._add_func(data)
        return data

    def _handle_autoinc(self, data: T):
        if not isinstance(data, BaseModel):
            return

        for field_name, field_info in data.__class__.model_fields.items():
            if field_info.json_schema_extra and field_info.json_schema_extra.get("autoinc_int"):
                # a truthy value is kept; 0 or None gets the next value after the container's maximum
                current_val = getattr(data, field_name, None)
                if current_val:
                    continue

                # We find the max value in the container
                max_val = 0
                items = self.container.values() if isinstance(self.container, MutableMapping) else self.container
                for item in items:
                    try:
                        val = item[field_name] if isinstance(item, dict) else getattr(item, field_name, 0)
                        if isinstance(val, int) and val > max_val:
                            max_val = val
                    except (KeyError, AttributeError):
                        continue

                setattr(data, field_name, max_val + 1)

    async def container_update(self, pk: K, data: T, partial: bool) -> T:
        self._check_mutable()
        return self._update_func(pk, data, partial)

    async def container_delete(self, pk: K) -> None:
        self._check_mutable()
        try:
            self._delete_func(pk)
        except KeyError as e:
            raise NotFoundError(pk) from e


def _replaced(item: Any, fields: dict[str, Any]) -> Any:
    """A copy of the hashable `item` with `fields` written onto it: a pydantic model, a named tuple
    or a dataclass."""
    if isinstance(item, BaseModel):
        return item.model_copy(update=fields)
    if hasattr(item, "_replace"):
        return item._replace(**fields)
    if dataclasses.is_dataclass(item):
        return dataclasses.replace(item, **fields)
    raise TypeError(
        f"{type(item).__name__} cannot be updated partially: it is neither a model, a named tuple nor a dataclass"
    )
