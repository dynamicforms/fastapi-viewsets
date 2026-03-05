import inspect

from functools import wraps
from typing import Annotated, get_args, get_origin, TypeVar

from fastapi import APIRouter, Depends, Query
from fastapi.params import FieldInfo
from pydantic import BaseModel

try:
    from typing_extensions import NoDefault as _TypeVarNoDefault
except ImportError:
    _TypeVarNoDefault = object()  # fallback sentinel — never equal to any real TypeVar default

from fastapi_viewsets.mixins import FilterParam, make_all_optional

from .build_schema import build_schema, route_to_add_api_route_kwargs
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


def route_viewset(
        router: APIRouter,
        base_path: str,
        lifecycle: LifecycleType = "singleton",
        pk_field_name: str = None,
):
    def decorator(cls: type[T]):
        seen_routes = set()
        instance = cls() if lifecycle == "singleton" else None

        type_map = build_type_map(cls)

        # Derive tag from class name: strip "ViewSet" suffix if present
        cls_name = cls.__name__
        if cls_name.endswith("ViewSet"):
            cls_name = cls_name[:-len("ViewSet")]
        default_tags = [cls_name] if cls_name else None

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

        def get_wrapper(original_endpoint, route_path, route_methods):
            sig = inspect.signature(original_endpoint)
            new_params = []

            for name, p in sig.parameters.items():
                if name == "self":
                    continue

                annotation = resolve_typevars(type_map, p.annotation)
                new_p = p.replace(annotation=annotation)

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

            new_return_annotation = resolve_typevars(type_map, sig.return_annotation)
            new_sig = sig.replace(parameters=new_params, return_annotation=new_return_annotation)

            @wraps(original_endpoint)
            async def wrapper(*args, **kwargs):
                nonlocal instance

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

                return await lifecycle_runner(original_endpoint, instance, cls, lifecycle, *args, **new_kwargs)

            wrapper.__signature__ = new_sig
            return wrapper, new_return_annotation

        build_schema(cls, base_path, default_tags, get_wrapper)

        for route in cls.__router.routes:
            router.add_api_route(**route_to_add_api_route_kwargs(route))

        cls.__viewset_metadata__ = {
            "base_path": base_path,
            "lifecycle": lifecycle,
            "router": router
        }

        return cls

    return decorator
