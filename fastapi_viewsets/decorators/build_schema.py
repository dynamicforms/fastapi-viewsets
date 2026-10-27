import inspect
import warnings

from typing import get_args, TypeVar

from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from starlette.responses import Response

from ..endpoint_docs import docs_for, viewset_tags
from ..middleware import unwrap_viewset_result_type


def _generic_args(annotation) -> tuple:
    """Type arguments of an annotation, from either the typing machinery or pydantic's own."""
    metadata = getattr(annotation, "__pydantic_generic_metadata__", None)
    if metadata and metadata.get("args"):
        return tuple(metadata["args"])
    return get_args(annotation)


def has_typevars(annotation) -> bool:
    """
    Whether an unbound TypeVar remains anywhere inside this annotation, through typing aliases and pydantic
    generics nested in each other (`Union[ListOf[T], PaginatedList[T]]`).
    """
    if isinstance(annotation, TypeVar):
        return True
    return any(has_typevars(argument) for argument in _generic_args(annotation))


def _pydantic_generic_args(annotation) -> tuple[type, tuple] | None:
    """
    (origin, args) for a parameterised pydantic generic that still has TypeVars in it, else None.

    `PaginatedList[T]` is a class, not a typing alias, so get_origin/get_args report nothing; pydantic keeps
    origin and args on the class, and re-subscripting the origin builds the resolved model.
    """
    metadata = getattr(annotation, "__pydantic_generic_metadata__", None)
    if not metadata:
        return None
    origin, args = metadata.get("origin"), metadata.get("args")
    if not origin or not args:
        return None
    if not any(isinstance(arg, TypeVar) for arg in args):
        return None
    return origin, args


def route_to_add_api_route_kwargs(route: APIRoute, **kwargs) -> dict:
    """
    Maps an APIRoute instance to a dict of kwargs suitable for add_api_route().
    Any keyword arguments passed will override the corresponding route attributes.
    """
    result = dict(
        path=route.path,
        endpoint=route.endpoint,
        response_model=route.response_model,
        status_code=route.status_code,
        tags=route.tags,
        dependencies=route.dependencies,
        summary=route.summary,
        description=route.description,
        response_description=route.response_description,
        responses=route.responses,
        deprecated=route.deprecated,
        methods=route.methods,
        operation_id=route.operation_id,
        include_in_schema=route.include_in_schema,
        response_class=route.response_class,
        name=route.name,
        callbacks=route.callbacks,
        openapi_extra=route.openapi_extra,
        generate_unique_id_function=route.generate_unique_id_function,
    )
    unknown_keys = set(kwargs) - set(result)
    if unknown_keys:
        warnings.warn(
            f"route_to_add_api_route_kwargs() received unknown kwargs: {unknown_keys}",
            stacklevel=2,
        )
    result.update(kwargs)
    return result


ACTION_EXTENSION = "x-viewset-action"
"""
The OpenAPI extension naming the viewset method serving an operation (`top_rated` for `GET /tracks/top-rated`),
by which a client matches a custom endpoint; the path may hold parameters or differ from the method name.
"""


def action_openapi_extra(action_name: str, openapi_extra: dict | None) -> dict:
    """`openapi_extra` with the operation's `x-viewset-action` set to `action_name`."""
    return {**(openapi_extra or {}), ACTION_EXTENSION: action_name}


def build_schema(cls, base_path: str = "", default_tags=None, get_wrapper=None, disable_response_model=False):
    """
    Builds `__router` (all mixin routes) and `__app` (the FastAPI app serving `/schema`) on the class.

    Without `get_wrapper` only `__router` is built, unwrapped, and only once per class; with it the routes are
    rebuilt per routing. `disable_response_model`: set when a middleware declares `modifies_response_shape`,
    so FastAPI does not coerce the reshaped body back to the declared return type.
    """
    own = vars(cls)
    if "__router" in own and get_wrapper is None:
        return

    celery_mode = get_wrapper is None
    # Keyed by (methods, path): a more specific class, visited later in reversed MRO, replaces a base's route.
    route_registry: dict[tuple, APIRoute] = {}

    for base in reversed(cls.__mro__):
        mangled_name = f"_{base.__name__}__router"
        mixin_router = getattr(base, mangled_name, None)
        if mixin_router and isinstance(mixin_router, APIRouter):
            for route in mixin_router.routes:
                if not isinstance(route, APIRoute):
                    continue

                full_path = f"{base_path.rstrip('/')}/{route.path.lstrip('/')}".rstrip("/")
                if not full_path:
                    full_path = "/"

                route_key = (tuple(sorted(route.methods)), full_path)
                route_registry[route_key] = route  # last (most specific) wins

    all_routes = list(route_registry.values())

    class_router = APIRouter()

    if not celery_mode:
        app = FastAPI(openapi_tags=viewset_tags())

        def schema():
            return app.openapi()

        # route_viewset replaces this /schema route with one per transport.
        schema.__viewset_schema_endpoint__ = True

        schema_route_kwargs = dict(
            path=f"{base_path.rstrip('/')}/schema",
            endpoint=schema,
            tags=default_tags,
            methods=["GET"],
            description="Returns OpenAPI schema for the ViewSet",
        )
        class_router.add_api_route(**schema_route_kwargs)

        for route in sorted(all_routes, key=route_sort_key, reverse=True):
            full_path = f"{base_path.rstrip('/')}/{route.path.lstrip('/')}".rstrip("/")
            if not full_path:
                full_path = "/"

            endpoint_wrapper, resolved_response_model = get_wrapper(route.endpoint, route.path, route.methods)

            if disable_response_model:
                response_model = None
            else:
                # A model still holding a TypeVar is the mixin's generic one; use what route_viewset resolved.
                use_resolved = route.response_model is None or has_typevars(route.response_model)
                response_model = resolved_response_model if use_resolved else route.response_model
                # route.response_model is inferred from the original annotation, so unwrap ViewSetResult[X] too.
                response_model = unwrap_viewset_result_type(response_model)
                # A Response return type is served as is, as FastAPI does.
                if inspect.isclass(response_model) and issubclass(response_model, Response):
                    response_model = None

            # Per-viewset wording applied here, so that /schema shows it too.
            class_router.add_api_route(
                **route_to_add_api_route_kwargs(
                    route,
                    path=full_path,
                    endpoint=endpoint_wrapper,
                    response_model=response_model,
                    tags=route.tags or default_tags,
                    openapi_extra=action_openapi_extra(
                        route.endpoint.__name__,
                        getattr(endpoint_wrapper, "__shape_openapi_extra__", None) or route.openapi_extra,
                    ),
                    **docs_for(cls, route.endpoint.__name__),
                )
            )

        app.include_router(class_router)
        cls.__app = app
    else:
        # celery mode: store raw mixin routes directly, no FastAPI wrapping
        for route in all_routes:
            class_router.routes.append(route)

    cls.__router = class_router


def route_sort_key(route: APIRoute):
    path = route.path.strip("/")
    methods = route.methods or set()

    method_order = ["GET", "POST", "PUT", "PATCH", "DELETE"]
    method_idx = min((method_order.index(m) for m in methods if m in method_order), default=10)

    def segment_key(segment: str):
        if segment == "":
            return 0, ""
        elif segment.startswith("{"):
            return 1, segment
        else:
            return 2, segment

    path_idx = tuple(segment_key(s) for s in path.split("/"))

    # Not flattened into `(*path_idx, method_idx)`: paths of different depth would then compare an int
    # against a segment tuple and raise TypeError.
    return path_idx, method_idx
