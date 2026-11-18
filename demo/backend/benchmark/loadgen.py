"""
The benchmark's load generator, a process of its own: a closed loop of `--concurrency` virtual users, each sending its
next request when the previous one answers, for `--duration` seconds after a warm-up that is not measured; then it
waits for the requests in flight. Prints the result as JSON.

    python -m demo.backend.benchmark.loadgen --base-url http://127.0.0.1:8000 --path /bench/adorm/fastapi \
        --transport rest --operation retrieve --concurrency 100 --duration 5 --ids 5000

REST uses one httpx connection per virtual user; muxws sends every virtual user's commands over one WebSocket.
"""

import argparse
import asyncio
import json
import math
import resource
import time

from collections import Counter
from datetime import datetime, timezone

WARMUP_S = 1.0
REQUEST_TIMEOUT_S = 10.0
LIST_PAGE_SIZE = 50


def request_target(operation: str, path: str, ids: int, user: int, sequence: int) -> tuple[str, dict[str, str]]:
    """The path and query of a virtual user's request: a deterministic id spread over the library, or the first page."""
    if operation == "list":
        return path, {"limit": str(LIST_PAGE_SIZE)}
    pk = (user * 7919 + sequence * 104_729) % ids + 1
    return f"{path}/{pk}", {}


class Recorder:
    """
    Outcomes and latencies of the requests started within the measured window, those answered or failed after its end
    too; and the answers within the window, for the throughput.
    """

    def __init__(self):
        self.latencies: list[float] = []
        self.errors: Counter[str] = Counter()
        self.answered_in_window = 0
        self.window_start = 0.0
        self.window_end = 0.0

    def record(self, started: float, finished: float, error: str | None) -> None:
        if error is None and self.window_start <= finished < self.window_end:
            self.answered_in_window += 1
        if not self.window_start <= started < self.window_end:
            return
        if error is not None:
            self.errors[error] += 1
        else:
            self.latencies.append((finished - started) * 1000)


async def run(args: argparse.Namespace) -> dict:
    started_at = datetime.now(timezone.utc)
    cpu_start = time.process_time()
    recorder = Recorder()
    # perf_counter, not loop.time(): uvloop's clock has a resolution of 1 ms
    clock = time.perf_counter
    stop_at = clock() + WARMUP_S + args.duration
    recorder.window_start = clock() + WARMUP_S
    recorder.window_end = stop_at

    async with _client(args) as send:

        async def user(index: int) -> None:
            sequence = 0
            while clock() < stop_at:
                path, query = request_target(args.operation, args.path, args.ids, index, sequence)
                sequence += 1
                started = clock()
                error = await send(path, query)
                recorder.record(started, clock(), error)

        await asyncio.gather(*(user(index) for index in range(args.concurrency)))
    finished = clock()

    latencies = sorted(recorder.latencies)
    completed = len(latencies)
    errors = sum(recorder.errors.values())
    measured = args.duration
    return {
        "completed": completed,
        "errors": errors,
        "error_rate": errors / (completed + errors) if completed + errors else 0.0,
        "error_kinds": dict(recorder.errors.most_common()),
        "throughput": recorder.answered_in_window / measured,
        "latency_ms": {
            "p50": _percentile(latencies, 50),
            "p95": _percentile(latencies, 95),
            "p99": _percentile(latencies, 99),
            "mean": sum(latencies) / completed if completed else None,
            "max": latencies[-1] if latencies else None,
        },
        "measured_s": measured,
        "drain_s": max(0.0, finished - stop_at),
        "loadgen_cpu_s": time.process_time() - cpu_start,
        "started": started_at.isoformat(),
        "finished": datetime.now(timezone.utc).isoformat(),
    }


def _percentile(ordered: list[float], percent: float) -> float | None:
    """The nearest-rank percentile of an ordered list; None for an empty one."""
    if not ordered:
        return None
    rank = max(1, math.ceil(percent / 100 * len(ordered)))
    return ordered[rank - 1]


class _client:  # noqa: N801 - used as a function
    """An async context manager yielding `send(path, query) -> error kind | None` over the selected transport."""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self._close = None

    async def __aenter__(self):
        if self.args.transport == "rest":
            return await self._rest()
        return await self._muxws()

    async def __aexit__(self, *_exc):
        if self._close is not None:
            await self._close()

    async def _rest(self):
        import httpx2 as httpx

        concurrency = self.args.concurrency
        client = httpx.AsyncClient(
            base_url=self.args.base_url,
            timeout=REQUEST_TIMEOUT_S,
            limits=httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency),
        )
        self._close = client.aclose

        async def send(path: str, query: dict[str, str]) -> str | None:
            try:
                response = await client.get(path, params=query)
                await response.aread()
            except httpx.TimeoutException:
                return "timeout"
            except Exception as error:  # noqa: BLE001 - every failure is a result
                return type(error).__name__
            return None if response.status_code < 400 else f"HTTP {response.status_code}"

        return send

    async def _muxws(self):
        from muxws import connect, StreamTimeout

        url = "ws" + self.args.base_url.removeprefix("http") + "/ws"
        peer = await connect(url, reconnect=None)

        async def close() -> None:
            await peer.close(drain=1.0)

        self._close = close

        async def send(path: str, query: dict[str, str]) -> str | None:
            stream = peer.open(None, headers={":method": "GET", ":path": path, ":query": query}, end=True)
            try:
                await asyncio.wait_for(_answer(stream), REQUEST_TIMEOUT_S)
            except (asyncio.TimeoutError, StreamTimeout):
                await stream.cancel()
                return "timeout"
            except Exception as error:  # noqa: BLE001 - every failure is a result
                return type(error).__name__
            status = stream.reply_headers.get(":status", 200)
            return None if status < 400 else f"HTTP {status}"

        return send


async def _answer(stream) -> None:
    async for _payload in stream:
        pass


def raise_open_files_limit() -> None:
    """Raises the soft limit of open files to the hard limit."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft < hard:
        resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", required=True, help="http://host:port of the demo's API")
    parser.add_argument("--path", required=True, help="the viewset's base path, e.g. /bench/adorm/fastapi")
    parser.add_argument("--transport", choices=("rest", "muxws"), default="rest")
    parser.add_argument("--operation", choices=("retrieve", "list"), default="retrieve")
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--duration", type=float, default=5.0, help="measured seconds, after a 1 s warm-up")
    parser.add_argument("--ids", type=int, default=5000, help="retrieve ids 1..ids")
    args = parser.parse_args()
    raise_open_files_limit()
    try:
        import uvloop
    except ImportError:
        runner = asyncio.run
    else:
        runner = uvloop.run
    print(json.dumps(runner(run(args))))


if __name__ == "__main__":
    main()
