from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, MutableMapping, MutableSequence, MutableSet
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

from .mixins import ImplMixin
from .response_classes import NotFoundError

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

    async def perform_list(self) -> list[T]:
        return await self.container_list()

    async def perform_retrieve(self, pk: K) -> T:
        return await self.container_retrieve(pk)

    async def perform_create(self, data: T) -> T:
        return await self.container_add(data)

    async def perform_bulk_create(self, data: list[T]) -> list[T]:
        results = []
        for item in data:
            results.append(await self.container_add(item))
        return results

    async def perform_update(self, pk: K, data: T, partial: bool = True) -> T:
        return await self.container_update(pk, data, partial)

    async def perform_bulk_update(self, records: dict[K, T], partial: bool = True) -> list[T]:
        results = []
        for pk, data in records.items():
            results.append(await self.container_update(pk, data, partial))
        return results

    async def perform_destroy(self, pk: K) -> dict[K, Any]:
        await self.container_delete(pk)
        return {pk: None}

    async def perform_bulk_destroy(self, pk: list[K]) -> list[dict[K, Any]]:
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
                self._delete_func = container.remove
            elif isinstance(container, MutableMapping):
                self._add_func = self._add_mapping
                self._update_func = self._update_mapping
                self._delete_func = container.__delitem__

    def _check_mutable(self):
        if not self.is_mutable:
            raise Exception("Provided container is not mutable")

    def _get_pk(self, item: T) -> K:
        if isinstance(item, dict):
            return item[self.pk_field]
        return getattr(item, self.pk_field)

    # Sequence helpers
    def _update_sequence(self, pk: K, data: T, partial: bool):
        for i, item in enumerate(self.container):
            if self._get_pk(item) == pk:
                if partial:
                    if isinstance(item, dict):
                        item.update(data)
                    else:
                        for key, value in data.items():
                            setattr(item, key, value)
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
        for item in self.container:
            if self._get_pk(item) == pk:
                self.container.remove(item)
                self.container.add(data)  # Sets don't really support partial update easily without mutation
                return data
        raise NotFoundError(pk)

    # Mapping helpers
    def _add_mapping(self, data: T):
        pk = self._get_pk(data)
        self.container[pk] = data

    def _update_mapping(self, pk: K, data: T, partial: bool):
        if pk not in self.container:
            raise NotFoundError(pk)
        if partial:
            item = self.container[pk]
            if isinstance(item, dict):
                item.update(data)
            else:
                for key, value in data.items():
                    setattr(item, key, value)
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
                # If the field is already set and is not 0 or None, we leave it (unless this is the desired behavior)
                # However, we usually want autoinc only if the value is not yet set
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
