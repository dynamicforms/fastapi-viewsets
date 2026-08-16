import inspect

from functools import wraps
from typing import Annotated, get_args, get_origin, Literal, TypeVar

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.params import FieldInfo
from pydantic import BaseModel

try:
    from typing_extensions import NoDefault as _TypeVarNoDefault
except ImportError:
    _TypeVarNoDefault = object()  # fallback sentinel — never equal to any real TypeVar default

from fastapi_viewsets.action_configuration import extra_middlewares_for, resolve_action_configuration
from fastapi_viewsets.conf import settings
from fastapi_viewsets.context import get_shared_context
from fastapi_viewsets.endpoint_docs import (
    DOCS_ATTRIBUTE,
    docs_for,
    register_tag,
    viewset_description,
)
from fastapi_viewsets.middleware import Middleware
from fastapi_viewsets.mixins import FilterParam
from fastapi_viewsets.mux_ws import register_viewset, resolve_register_muxws, resolve_register_rest

from .build_schema import _pydantic_generic_args, build_schema, route_to_add_api_route_kwargs
from .lifecycle_runner import lifecycle_runner, LifecycleType
from .primary_key_model_helper import create_model_without_pk, typecast_to_original_model

T = TypeVar("T")


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

        # Update local mapping for this origin
        type_map.update(new_map)
        # Recursively search parents of this origin and add results to our mapping
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
        # A parameterised pydantic model (PaginatedList[T]) is a real class, not a typing alias, so
        # get_origin/get_args see nothing and the branch below would hand back the annotation with
        # its TypeVars still in it - which FastAPI then turns into a response model full of Any.
        # Pydantic keeps the origin and args on the class itself; re-subscripting the origin is how
        # a resolved model gets built.
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
            # To deluje za List, Dict, Union, itd.
            try:
                return origin[new_args]
            except:
                return annotation
    return annotation


def _make_middleware_depends(cls: type, action_name: str):
    """
    Builds a per-route FastAPI dependency that bridges Middleware.depends() (see
    fastapi_viewsets/middleware/__init__.py) onto FastAPI's own Depends() resolution - runs before
    FastAPI even parses the request body. `cls`/`action_name` are captured once, at route
    registration time; actual @action_configuration resolution stays fully dynamic; settings can
    still change at runtime between requests.
    """
    async def _run(request: Request) -> None:
        action_configuration = resolve_action_configuration(cls, action_name)
        effective_middlewares = settings.viewsets_command_middleware + extra_middlewares_for(
            action_configuration, settings.viewsets_command_middleware
        )
        context = await get_shared_context(request, cls, action_configuration, action_name)
        for middleware in effective_middlewares:
            # Plain-function CommandMiddleware entries have no depends() to bridge - they only
            # ever run in the onion chain, unchanged.
            if isinstance(middleware, Middleware):
                await middleware.depends(request, cls, context)

    scheme = settings.viewsets_security_scheme
    if scheme is None:
        async def middleware_depends(request: Request) -> None:
            await _run(request)
        return middleware_depends

    async def middleware_depends(request: Request, _credential=Depends(scheme)) -> None:  # noqa: B008
        await _run(request)
    return middleware_depends


