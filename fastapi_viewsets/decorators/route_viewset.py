import inspect
import re

from dataclasses import dataclass
from functools import wraps
from typing import Annotated, get_args, get_origin, Literal, TypeVar

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.params import FieldInfo
from pydantic import BaseModel

try:
    from typing_extensions import NoDefault as _TypeVarNoDefault
except ImportError:
    _TypeVarNoDefault = object()  # fallback sentinel — never equal to any real TypeVar default

from fastapi_viewsets.action_configuration import extra_middlewares_for, resolve_action_configuration
from fastapi_viewsets.conf import settings
from fastapi_viewsets.context import build_context, Context, get_shared_context
from fastapi_viewsets.endpoint_docs import (
    DOCS_ATTRIBUTE,
    docs_for,
    register_tag,
    viewset_description,
)
from fastapi_viewsets.middleware import any_modifies_response_shape, Middleware, unwrap_viewset_result_type
from fastapi_viewsets.mixins import FilterParam
from fastapi_viewsets.mux_ws import register_viewset, resolve_register_muxws, resolve_register_rest
from fastapi_viewsets.response_classes import with_error_responses
from fastapi_viewsets.routing import MUXWS, openapi_endpoint, REST, transport_route_class

from .build_schema import _pydantic_generic_args, build_schema, route_to_add_api_route_kwargs
from .lifecycle_runner import lifecycle_runner, LifecycleType
from .primary_key_model_helper import (
    create_model_without_pk,
    from_partial_body,
    partial_body_annotation,
    PARTIAL_UPDATE_ACTIONS,
    typecast_to_original_model,
)
from .query_model import query_model_dependency
from .sub_viewsets import sub_viewsets_of, SubViewSet

T = TypeVar("T")

MIDDLEWARE_DEPENDS_ATTRIBUTE = "__viewset_middleware_depends__"
"""Marks the dependency `route_viewset` appends to each route to run `Middleware.depends()`."""

SUB_VIEWSET_GATE_ATTRIBUTE = "__viewset_sub_viewset_gate__"
"""
Marks a sub-viewset route's dependency running a parent's checks; value `(parent class, action name)`.
One per nesting level, outermost first, ahead of the `MIDDLEWARE_DEPENDS_ATTRIBUTE` dependency.
"""


def build_type_map(cls: type[T], current_map: dict = None) -> dict:
    if current_map is None:
        current_map = {}

    type_map = dict(current_map)

    if not hasattr(cls, "__orig_bases__"):
        return type_map

    for base in cls.__orig_bases__:
        origin = getattr(base, "__origin__", None)
        if not origin:
            continue

        args = get_args(base)
        params = getattr(origin, "__parameters__", ())

        new_map = {}
        for param, arg in zip(params, args, strict=False):
            # Try to resolve arg (which can be a TypeVar or e.g. List[T]) from current mapping
            new_map[param] = resolve_typevars(type_map, arg)

        type_map.update(new_map)
        type_map.update(build_type_map(origin, new_map))

    return type_map


def resolve_typevars(type_map, annotation):
    if isinstance(annotation, TypeVar):
        resolved = type_map.get(annotation, annotation)
        if not isinstance(resolved, TypeVar):
            return resolved
        # Fall back to PEP 696 TypeVar default when the TypeVar is not bound in the type_map
        tv_default = getattr(resolved, "__default__", _TypeVarNoDefault)
        if tv_default is not _TypeVarNoDefault and not isinstance(tv_default, TypeVar):
            return tv_default
    elif _pydantic_generic_args(annotation):
        # A parameterised pydantic model (PaginatedList[T]) is a class that get_origin/get_args do not
        # see through; pydantic keeps origin and args on the class, and re-subscripting rebuilds it.
        origin, args = _pydantic_generic_args(annotation)
        new_args = tuple(resolve_typevars(type_map, arg) for arg in args)
        if new_args != args:
            try:
                return origin[new_args]
            except Exception:
                return annotation
    elif get_origin(annotation) is not None:
        origin = get_origin(annotation)
        args = get_args(annotation)
        new_args = tuple(resolve_typevars(type_map, arg) for arg in args)
        if new_args != args:
            # list[...], dict[...], Union[...] and the other subscriptable generics
            try:
                return origin[new_args]
            except:
                return annotation
    return annotation


