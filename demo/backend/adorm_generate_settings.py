"""
Django settings for generating the adorm model of the demo's PostgreSQL table (`demo/backend/adorm_tables.py`):

    adorm generate --pythonpath . --settings demo.backend.adorm_generate_settings --output demo/backend/adorm_models \
        --line-length 120 backend
"""

from django.apps import AppConfig


class AdormTablesConfig(AppConfig):
    """The demo's app with only the PostgreSQL table registered."""

    name = "demo.backend"
    label = "backend"

    def ready(self):
        import demo.backend.adorm_tables  # noqa: F401


SECRET_KEY = "adorm-generate"  # noqa: S105 - not a credential: generating models opens no connection
INSTALLED_APPS = ["demo.backend.adorm_generate_settings.AdormTablesConfig"]
DATABASES = {}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