def route_viewset(
        router: APIRouter,
        base_path: str,
        lifecycle: LifecycleType = "singleton",
        pk_field_name: str = None,
        register_muxws: bool = None,
        register_rest: bool = None,
):
    """
    `register_rest` and `register_muxws` decide which transports this viewset is published on.
    True registers, False does not, and None - the default - defers: REST to "yes", muxws to
    `settings.viewsets_register_muxws`. Individual endpoints override either again with
    `@transports(...)`.

    Two parameters rather than one because there are two transports. If a third ever arrives, the
    replacement is a single mapping - `transports={"rest": False}`, absent key meaning defer -
    rather than a third `register_*`; see GAPS.md for why a set of flags is the worse shape.
    """
    def decorator(cls: type[T]):
        instance = cls() if lifecycle == "singleton" else None

        type_map = build_type_map(cls)

        # Derive tag from class name: strip "ViewSet" suffix if present
        cls_name = cls.__name__
        if cls_name.endswith("ViewSet"):
            cls_name = cls_name[:-len("ViewSet")]
        default_tags = [cls_name] if cls_name else None
        # The viewset's own docstring becomes the description of the group its endpoints appear
        # under - the section intro that was otherwise blank however well each endpoint was
        # documented. See endpoint_docs.apply_viewset_tags.
        if cls_name:
            register_tag(cls_name, viewset_description(cls))

        def _is_filter_param(annotation) -> bool:
            """Return True when annotation is Annotated[T, FilterParam()]."""
            return (
                hasattr(annotation, "__metadata__")
                and any(isinstance(m, FilterParam) for m in annotation.__metadata__)
            )

        def _is_model_query_param(annotation) -> bool:
            """Return True for Annotated[PydanticModel, <FieldInfo>] (e.g. Query() or Depends()).
            These should use Depends() in the wrapper so FastAPI expands model fields as
            individual query params even when other parameters are present on the same endpoint."""
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
            Trims the shape-aware list endpoint down to this viewset's own shapes.

            `ListMixin.list_items` declares every parameter any shape could need, because a method
            on a shared mixin cannot know which viewset it will end up on. Here it can: parameters
            no allowed shape uses are dropped so they never reach the schema, the `X-List-Shape`
            header is kept only when there is a choice to make, and the return annotation becomes
            the union of exactly the models this viewset can produce.
            """
            from fastapi_viewsets.list_shapes import response_model, shape_examples, SHAPE_HEADER
            from fastapi_viewsets.mixins import T as ItemVar

            default, allowed = cls.resolve_shapes()
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
                    parameter = parameter.replace(annotation=Annotated[
                        Literal[(*allowed, None)],
                        Header(
                            alias=SHAPE_HEADER,
                            description=f"Response shape. Omit for this endpoint's default ({default}).",
                        ),
                    ])
                params.append(parameter)

            item_type = type_map.get(ItemVar, ItemVar)
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

            for name, p in sig.parameters.items():
                if name == "self":
                    continue

                annotation = resolve_typevars(type_map, p.annotation)
                new_p = p.replace(annotation=annotation)

                # context is built centrally (see fastapi_viewsets/context.py) from a live Request -
                # it must never reach FastAPI's own signature/validation, so it's dropped here and
                # rebuilt by lifecycle_runner right before the endpoint runs. Detected by parameter
                # name (by convention) rather than type, since its type is just a plain dict.
                if name == "context":
                    needs_context = True
                    continue

                # Transform Annotated[T, FilterParam()] → Annotated[TFilter, Query()]
                if _is_filter_param(annotation):
                    inner_type = annotation.__args__[0]
                    if inspect.isclass(inner_type) and issubclass(inner_type, BaseModel):
                        # optional_model = make_all_optional(inner_type)
                        new_p = new_p.replace(annotation=Annotated[inner_type, Query()])
                    new_params.append(new_p)
                    continue

                # Convert Annotated[PydanticModel, FieldInfo] → Annotated[PydanticModel, Depends()]
                # so that FastAPI expands model fields as individual query params even when
                # other parameters (e.g. sort) are present on the same endpoint.
                if _is_model_query_param(annotation):
                    inner_type = annotation.__args__[0]
                    new_p = new_p.replace(annotation=Annotated[inner_type, Depends()])
                    new_params.append(new_p)
                    continue

                # If we have a PK field and this parameter is a model from which we want to exclude PK
                if (pk_field_name and
                    inspect.isclass(annotation) and
                    issubclass(annotation, BaseModel)
                ):
                    # Check if {pk} is in the path and if the method is one that normally accepts a model in the body
                    # (POST without {pk} or PUT/PATCH with {pk})
                    is_create = "POST" in route_methods and "{pk}" not in route_path
                    is_update = False  # ("PUT" in route_methods or "PATCH" in route_methods) and "{pk}" in route_path

                    if (is_create or is_update) and pk_field_name in annotation.model_fields:
                        new_model = create_model_without_pk(annotation, pk_field_name)
                        new_p = new_p.replace(annotation=new_model)

                new_params.append(new_p)

            # Reserved param name - FastAPI injects the REAL Response for this connection here
            # (never forwarded to the underlying method itself, see wrapper below). Keyword-only
            # so it can be appended regardless of what defaults precede it. Used by lifecycle_runner
            # to run the command middleware chain and apply headers/cookies onto the real response.
            new_params.append(
                inspect.Parameter("response", inspect.Parameter.KEYWORD_ONLY, annotation=Response)
            )

            if needs_context:
                # Reserved param name, only added for endpoints that declare a `context` param.
                # FastAPI injects the REAL Request here; lifecycle_runner uses it to build the
                # context (see fastapi_viewsets/context.py) and injects the result as `context`
                # right before the endpoint runs - never forwarded to the endpoint itself.
                new_params.append(
                    inspect.Parameter("request", inspect.Parameter.KEYWORD_ONLY, annotation=Request)
                )

            new_return_annotation = resolve_typevars(type_map, sig.return_annotation)
            new_sig = sig.replace(parameters=new_params, return_annotation=new_return_annotation)

            @wraps(original_endpoint)
            async def wrapper(*args, **kwargs):
                nonlocal instance

                response_obj = kwargs.pop("response", None)
                request_obj = kwargs.pop("request", None) if needs_context else None

                # Check if any argument needs to be "typecasted" back to the original model
                # This happens if we replaced the model type with a NoPK version in get_wrapper
                new_kwargs = {}
                for param_name, value in kwargs.items():
                    if param_name in sig.parameters:
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
                    original_endpoint, instance, cls, lifecycle, *args, response=response_obj,
                    needs_context=needs_context, request=request_obj, **new_kwargs
                )

            wrapper.__signature__ = new_sig
            # Carried on the wrapper because build_schema, which registers the route, has no other
            # way to learn what get_wrapper worked out about this endpoint's shapes.
            wrapper.__shape_openapi_extra__ = shape_openapi_extra
            return wrapper, new_return_annotation

        # Command middleware (see fastapi_viewsets/middleware.py) can reshape the response body via
        # ViewSetResult - if any is configured, the endpoint's declared return type can no longer
        # be trusted as the actual response_model, same reasoning as the old finalize_response hook.
        build_schema(
            cls, base_path, default_tags, get_wrapper,
            disable_response_model=bool(settings.viewsets_command_middleware),
        )

        muxws_routes = []
        documented = set()
        for route in cls.__router.routes:
            action_name = route.endpoint.__name__  # survives @wraps(original_endpoint) in get_wrapper
            documented.add(action_name)
            dependencies = list(route.dependencies or []) + [
                Depends(_make_middleware_depends(cls, action_name))
            ]
            route_kwargs = route_to_add_api_route_kwargs(route, dependencies=dependencies)
            # Per-viewset wording for an endpoint the mixin provided - the mixin's own docstring is
            # the same sentence on every viewset in the application. See endpoint_docs.py.
            route_kwargs.update(docs_for(cls, action_name))

            if resolve_register_rest(route.endpoint, register_rest):
                router.add_api_route(**route_kwargs)

            # The /schema route is deliberately not carried over: it closes over the REST app and
            # would report the REST endpoint set no matter which transport asked. The muxws
            # registry substitutes its own.
            is_schema_route = getattr(route.endpoint, "__viewset_schema_endpoint__", False)
            if not is_schema_route and resolve_register_muxws(route.endpoint, register_muxws):
                muxws_routes.append(route_kwargs)

        # Checked here rather than in the decorator, which runs before any route exists. A name
        # that matches nothing would otherwise leave that endpoint on the mixin's generic
        # docstring - indistinguishable from never having written the entry.
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
            "router": router
        }

        return cls

    return decorator