def _make_middleware_depends(cls: type, action_name: str):
    """
    A per-route FastAPI dependency running `Middleware.depends()` before the body is parsed.
    `@action_configuration` and settings are resolved per request.
    """

    async def _run(request: Request) -> None:
        action_configuration = resolve_action_configuration(cls, action_name)
        context = await get_shared_context(request, cls, action_configuration, action_name)
        await _run_depends(request, cls, action_configuration, context)

    middleware_depends = _with_security_scheme(_run)
    setattr(middleware_depends, MIDDLEWARE_DEPENDS_ATTRIBUTE, True)
    return middleware_depends


def _make_sub_viewset_gate(cls: type, mount: SubViewSet):
    """
    The dependency running `cls`'s checks for action `mount.path` on a request to the mounted viewset:
    security scheme, context processors and `depends()`, with `mount.action_configuration` over `cls`'s own.
    Its context is built for this check only.
    """

    async def _run(request: Request) -> None:
        action_configuration = {**resolve_action_configuration(cls, mount.path), **mount.action_configuration}
        context = await build_context(request, cls, action_configuration, mount.path)
        await _run_depends(request, cls, action_configuration, context)

    gate = _with_security_scheme(_run)
    setattr(gate, SUB_VIEWSET_GATE_ATTRIBUTE, (cls, mount.path))
    return gate


async def _run_depends(request: Request, cls: type, action_configuration: dict, context: Context) -> None:
    effective_middlewares = settings.viewsets_command_middleware + extra_middlewares_for(
        action_configuration, settings.viewsets_command_middleware
    )
    for middleware in effective_middlewares:
        # Plain-function middleware has no depends().
        if isinstance(middleware, Middleware):
            await middleware.depends(request, cls, context)


def _with_security_scheme(run):
    """A dependency calling `run(request)`, behind `settings.viewsets_security_scheme` when one is set."""
    scheme = settings.viewsets_security_scheme
    if scheme is None:

        async def dependency(request: Request) -> None:
            await run(request)

    else:

        async def dependency(request: Request, _credential=Depends(scheme)) -> None:  # noqa: B008
            await run(request)

    return dependency


def route_viewset(
    router: APIRouter,
    base_path: str,
    lifecycle: LifecycleType = "singleton",
    pk_field_name: str = None,
    register_muxws: bool = None,
    register_rest: bool = None,
):
    """
    Routes a viewset class on `router` at `base_path`, its sub-viewsets first.

    `register_rest` / `register_muxws`: None defers, REST to True and muxws to
    `settings.viewsets_register_muxws`; `@transports(...)` overrides per endpoint. Sets
    `cls.__viewset_metadata__` and marks each endpoint with `__viewset_class__` and `__viewset_action__`.
    """

    if lifecycle not in get_args(LifecycleType):
        raise ValueError(f"unknown lifecycle {lifecycle!r}; one of: {', '.join(get_args(LifecycleType))}")

    def decorator(cls: type[T]):
        return _route(router, base_path, cls, None, lifecycle, pk_field_name, register_muxws, register_rest)

    return decorator


