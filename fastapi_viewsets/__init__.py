from importlib.metadata import version

from .error_namespace import ERROR_NAMESPACE, ERROR_NAMESPACE_PYDANTIC

__version__ = version("dynamicforms-fastapi-viewsets")

__all__ = ["ERROR_NAMESPACE", "ERROR_NAMESPACE_PYDANTIC", "__version__"]
