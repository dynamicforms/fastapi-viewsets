"""
The benchmark's load generator: a closed loop of `--concurrency` virtual users, each sending its next request when the
previous one answers, for `--duration` seconds after a warm-up that is not measured; then it waits for the requests in
flight. Prints the result as JSON.

    python -m demo.backend.benchmark.loadgen --base-url http://127.0.0.1:8010,http://127.0.0.1:8011 \
        --path /bench/adorm/fastapi --transport rest --operation retrieve --concurrency 100 --duration 5 --ids 5000

The virtual users are split over `--processes` child processes; child `i` sends to base URL `i mod N`. All children
start the warm-up and the measured window at the same instant, and the parent merges their results. REST uses one
httpx connection per virtual user; muxws sends a child's commands over one WebSocket.
"""

import argparse
import asyncio
import json
import math
import resource
import sys
import time

from collections import Counter
from datetime import datetime, timezone

MODULE = "demo.backend.benchmark.loadgen"
WARMUP_S = 1.0
READY_TIMEOUT_S = 30.0
"""Seconds the load generator processes have to connect."""
START_DELAY_S = 0.2
"""Seconds between the last process being ready and the start of the warm-up."""
READY = "ready"
RESULT_TIMEOUT_S = 10.0
"""Seconds the load generator processes have, after their last request, to report their results."""
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


def split_users(concurrency: int, processes: int) -> list[tuple[int, int]]:
    """`(first user index, users)` per process, as even as possible; no process without users."""
    shares = []
    first = 0
    for index in range(min(processes, concurrency)):
        users = concurrency // processes + (1 if index < concurrency % processes else 0)
        shares.append((first, users))
        first += users
    return shares


def base_urls(values: list[str]) -> list[str]:
    """The base URLs of repeated and comma-separated `--base-url` values, without trailing slashes."""
    return [url.strip().rstrip("/") for value in values for url in value.split(",") if url.strip()]


async def run(args: argparse.Namespace) -> dict:
    """Runs the load in `--processes` child processes, starting them at one instant, and merges their results."""
    started_at = datetime.now(timezone.utc)
    urls = base_urls(args.base_url)
    children = []
    try:
        for index, (first_user, users) in enumerate(split_users(args.concurrency, args.processes)):
            children.append(
                await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    MODULE,
                    "--child",
                    f"--base-url={urls[index % len(urls)]}",
                    f"--path={args.path}",
                    f"--transport={args.transport}",
                    f"--operation={args.operation}",
                    f"--concurrency={users}",
                    f"--first-user={first_user}",
                    f"--duration={args.duration:g}",
                    f"--ids={args.ids}",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                )
            )
        ready = await asyncio.wait_for(
            asyncio.gather(*(child.stdout.readline() for child in children)), READY_TIMEOUT_S
        )
        if any(line.strip() != READY.encode() for line in ready):
            raise SystemExit("a load generator process exited before it was ready")
        start = time.time() + START_DELAY_S
        for child in children:
            child.stdin.write(f"{start!r}\n".encode())
            await child.stdin.drain()
        outputs = await asyncio.wait_for(
            asyncio.gather(*(child.communicate() for child in children)),
            START_DELAY_S + WARMUP_S + args.duration + REQUEST_TIMEOUT_S + RESULT_TIMEOUT_S,
        )
    finally:
        for child in children:
            if child.returncode is None:
                child.kill()
                await child.wait()
    for child in children:
        if child.returncode != 0:
            raise SystemExit(f"a load generator process exited with status {child.returncode}")
    parts = [json.loads(stdout) for stdout, _stderr in outputs]
    return _merge(parts, args.duration, started_at)