def _route(
    router: APIRouter,
    base_path: str,
    cls: type[T],
    instance: T | None,
    lifecycle: LifecycleType,
    pk_field_name: str | None,
    register_muxws: bool | None,
    register_rest: bool | None,
    parent: "_Parent | None" = None,
) -> type[T]:
    """
    Routes `cls` at `base_path`, sub-viewsets first. `instance` is the singleton when given; `parent` is
    set for a sub-viewset.
    """
    if instance is None and lifecycle == "singleton":
        instance = cls()

    type_map = build_type_map(cls)

    if parent is None:
        # Tag: the class name without its "ViewSet" suffix; its description is the viewset's docstring.
        cls_name = cls.__name__
        if cls_name.endswith("ViewSet"):
            cls_name = cls_name[: -len("ViewSet")]
        default_tags = [cls_name] if cls_name else None
        if cls_name:
            register_tag(cls_name, viewset_description(cls))
    else:
        # A sub-viewset's endpoints are listed in the group of the viewset at the top of its mounts.
        default_tags = parent.tags

    def _is_filter_param(annotation) -> bool:
        """Return True when annotation is Annotated[T, FilterParam()]."""
        return hasattr(annotation, "__metadata__") and any(isinstance(m, FilterParam) for m in annotation.__metadata__)

    def _is_model_query_param(annotation) -> bool:
        """Return True for Annotated[PydanticModel, <FieldInfo>] (e.g. Query()), read from the query string."""
        args = getattr(annotation, "__args__", None)
        metadata = getattr(annotation, "__metadata__", None)
        if not (args and metadata):
            return False
        inner = args[0]
        if not (inspect.isclass(inner) and issubclass(inner, BaseModel)):
            return False
        return any(isinstance(m, FieldInfo) for m in metadata)

    def _shape_signature(sig):
        """
        Trims the shape-aware list endpoint to this viewset's shapes: drops unused parameters, keeps
        `X-List-Shape` only when there is a choice, returns the union of the allowed models and sets
        `default_sort` as the `sort` default.
        """
        from fastapi_viewsets.list_shapes import response_model, shape_examples, SHAPE_HEADER
        from fastapi_viewsets.mixins import check_declared_sort, T as ItemVar

        default, allowed = cls.resolve_shapes()
        item_type = type_map.get(ItemVar, ItemVar)
        uses = {
            "offset": "paginated" in allowed,
            "cursor": "cursor" in allowed,
            "limit": bool({"paginated", "cursor"} & set(allowed)),
            "x_list_shape": len(allowed) > 1,
        }

        params = []
        for name, parameter in sig.parameters.items():
            if uses.get(name) is False:
                continue
            if name == "x_list_shape":
                parameter = parameter.replace(
                    annotation=Annotated[
                        Literal[(*allowed, None)],
                        Header(
                            alias=SHAPE_HEADER,
                            description=f"Response shape. Omit for this endpoint's default ({default}).",
                        ),
                    ]
                )
            if name == "sort" and cls.default_sort is not None:
                check_declared_sort(cls.default_sort, item_type)
                parameter = parameter.replace(default=cls.default_sort)
            params.append(parameter)

        nonlocal shape_openapi_extra
        shape_openapi_extra = shape_examples(allowed, item_type)
        return sig.replace(parameters=params, return_annotation=response_model(allowed, item_type))

    shape_openapi_extra = None

    def get_wrapper(original_endpoint, route_path, route_methods):
        nonlocal shape_openapi_extra
        shape_openapi_extra = None
        sig = inspect.signature(original_endpoint)
        if getattr(original_endpoint, "__list_shape_aware__", False):
            sig = _shape_signature(sig)
        new_params = []
        needs_context = False
        # the body parameters of a partial update, by name, with the annotation they replace
        partial_params: dict[str, object] = {}

        for name, p in sig.parameters.items():
            if name == "self":
                continue

            annotation = resolve_typevars(type_map, p.annotation)
            new_p = p.replace(annotation=annotation)

            # `context` (detected by name) never reaches FastAPI; lifecycle_runner builds and injects it.
            if name == "context":
                needs_context = True
                continue

            # Annotated[PydanticModel, FilterParam() | <FieldInfo>]: one query parameter per model field
            if _is_filter_param(annotation) or _is_model_query_param(annotation):
                inner_type = annotation.__args__[0]
                if inspect.isclass(inner_type) and issubclass(inner_type, BaseModel):
                    new_p = new_p.replace(annotation=Annotated[inner_type, Depends(query_model_dependency(inner_type))])
                new_params.append(new_p)
                continue

            # A partial update's body is validated with every field optional, then handed on as the
            # original model holding only the stated fields
            if original_endpoint.__name__ in PARTIAL_UPDATE_ACTIONS:
                partial_annotation = partial_body_annotation(annotation, pk_field_name)
                if partial_annotation is not None:
                    partial_params[name] = annotation
                    new_params.append(new_p.replace(annotation=partial_annotation))
                    continue

            # A create's body is the model without its primary key, which the backend assigns
            if pk_field_name and inspect.isclass(annotation) and issubclass(annotation, BaseModel):
                is_create = "POST" in route_methods and "{pk}" not in route_path
                if is_create and pk_field_name in annotation.model_fields:
                    new_p = new_p.replace(annotation=create_model_without_pk(annotation, pk_field_name))

            new_params.append(new_p)

        # Reserved: FastAPI injects the real Response, onto which lifecycle_runner applies headers/cookies.
        new_params.append(inspect.Parameter("response", inspect.Parameter.KEYWORD_ONLY, annotation=Response))

        # Reserved: FastAPI injects the real Request, from which lifecycle_runner builds the context.
        # An endpoint declaring `request` itself receives it.
        forwards_request = "request" in sig.parameters
        if not forwards_request:
            new_params.append(inspect.Parameter("request", inspect.Parameter.KEYWORD_ONLY, annotation=Request))

        # -> ViewSetResult[X] documents/validates as X - see unwrap_viewset_result_type.
        new_return_annotation = unwrap_viewset_result_type(resolve_typevars(type_map, sig.return_annotation))
        new_sig = sig.replace(parameters=new_params, return_annotation=new_return_annotation)

        @wraps(original_endpoint)
        async def wrapper(*args, **kwargs):
            nonlocal instance

            response_obj = kwargs.pop("response", None)
            request_obj = kwargs.pop("request", None)

            # Bodies whose model get_wrapper replaced (no-pk, partial) go back to the original model
            new_kwargs = {}
            for param_name, value in kwargs.items():
                if param_name in partial_params:
                    new_kwargs[param_name] = from_partial_body(value, partial_params[param_name])
                elif param_name in sig.parameters:
                    orig_param = sig.parameters[param_name]
                    orig_annotation = resolve_typevars(type_map, orig_param.annotation)
                    # Filter params must not be typecast — they arrive as the all-optional model
                    if _is_filter_param(orig_annotation):
                        new_kwargs[param_name] = value
                    else:
                        new_kwargs[param_name] = typecast_to_original_model(value, orig_annotation)
                else:
                    new_kwargs[param_name] = value

            return await lifecycle_runner(
                original_endpoint,
                instance,
                cls,
                lifecycle,
                *args,
                response=response_obj,
                needs_context=needs_context,
                request=request_obj,
                forwards_request=forwards_request,
                **new_kwargs,
            )

        wrapper.__signature__ = new_sig
        # Read by build_schema when it registers the route.
        wrapper.__shape_openapi_extra__ = shape_openapi_extra
        return wrapper, new_return_annotation

    # A middleware with modifies_response_shape=True makes the declared return type unusable as response_model.
    build_schema(
        cls,
        base_path,
        default_tags,
        get_wrapper,
        disable_response_model=any_modifies_response_shape(settings.viewsets_command_middleware),
    )

    def is_schema_route(route) -> bool:
        return getattr(route.endpoint, "__viewset_schema_endpoint__", False)

    def add_route(route_kwargs: dict, transports: set[str], action_name: str) -> None:
        # One route per endpoint, whatever its transports, so both pass the application's middleware,
        # handlers and dependencies; it matches only its transports and is documented only on REST.
        route_kwargs["endpoint"].__viewset_class__ = cls
        route_kwargs["endpoint"].__viewset_action__ = action_name
        router.add_api_route(
            **{**route_kwargs, "include_in_schema": route_kwargs["include_in_schema"] and REST in transports},
            route_class_override=transport_route_class(router.route_class, frozenset(transports)),
        )

    on_muxws_any = any(
        resolve_register_muxws(route.endpoint, register_muxws)
        for route in cls.__router.routes
        if not is_schema_route(route)
    )
    base = base_path.rstrip("/")
    gates = parent.gates if parent is not None else ()
    chain = (*parent.chain, cls) if parent is not None else (cls,)
    mounts = sub_viewsets_of(cls)
    _check_mounts(cls, base, mounts, chain)
    # Routed first: otherwise `{base_path}/{pk}` would match `{base_path}/members` and answer 422.
    for mount in mounts.values():
        _route(
            router,
            f"{base}/{mount.path}",
            mount.viewset,
            mount.instance,
            mount.lifecycle,
            mount.pk_field_name,
            register_muxws if mount.register_muxws is None else mount.register_muxws,
            register_rest if mount.register_rest is None else mount.register_rest,
            _Parent(
                cls=cls,
                tags=default_tags,
                gates=(*gates, Depends(_make_sub_viewset_gate(cls, mount))),
                summary_prefix=" / ".join(
                    part for part in (parent.summary_prefix if parent else "", _summary_label(mount.path)) if part
                ),
                chain=chain,
            ),
        )

    rest_routes = []
    muxws_routes = []
    documented = set()
    # action name -> the route's path relative to base_path
    paths: dict[str, str] = {}
    for route in cls.__router.routes:
        action_name = route.endpoint.__name__  # survives @wraps(original_endpoint) in get_wrapper
        documented.add(action_name)
        paths.setdefault(action_name, route.path[len(base) :] if route.path.startswith(base) else route.path)
        dependencies = [
            *(route.dependencies or []),
            *gates,
            Depends(_make_middleware_depends(cls, action_name)),
        ]
        route_kwargs = route_to_add_api_route_kwargs(
            route, dependencies=dependencies, responses=with_error_responses(route.responses)
        )
        # Per-viewset wording from @endpoint_docs.
        route_kwargs.update(docs_for(cls, action_name))
        if parent is not None and not route_kwargs.get("summary"):
            # Prefix the mount, telling a sub-viewset's `List Items` apart from the parent's in its group.
            default_summary = (route_kwargs.get("name") or action_name).replace("_", " ").title()
            route_kwargs["summary"] = f"{parent.summary_prefix}: {default_summary}"
        on_rest = resolve_register_rest(route.endpoint, register_rest)

        if is_schema_route(route):
            # One /schema route per transport, each listing that transport's endpoints, itself included
            # (the TS client derives the base path from it).
            if on_rest:
                rest_schema_kwargs = {**route_kwargs, "endpoint": openapi_endpoint(rest_routes)}
                rest_routes.append(rest_schema_kwargs)
                add_route(rest_schema_kwargs, {REST}, action_name)
            if on_muxws_any:
                muxws_schema_kwargs = {**route_kwargs, "endpoint": openapi_endpoint(muxws_routes)}
                muxws_routes.append(muxws_schema_kwargs)
                add_route(muxws_schema_kwargs, {MUXWS}, action_name)
            continue

        on_muxws = resolve_register_muxws(route.endpoint, register_muxws)
        transports = set()
        if on_rest:
            transports.add(REST)
            rest_routes.append(route_kwargs)
        if on_muxws:
            transports.add(MUXWS)
            muxws_routes.append(route_kwargs)
        if transports:
            add_route(route_kwargs, transports, action_name)

    # Checked here: the @endpoint_docs decorator runs before any route exists.
    unknown = set(getattr(cls, DOCS_ATTRIBUTE, {})) - documented
    if unknown:
        raise ValueError(
            f"@endpoint_docs on {cls.__name__} names endpoint(s) it does not have: "
            f"{', '.join(sorted(unknown))}; it has {', '.join(sorted(documented))}"
        )

    if muxws_routes:
        register_viewset(cls, base_path, muxws_routes, default_tags)

    cls.__viewset_metadata__ = {
        "base_path": base_path,
        "lifecycle": lifecycle,
        "router": router,
        "pk_field_name": pk_field_name,
        "paths": paths,
        "parent": parent.cls if parent is not None else None,
        "sub_viewsets": {path: mount.viewset for path, mount in mounts.items()},
    }

    return cls


