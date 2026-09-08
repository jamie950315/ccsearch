"""Regression coverage for malformed requests and observable tool failures."""
import asyncio
import importlib
import threading
import tempfile
import os
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

import ccsearch


class TestStartupConfiguration(unittest.TestCase):
    def test_empty_key_file_fails_closed(self):
        with tempfile.NamedTemporaryFile() as key, patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "empty"):
                ccsearch.load_api_key(key.name, create_if_missing=True)

    def test_key_generation_is_private_and_shared_across_startups(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = os.path.join(directory, ".api_key")
            with ThreadPoolExecutor(max_workers=8) as executor:
                keys = list(executor.map(lambda _: ccsearch.load_api_key(path, create_if_missing=True), range(16)))
            self.assertEqual(len(set(keys)), 1)
            self.assertTrue(keys[0])
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(os.listdir(directory), [".api_key"])

    def test_unreadable_existing_config_is_not_silently_ignored(self):
        with patch("os.path.exists", return_value=True), patch("builtins.open", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                ccsearch.load_config("unreadable.ini")


class TestHttpRequestBoundaries(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.dict("os.environ", {"CCSEARCH_API_KEY": "review-test-key"}):
            cls.api = importlib.import_module("api_server")

    def setUp(self):
        self.key_patch = patch.object(self.api, "API_KEY", "review-test-key")
        self.key_patch.start()
        self.addCleanup(self.key_patch.stop)
        self.client = self.api.app.test_client()
        self.headers = {"X-API-Key": "review-test-key"}

    def test_non_object_json_is_a_client_error(self):
        for path in ("/search", "/batch"):
            for value in ([1], [], "query", 42, True):
                with self.subTest(path=path, value=value):
                    response = self.client.post(path, json=value, headers=self.headers)
                    self.assertEqual(response.status_code, 400)
                    self.assertIn("object", response.get_json()["message"])

    def test_non_string_query_and_engine_are_client_errors(self):
        for field in ("query", "engine"):
            for value in (None, 123, [], {}, True):
                with self.subTest(field=field, value=value):
                    payload = {"query": "hello", "engine": "brave", field: value}
                    response = self.client.post("/search", json=payload, headers=self.headers)
                    self.assertEqual(response.status_code, 400)

    def test_non_ascii_invalid_key_is_unauthorized(self):
        response = self.client.get("/diagnostics", headers={"X-API-Key": "\u00e9"})
        self.assertEqual(response.status_code, 401)

    def test_unexpected_exception_is_logged_not_silently_converted(self):
        with patch.object(self.api, "load_config"), patch.object(
            self.api, "execute_query", side_effect=RuntimeError("upstream unavailable")
        ), patch.object(self.api.app.logger, "exception") as log:
            response = self.client.post(
                "/search", json={"query": "hello", "engine": "brave"}, headers=self.headers
            )
        self.assertEqual(response.status_code, 500)
        log.assert_called_once()


class TestMcpErrorBoundaries(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.dict("os.environ", {"CCSEARCH_API_KEY": "review-test-key"}):
            cls.server = importlib.import_module("mcp_server")

    def test_runtime_errors_propagate(self):
        with patch.object(self.server, "load_config"), patch.object(
            self.server, "execute_query", side_effect=RuntimeError("upstream unavailable")
        ), self.assertRaisesRegex(RuntimeError, "upstream unavailable"):
            self.server.search("hello")

    def test_error_payload_becomes_tool_failure(self):
        with patch.object(self.server, "load_config"), patch.object(
            self.server, "execute_query", return_value={"error": "Both engines failed"}
        ), self.assertRaisesRegex(RuntimeError, "Both engines failed"):
            self.server.search("hello", engine="both")

    def test_registered_tool_rejects_invalid_search(self):
        from mcp.server.fastmcp.exceptions import ToolError

        with self.assertRaises(ToolError):
            asyncio.run(self.server.mcp.call_tool("search", {"query": ""}))

    def test_registered_tool_rejects_invalid_batch(self):
        from mcp.server.fastmcp.exceptions import ToolError

        with self.assertRaises(ToolError):
            asyncio.run(self.server.mcp.call_tool("batch", {"requests": []}))

    def test_blocking_search_does_not_block_transport_loop(self):
        started = threading.Event()
        released = threading.Event()

        def execute(*args, **kwargs):
            started.set()
            if not released.wait(timeout=2):
                raise AssertionError("MCP event loop could not release the worker")
            return {"engine": "brave", "results": []}

        async def exercise():
            pending = asyncio.create_task(self.server.mcp.call_tool("search", {"query": "hello"}))
            try:
                while not started.is_set():
                    if pending.done():
                        await pending
                        self.fail("Search returned without invoking its worker")
                    await asyncio.sleep(0.001)
                released.set()
                await pending
            finally:
                released.set()

        with patch.object(self.server, "load_config"), patch.object(self.server, "execute_query", execute):
            asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
