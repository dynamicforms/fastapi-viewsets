"""
Viewsets mounted under another viewset's base path with `ViewSetRouter.sub_viewset`.

`route_viewset` routes each mount at `{base_path}/{path}` as a viewset of its own; a request to it passes the
parent's checks for the action `path` first, then the child's own.
"""

import re

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, get_args

from fastapi import APIRouter

from .lifecycle_runner import LifecycleType

_PATH = re.compile(r"[A-Za-z0-9_.~-]+")

RESERVED_PATHS = frozenset({"schema"})
"""Sub-paths a viewset serves itself and no sub-viewset may take."""


@dataclass(frozen=True)
class SubViewSet:
    """A viewset mounted by `ViewSetRouter.sub_viewset`."""

    path: str
    """One path segment under the parent's base path, also the parent's action name for its checks."""
    viewset: type
    """The class of the mounted viewset."""
    instance: Any | None
    """The instance passed to `sub_viewset`, used as the singleton; None when a class was passed."""
    lifecycle: LifecycleType
    pk_field_name: str | None
    register_muxws: bool | None
    """None takes the value the parent is routed with."""
    register_rest: bool | None
    """None takes the value the parent is routed with."""
    action_configuration: dict[Any, Any] = field(default_factory=dict)
    """Configuration of the parent's action `path`, merged over the parent's class-level `@action_configuration`."""


class ViewSetRouter(APIRouter):
    """An `APIRouter`, declared as a viewset's `__router`, that can also mount viewsets under its base path."""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.sub_viewsets: dict[str, SubViewSet] = {}
        """The mounted viewsets by sub-path, in the order they were mounted."""

    def sub_viewset(
        self,
        path: str,
        viewset: Any,
        *,
        lifecycle: LifecycleType = "singleton",
        pk_field_name: str | None = None,
        register_muxws: bool | None = None,
        register_rest: bool | None = None,
        action_configuration: dict[Any, Any] | None = None,
    ) -> SubViewSet:
        """
        Mounts `viewset` (a class, or an instance used as the singleton) at `{base_path}/{path}`. Arguments are
        as for `route_viewset`; None transport flags take the parent's. `action_configuration` configures the
        parent's action `path`.

        Raises `ValueError` for a `path` that is not one segment of `[A-Za-z0-9_.~-]`, for `schema`, for a path
        already mounted, an unknown `lifecycle`, or an instance with a non-singleton lifecycle; `TypeError` for
        a `viewset` that is neither a class nor an instance.
        """
        segment = path.strip("/")
        if not _PATH.fullmatch(segment):
            raise ValueError(f"sub_viewset path {path!r} is not one path segment")
        if segment in RESERVED_PATHS:
            raise ValueError(f"sub_viewset path {path!r} is reserved for the parent's own /{segment} endpoint")
        if segment in self.sub_viewsets:
            raise ValueError(f"sub_viewset path {path!r} is already mounted on this router")
        if lifecycle not in get_args(LifecycleType):
            raise ValueError(f"unknown lifecycle {lifecycle!r}; one of: {', '.join(get_args(LifecycleType))}")
        if isinstance(viewset, type):
            cls, instance = viewset, None
        elif hasattr(viewset, "__dict__"):
            cls, instance = type(viewset), viewset
            if lifecycle != "singleton":
                raise ValueError(
                    f"sub_viewset {path!r}: an instance is a singleton; lifecycle {lifecycle!r} needs a class"
                )
        else:
            raise TypeError(f"sub_viewset {path!r}: {viewset!r} is neither a viewset class nor an instance of one")
        mount = SubViewSet(
            path=segment,
            viewset=cls,
            instance=instance,
            lifecycle=lifecycle,
            pk_field_name=pk_field_name,
            register_muxws=register_muxws,
            register_rest=register_rest,
            action_configuration=dict(action_configuration or {}),
        )
        self.sub_viewsets[segment] = mount
        return mount


def sub_viewsets_of(cls: type) -> dict[str, SubViewSet]:
    """The viewsets `cls` mounts across its MRO, by sub-path; a more derived class's mount wins."""
    mounts: dict[str, SubViewSet] = {}
    for base in reversed(cls.__mro__):
        router = getattr(base, f"_{base.__name__}__router", None)
        if isinstance(router, ViewSetRouter):
            mounts.update(router.sub_viewsets)
    return mounts


def mounted_under(cls: type) -> Iterator[type]:
    """Every class mounted under `cls`: its sub-viewsets, theirs, and so on, each once."""
    seen: set[type] = set()
    pending = [cls]
    while pending:
        for mount in sub_viewsets_of(pending.pop()).values():
            if mount.viewset not in seen:
                seen.add(mount.viewset)
                pending.append(mount.viewset)
                yield mount.viewset
