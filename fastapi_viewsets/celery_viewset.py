from typing import Any, Generic, TYPE_CHECKING, TypeVar

from .mixins import BulkViewSetMixin

if TYPE_CHECKING:
    from celery import Celery


T = TypeVar("T")
K = TypeVar("K")


class CeleryViewSet(BulkViewSetMixin[K, T], Generic[K, T]):
    def __init__(self, celery: "Celery", task_prefix: str):
        self.celery = celery
        self.task_prefix = task_prefix

    async def perform_create(self, data):
        result = self.celery.send_task(f"{self.task_prefix}.create", args=[data])
        return result

    async def perform_bulk_create(self, data: list):
        result = self.celery.send_task(f"{self.task_prefix}.bulk_create", args=[data])
        return result

    async def perform_list(self):
        result = self.celery.send_task(f"{self.task_prefix}.list")
        return result

    async def perform_retrieve(self, pk):
        result = self.celery.send_task(f"{self.task_prefix}.retrieve", args=[pk])
        return result

    async def perform_update(self, pk, data, partial: bool = True):
        task_name = "partial_update" if partial else "update"
        result = self.celery.send_task(f"{self.task_prefix}.{task_name}", args=[pk, data])
        return result

    async def perform_bulk_update(self, records: dict, partial: bool = True):
        task_name = "bulk_partial_update" if partial else "bulk_update"
        result = self.celery.send_task(f"{self.task_prefix}.{task_name}", args=[records])
        return result

    async def perform_destroy(self, pk) -> dict[Any, Any]:
        result = self.celery.send_task(f"{self.task_prefix}.destroy", args=[pk])
        return result

    async def perform_bulk_destroy(self, pk: list) -> list[dict[Any, Any]]:
        result = self.celery.send_task(f"{self.task_prefix}.bulk_destroy", args=[pk])
        return result
