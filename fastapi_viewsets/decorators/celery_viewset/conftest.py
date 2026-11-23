import pytest

WORKER_LOG_LINES = 300


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item):
    """Adds the tail of the Celery worker's log to the report of a failed test that uses the `worker` fixture."""
    report = yield
    worker = item.funcargs.get("worker") if hasattr(item, "funcargs") else None
    log_path = getattr(worker, "log_path", None)
    if report.failed and log_path is not None and log_path.exists():
        lines = log_path.read_text(errors="replace").splitlines()[-WORKER_LOG_LINES:]
        report.sections.append((f"Celery worker log (last {WORKER_LOG_LINES} lines)", "\n".join(lines)))
    return report