def _merge(parts: list[dict], measured: float, started_at: datetime) -> dict:
    """The result of the whole load from the children's raw results."""
    latencies = sorted(latency for part in parts for latency in part["latencies"])
    error_kinds: Counter[str] = Counter()
    for part in parts:
        error_kinds.update(part["errors"])
    completed = len(latencies)
    errors = sum(error_kinds.values())
    cpu = [part["cpu_s"] for part in parts]
    return {
        "completed": completed,
        "errors": errors,
        "error_rate": errors / (completed + errors) if completed + errors else 0.0,
        "error_kinds": dict(error_kinds.most_common()),
        "throughput": sum(part["answered_in_window"] for part in parts) / measured,
        "latency_ms": {
            "p50": _percentile(latencies, 50),
            "p95": _percentile(latencies, 95),
            "p99": _percentile(latencies, 99),
            "mean": sum(latencies) / completed if completed else None,
            "max": latencies[-1] if latencies else None,
        },
        "measured_s": measured,
        "drain_s": max((part["drain_s"] for part in parts), default=0.0),
        "loadgen_cpu_s": sum(cpu),
        "loadgen_cpu_max_s": max(cpu, default=0.0),
        "loadgen_active_s": max((part["active_s"] for part in parts), default=0.0),
        "loadgen_processes": len(parts),
        "started": started_at.isoformat(),
        "finished": datetime.now(timezone.utc).isoformat(),
    }


async def run_child(args: argparse.Namespace) -> dict:
    """
    One load generator process: connects, prints `ready`, reads the warm-up's start (Unix time) from stdin, runs its
    virtual users and returns its raw result. CPU and wall seconds count from the start of the warm-up.
    """
    recorder = Recorder()
    # perf_counter, not loop.time(): uvloop's clock has a resolution of 1 ms
    clock = time.perf_counter

    async with _client(args) as send:
        print(READY, flush=True)
        line = await asyncio.to_thread(sys.stdin.readline)
        begin = clock() + float(line) - time.time()
        await asyncio.sleep(max(0.0, begin - clock()))
        cpu_start = time.process_time()
        recorder.window_start = begin + WARMUP_S
        recorder.window_end = stop_at = recorder.window_start + args.duration

        async def user(index: int) -> None:
            sequence = 0
            while clock() < stop_at:
                path, query = request_target(args.operation, args.path, args.ids, index, sequence)
                sequence += 1
                started = clock()
                error = await send(path, query)
                recorder.record(started, clock(), error)

        await asyncio.gather(*(user(args.first_user + index) for index in range(args.concurrency)))
    finished = clock()
    return {
        "latencies": [round(latency, 4) for latency in recorder.latencies],
        "errors": dict(recorder.errors),
        "answered_in_window": recorder.answered_in_window,
        "drain_s": max(0.0, finished - stop_at),
        "cpu_s": time.process_time() - cpu_start,
        "active_s": finished - begin,
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
            base_url=self.args.base_url[0],
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

        url = "ws" + self.args.base_url[0].removeprefix("http") + "/ws"
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
    parser.add_argument(
        "--base-url",
        required=True,
        action="append",
        help="http://host:port of a benchmark process; repeatable or comma-separated, used round-robin per process",
    )
    parser.add_argument("--path", required=True, help="the viewset's base path, e.g. /bench/adorm/fastapi")
    parser.add_argument("--transport", choices=("rest", "muxws"), default="rest")
    parser.add_argument("--operation", choices=("retrieve", "list"), default="retrieve")
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--duration", type=float, default=5.0, help="measured seconds, after a 1 s warm-up")
    parser.add_argument("--ids", type=int, default=5000, help="retrieve ids 1..ids")
    parser.add_argument("--processes", type=int, default=4, help="processes the virtual users are split over")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--first-user", type=int, default=0, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.processes < 1 or args.concurrency < 1:
        parser.error("--processes and --concurrency must be at least 1")
    if not args.child:
        print(json.dumps(asyncio.run(run(args))))
        return
    raise_open_files_limit()
    try:
        import uvloop
    except ImportError:
        runner = asyncio.run
    else:
        runner = uvloop.run
    print(json.dumps(runner(run_child(args))))


if __name__ == "__main__":
    main()
