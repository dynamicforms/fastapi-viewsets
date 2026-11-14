from importlib.metadata import version

from .error_namespace import ERROR_NAMESPACE, ERROR_NAMESPACE_DJANGO, ERROR_NAMESPACE_PYDANTIC
from .error_responses import FIELD_ERROR_PARAMS

__version__ = version("dynamicforms-fastapi-viewsets")

__all__ = ["ERROR_NAMESPACE", "ERROR_NAMESPACE_DJANGO", "ERROR_NAMESPACE_PYDANTIC", "FIELD_ERROR_PARAMS", "__version__"]
