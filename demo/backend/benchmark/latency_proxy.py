"""
A TCP proxy in front of PostgreSQL that adds a round-trip time to every connection through it, standing in for a remote
database or slow queries. Every chunk from the server to the client is delayed by the latency current when it arrives,
in order per connection; the client's chunks pass at once.

    python -m demo.backend.benchmark.latency_proxy --listen 55433 --upstream 127.0.0.1:5432 --control 55434 --latency 10

The control port answers one line per command line: `GET` → the latency in ms, `SET <ms>` → the new latency in ms,
anything else → `ERR <reason>`.
"""

import argparse
import asyncio
import contextlib
import resource

from collections import deque

CHUNK = 65536


class LatencyProxy:
    """Forwards connections from `listen_port` to `upstream`, delaying the server's data by `latency_ms`."""

    def __init__(self, upstream_host: str, upstream_port: int, latency_ms: float):
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self.latency_ms = latency_ms
        self.connections = 0

    async def handle_client(self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
        try:
            server_reader, server_writer = await asyncio.open_connection(self.upstream_host, self.upstream_port)
        except OSError:
            client_writer.close()
            return
        self.connections += 1
        try:
            to_server = asyncio.create_task(_pipe(client_reader, server_writer))
            to_client = asyncio.create_task(self._delayed_pipe(server_reader, client_writer))
            await asyncio.wait((to_server, to_client), return_when=asyncio.FIRST_COMPLETED)
            for task in (to_server, to_client):
                task.cancel()
            await asyncio.gather(to_server, to_client, return_exceptions=True)
        finally:
            self.connections -= 1
            for writer in (client_writer, server_writer):
                writer.close()

    async def _delayed_pipe(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Copies `reader` to `writer`, each chunk at its arrival time plus the latency, in arrival order."""
        loop = asyncio.get_running_loop()
        pending: deque[tuple[float, bytes]] = deque()
        arrived = asyncio.Event()
        closed = False

        async def receive() -> None:
            nonlocal closed
            try:
                while data := await reader.read(CHUNK):
                    pending.append((loop.time() + self.latency_ms / 1000, data))
                    arrived.set()
            finally:
                closed = True
                arrived.set()

        receiver = asyncio.create_task(receive())
        try:
            while True:
                if not pending:
                    if closed:
                        return
                    arrived.clear()
                    await arrived.wait()
                    continue
                due, data = pending.popleft()
                delay = due - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                writer.write(data)
                await writer.drain()
        finally:
            receiver.cancel()
            with contextlib.suppress(asyncio.CancelledError, OSError):
                await receiver

    async def handle_control(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while line := await reader.readline():
                writer.write((self.command(line.decode(errors="replace").strip()) + "\n").encode())
                await writer.drain()
        except OSError:
            pass
        finally:
            writer.close()

    def command(self, line: str) -> str:
        """The answer to one control line."""
        verb, _, argument = line.partition(" ")
        verb = verb.upper()
        if verb == "GET":
            return _format(self.latency_ms)
        if verb == "SET":
            try:
                value = float(argument)
            except ValueError:
                return f"ERR not a number: {argument!r}"
            if not 0 <= value <= 10_000:
                return "ERR the latency is 0 to 10000 ms"
            self.latency_ms = value
            return _format(value)
        return f"ERR unknown command: {verb!r}"


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while data := await reader.read(CHUNK):
        writer.write(data)
        await writer.drain()


def _format(value: float) -> str:
    return f"{value:g}"


async def latency_command(port: int, line: str, timeout: float = 2.0) -> str:
    """Sends one control line to the proxy on `port` and returns its answer; raises `RuntimeError` on `ERR`."""

    async def exchange() -> str:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write((line + "\n").encode())
            await writer.drain()
            return (await reader.readline()).decode().strip()
        finally:
            writer.close()

    answer = await asyncio.wait_for(exchange(), timeout)
    if answer.startswith("ERR") or not answer:
        raise RuntimeError(f"latency proxy: {answer or 'no answer'}")
    return answer


async def serve(listen_port: int, control_port: int, upstream: str, latency_ms: float) -> None:
    host, _, port = upstream.rpartition(":")
    proxy = LatencyProxy(host or "127.0.0.1", int(port), latency_ms)
    data_server = await asyncio.start_server(proxy.handle_client, "127.0.0.1", listen_port, backlog=4096)
    control_server = await asyncio.start_server(proxy.handle_control, "127.0.0.1", control_port)
    async with data_server, control_server:
        await asyncio.gather(data_server.serve_forever(), control_server.serve_forever())


def raise_open_files_limit() -> None:
    """Raises the soft limit of open files to the hard limit."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft < hard:
        resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--listen", type=int, required=True, help="the port clients connect to")
    parser.add_argument("--control", type=int, required=True, help="the control port")
    parser.add_argument("--upstream", required=True, help="host:port of the PostgreSQL server")
    parser.add_argument("--latency", type=float, default=10, help="the added round-trip time in ms")
    args = parser.parse_args()
    raise_open_files_limit()
    try:
        import uvloop
    except ImportError:
        runner = asyncio.run
    else:
        runner = uvloop.run
    with contextlib.suppress(KeyboardInterrupt):
        runner(serve(args.listen, args.control, args.upstream, args.latency))


if __name__ == "__main__":
    main()