@dataclass(frozen=True)
class _Parent:
    """What routing a sub-viewset takes from the viewset it is mounted under."""

    cls: type
    """The class that mounts the sub-viewset."""
    tags: list[str] | None
    """The OpenAPI tags of the sub-viewset's routes: those of the viewset at the top of its mounts."""
    gates: tuple
    """The `Depends` of the checks of every viewset above the sub-viewset, the outermost first."""
    summary_prefix: str
    """What a default summary of the sub-viewset's routes starts with: `Members`, `Members / Active`."""
    chain: tuple[type, ...]
    """The classes above the sub-viewset, the outermost first."""


def _summary_label(path: str) -> str:
    """`member-rows` -> `Member Rows`."""
    return re.sub(r"[-_.~]+", " ", path).strip().title() or path


def _check_mounts(cls: type, base: str, mounts: dict[str, SubViewSet], chain: tuple[type, ...]) -> None:
    """Raises `ValueError` for a mount clashing with `cls`'s own route, or of a class above it (itself included)."""
    for route in cls.__router.routes:
        relative = route.path[len(base) :] if route.path.startswith(base) else route.path
        first = relative.strip("/").split("/", 1)[0]
        if first in mounts:
            raise ValueError(
                f"{cls.__name__} mounts a sub-viewset at {first!r} and serves {route.path!r} itself; "
                f"one of them has to move"
            )
    for path, mount in mounts.items():
        if mount.viewset in chain:
            raise ValueError(
                f"{cls.__name__} mounts {mount.viewset.__name__} at {path!r}, a viewset it is mounted under"
            )
