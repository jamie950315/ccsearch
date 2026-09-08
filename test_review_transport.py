"""Real loopback MCP transport regression tests, using disposable credentials."""
import asyncio
import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client


def local_http_client(**kwargs):
    return httpx.AsyncClient(trust_env=False, **kwargs)


@asynccontextmanager
async def local_streamable_client(url, httpx_client_factory=local_http_client):
    async with httpx_client_factory(timeout=10) as client:
        async with streamable_http_client(url, http_client=client) as streams:
            yield streams


class TestLocalMcpTransport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for name in ("httpx", "mcp.client.streamable_http"):
            logger = logging.getLogger(name)
            cls.addClassCleanup(logger.setLevel, logger.level)
            logger.setLevel(logging.WARNING)
        cls.directory = tempfile.TemporaryDirectory(prefix="ccsearch-transport-")
        cls.addClassCleanup(cls.directory.cleanup)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            cls.port = listener.getsockname()[1]
        cls.key = "disposable-transport-test-key"
        cls.base = f"http://127.0.0.1:{cls.port}"
        environment = {
            key: value for key, value in os.environ.items()
            if not key.lower().endswith("_proxy")
        }
        environment.update(CCSEARCH_API_KEY=cls.key, CCSEARCH_MCP_PORT=str(cls.port))
        root = Path(__file__).resolve().parent
        # Keep test cache/config completely separate from developer runtime state.
        program = (
            "import ccsearch, runpy; "
            f"ccsearch.get_cache_dir=lambda: {cls.directory.name!r}; "
            "original_load=ccsearch.load_config; "
            "ccsearch.load_config=lambda path: original_load('/nonexistent-transport-test.ini'); "
            f"runpy.run_path({str(root / 'mcp_server.py')!r}, run_name='__main__')"
        )
        cls.log = tempfile.TemporaryFile()
        cls.addClassCleanup(cls.log.close)
        cls.process = subprocess.Popen(
            [sys.executable, "-c", program], cwd=root, env=environment,
            stdout=cls.log, stderr=cls.log,
        )
        cls.addClassCleanup(cls.stop_server)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if cls.process.poll() is not None:
                raise AssertionError("Disposable MCP server exited during startup")
            try:
                with socket.create_connection(("127.0.0.1", cls.port), timeout=0.2):
                    return
            except OSError:
                time.sleep(0.05)
        raise AssertionError("Disposable MCP server did not start within 15 seconds")

    @classmethod
    def stop_server(cls):
        if cls.process.poll() is None:
            cls.process.terminate()
            try:
                cls.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.process.kill()
                cls.process.wait(timeout=5)

    async def exercise(self, transport, suffix):
        async with transport(f"{self.base}/{self.key}/{suffix}", httpx_client_factory=local_http_client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                initialized = await session.initialize()
                self.assertEqual(initialized.serverInfo.name, "ccsearch")
                listed = await session.list_tools()
                self.assertEqual({tool.name for tool in listed.tools}, {"search", "fetch", "batch", "engines", "diagnostics"})
                good = await session.call_tool("engines", {})
                self.assertFalse(good.isError)
                invalid = await session.call_tool("search", {"query": ""})
                self.assertTrue(invalid.isError)
                invalid_batch = await session.call_tool("batch", {"requests": []})
                self.assertTrue(invalid_batch.isError)

    def test_streamable_http_initializes_and_reports_tool_errors(self):
        asyncio.run(self.exercise(local_streamable_client, "mcp"))

    def test_sse_initializes_and_reports_tool_errors(self):
        asyncio.run(self.exercise(sse_client, "sse"))

    def test_missing_key_is_unauthorized(self):
        response = httpx.get(f"{self.base}/mcp", timeout=3, trust_env=False)
        self.assertEqual(response.status_code, 401)

    def test_slow_fetch_does_not_block_diagnostics(self):
        started, release = threading.Event(), threading.Event()

        class Page(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                started.set()
                release.wait(timeout=8)
                body = b"<html><title>Local test</title><body><p>A local page with real readable content for extraction.</p></body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        page = ThreadingHTTPServer(("127.0.0.1", 0), Page)
        worker = threading.Thread(target=page.serve_forever, daemon=True)
        worker.start()

        async def exercise():
            async with local_streamable_client(f"{self.base}/{self.key}/mcp") as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    fetching = asyncio.create_task(session.call_tool("fetch", {
                        "url": f"http://127.0.0.1:{page.server_port}/page"
                    }))
                    try:
                        self.assertTrue(await asyncio.to_thread(started.wait, 3), "Local fetch never reached the page")
                        diagnostic = await asyncio.wait_for(session.call_tool("diagnostics", {}), timeout=3)
                        self.assertFalse(diagnostic.isError)
                        self.assertFalse(fetching.done(), "Fetch should still be waiting for the local page")
                    finally:
                        release.set()
                    fetched = await fetching
                    self.assertFalse(fetched.isError)

        try:
            asyncio.run(exercise())
        finally:
            release.set()
            page.shutdown()
            page.server_close()
            worker.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
