# fastapi-viewsets demo

The demo starts in the terminal below once the codespace is ready, and opens in a new browser tab.

- Tab did not open: **Ports** panel → port 5173 → **Open in Browser**.
- Demo not running: run `uv run python demo.py --benchmark` in the terminal.
- Stop it: Ctrl-C in the terminal where it runs.

The codespace runs PostgreSQL 17 and Valkey next to the demo, so every data source and the **Benchmark** tab are on:
the Django ORM and adorm on PostgreSQL, the benchmark's FastAPI, classic Celery and hybrid Celery executors. The
benchmark viewsets run in 2 uvicorn processes (`--bench-processes`), and the benchmark's load generator, in 4
processes, shares the codespace's CPUs with them.

Celery mode (the in-memory and Django ORM data sources' viewset calls through a Celery worker): stop the demo with
Ctrl-C, then run `uv run python demo.py --benchmark --celery`.
