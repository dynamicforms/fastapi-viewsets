"""
The benchmark's read-only viewsets over the PostgreSQL table, through the Django alias and the adorm profile `bench`.
Imported only when PostgreSQL is on (`postgres_demo.ENABLED`).

`VIEWSETS` maps `(db_api, executor)` to the class routed at `/bench/{db_api}/{executor}`: the class itself for the
FastAPI executor, a subclass wrapped with `celery_viewset` for the Celery executors (when the benchmark's workers run).
"""

from demo.backend import benchmark, postgres_demo
from demo.backend.benchmark.loadgen import REQUEST_TIMEOUT_S

# demo.backend.viewsets configures Django first, then imports its PostgreSQL model
from demo.backend.viewsets import as_list, MusicTrack, MusicTrackRecord as MusicTrackTable
from fastapi_viewsets.backends.django_orm import DjangoORMViewSet
from fastapi_viewsets.context import Context
from fastapi_viewsets.endpoint_docs import endpoint_docs
from fastapi_viewsets.mixins import CursorListMixin, RetrieveMixin

BENCH_DOCS = {
    "list_items": {"summary": "The first page of tracks", "description": "Cursor-paged; `limit=50` in the benchmark."},
    "retrieve": {"summary": "One track by id"},
}

EXECUTOR_LABELS = {"fastapi": "FastAPI in-process", "classic": "Celery classic", "hybrid": "Celery hybrid"}
DB_API_LABELS = {"django": "Django ORM", "adorm": "adorm"}


@endpoint_docs(BENCH_DOCS)
class BenchDjangoViewSet(
    DjangoORMViewSet[int, MusicTrack],
    CursorListMixin[MusicTrack],
    RetrieveMixin[int, MusicTrack],
):
    """
    Benchmark: the music library on PostgreSQL through the Django ORM.

    Reads through the Django alias `bench` (the latency proxy) with a persistent connection per thread.
    """

    model = MusicTrackTable
    schema = MusicTrack
    default_page_size = 50

    def get_queryset(self, context: Context):  # noqa: ARG002 - the signature of the override
        return self.model.objects.using(postgres_demo.BENCH_ALIAS).all()

    def to_record(self, raw) -> MusicTrack:
        if isinstance(raw, MusicTrack):
            return raw
        return MusicTrack(
            id=raw.id,
            title=raw.title,
            artist=raw.artist,
            year=raw.year,
            duration=raw.duration,
            genres=as_list(raw.genres),
            rating=raw.rating,
            favorite=raw.favorite,
            play_count=raw.play_count,
            moods=as_list(raw.moods),
            language=raw.language,
        )


VIEWSETS: dict[tuple[str, str], type] = {("django", "fastapi"): BenchDjangoViewSet}

if postgres_demo.ADORM_ENABLED:
    from demo.backend.adorm_models.backend import MusicTrackRecord
    from fastapi_viewsets.action_configuration import action_configuration
    from fastapi_viewsets.backends.adorm import AdormSession, AdormViewSet

    @endpoint_docs(BENCH_DOCS)
    @action_configuration({AdormSession: False})
    class BenchAdormViewSet(
        AdormViewSet[int, MusicTrack],
        CursorListMixin[MusicTrack],
        RetrieveMixin[int, MusicTrack],
    ):
        """
        Benchmark: the music library on PostgreSQL through adorm.

        Reads on the adorm profile `bench` (the latency proxy), each query awaited on the profile's pool outside a
        transaction.
        """

        model = MusicTrackRecord
        schema = MusicTrack
        using = postgres_demo.BENCH_ALIAS
        default_page_size = 50

    VIEWSETS["adorm", "fastapi"] = BenchAdormViewSet


def _celery_subclass(base: type, db_api: str, executor: str) -> type:
    """A subclass of `base` whose actions run on the benchmark worker of `executor`."""
    from demo.backend.celery_app import celery_app, redis_sync
    from fastapi_viewsets.decorators.celery_viewset import celery_viewset

    subclass = type(
        f"{base.__name__.removesuffix('ViewSet')}{executor.title()}ViewSet",
        (base,),
        {
            "__module__": __name__,
            "__doc__": (
                f"Benchmark: the music library on PostgreSQL through the {DB_API_LABELS[db_api]}, "
                f"run by the {EXECUTOR_LABELS[executor]} worker."
            ),
        },
    )
    return celery_viewset(
        celery_app=celery_app,
        task_prefix=task_prefix(db_api, executor),
        redis_client=redis_sync,
        # a wait whose client has given up ends soon after, also for a task purged by the next run's drain
        result_timeout=REQUEST_TIMEOUT_S + 5,
    )(subclass)


def task_prefix(db_api: str, executor: str) -> str:
    """The Celery task names of a benchmark viewset: `bench_<db_api>_<executor>.<action>`."""
    return f"bench_{db_api}_{executor}"


if benchmark.CELERY_ENABLED:
    for (_db_api, _), _base in list(VIEWSETS.items()):
        for _executor in benchmark.CELERY_EXECUTORS:
            VIEWSETS[_db_api, _executor] = _celery_subclass(_base, _db_api, _executor)


def unavailable(db_api: str, executor: str) -> str | None:
    """Why `/bench/{db_api}/{executor}` is not routed; None when it is."""
    if (db_api, executor) in VIEWSETS:
        return None
    if db_api == "adorm" and not postgres_demo.ADORM_ENABLED:
        return f"adorm is off: {postgres_demo.ADORM_UNAVAILABLE}"
    return "the benchmark's Celery workers run with demo.py --benchmark"
