import warnings

from typing import get_args, get_origin, TypeVar

from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute


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


def build_schema(cls, base_path: str = "", default_tags=None, get_wrapper=None):
    """
    Builds __router (APIRouter with all routes from mixins) and __app (FastAPI instance),
    and attaches them to the class. Idempotent: if already called, returns immediately.
    If get_wrapper is None, only __router is built (no FastAPI app, no endpoint wrapping).
    """
    if hasattr(cls, "__router") and (get_wrapper is None or getattr(cls, "__router_full", False)):
        return

    celery_mode = get_wrapper is None
    # Use a dict so that more-specific classes (visited later in reversed MRO) override
    # routes from less-specific base classes.  This allows e.g. FilterableMixin to
    # replace ListMixin's plain GET "" route with one that carries filter query params.
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
        app = FastAPI()

        def schema():
            return app.openapi()

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

            use_resolved = (
                route.response_model is None or isinstance(route.response_model, TypeVar) or
                (get_origin(route.response_model) and
                 any(isinstance(arg, TypeVar) for arg in get_args(route.response_model)))
            )
            class_router.add_api_route(**route_to_add_api_route_kwargs(
                route,
                path=full_path,
                endpoint=endpoint_wrapper,
                response_model=resolved_response_model if use_resolved else route.response_model,
                tags=route.tags or default_tags,
            ))

        app.include_router(class_router)
        cls.__app = app
    else:
        # celery mode: store raw mixin routes directly, no FastAPI wrapping
        for route in all_routes:
            class_router.routes.append(route)

    cls.__router = class_router
    if not celery_mode:
        cls.__router_full = True


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

    return *path_idx, method_idx
