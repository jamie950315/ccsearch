"""Regression coverage for fetch correctness and resource handling."""
import os
import importlib.util
import sys
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, patch

import requests
import ccsearch


def response(body, content_type="text/html", status=200):
    result = requests.Response()
    result.status_code = status
    result.url = "https://example.com/document"
    result._content = body.encode()
    result.headers["Content-Type"] = content_type
    return result


def pdf_fixture():
    """A valid one-page PDF generated in memory, with explicit cross-reference offsets."""
    stream = b"BT /F1 12 Tf 40 100 Td (ccsearch real PDF verification) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    data = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(data)
    data.extend(b"xref\n0 6\n0000000000 65535 f \n")
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(data)


class FetchReviewTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("markitdown") and importlib.util.find_spec("pdfminer"), "requires standard PDF dependencies")
    def test_real_http_pdf_conversion_from_dynamic_url(self):
        pdf = pdf_fixture()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Length", str(len(pdf)))
                self.end_headers()
                self.wfile.write(pdf)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            config = ccsearch.load_config("/nonexistent/config.ini")
            result = ccsearch.perform_fetch(f"http://127.0.0.1:{server.server_port}/download.php", config)
            self.assertNotIn("error", result)
            self.assertEqual(result["converted_via"], "markitdown")
            self.assertIn("ccsearch real PDF verification", result["content"])
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def test_cloudflare_ray_does_not_replace_successful_content(self):
        direct = response("<html><body><p>Useful short article.</p></body></html>")
        direct.headers["cf-ray"] = "ordinary-cdn-request"
        config = ccsearch.load_config("/nonexistent/config.ini")
        config.set("Fetch", "flaresolverr_url", "http://localhost:8191/v1")
        with patch.object(ccsearch, "_simple_fetch", return_value=direct), patch.object(ccsearch, "_flaresolverr_fetch") as browser:
            result = ccsearch.perform_fetch(direct.url, config)
        self.assertNotIn("error", result)
        self.assertIn("Useful short article.", result["content"])
        browser.assert_not_called()

    def test_explicit_cloudflare_challenge_header(self):
        direct = response("Challenge", status=403)
        direct.headers["cf-mitigated"] = "challenge"
        self.assertTrue(ccsearch._detect_cloudflare(direct))

    def test_browser_challenge_page_is_not_reported_as_success(self):
        challenge = response('<html><title>Just a moment...</title><body>Checking your browser</body></html>')
        result = ccsearch._build_flaresolverr_fetch_result(challenge.url, challenge)
        self.assertIn("challenge remains", result["error"])

    def test_flaresolverr_checks_http_status_before_decoding(self):
        upstream = response("Bad gateway", status=502)
        with patch.object(ccsearch.requests, "post", return_value=upstream):
            with self.assertRaisesRegex(requests.HTTPError, "502"):
                ccsearch._flaresolverr_fetch("https://example.com", "http://localhost:8191/v1")

    def test_curl_session_closed_on_success_and_failure(self):
        for failure in (None, TypeError("broken client")):
            with self.subTest(failure=failure):
                client = MagicMock()
                session = client.Session.return_value
                session.get.side_effect = failure
                with patch.object(ccsearch, "HAS_CURL_CFFI", True), patch.object(ccsearch, "cffi_requests", client, create=True):
                    if failure:
                        with self.assertRaises(TypeError):
                            ccsearch._simple_fetch("https://example.com", maxRetries=0)
                    else:
                        ccsearch._simple_fetch("https://example.com", maxRetries=0)
                session.close.assert_called_once()

    def test_programming_error_does_not_trigger_browser_fallback(self):
        config = ccsearch.load_config("/nonexistent/config.ini")
        config.set("Fetch", "flaresolverr_url", "http://localhost:8191/v1")
        with patch.object(ccsearch, "_simple_fetch", side_effect=TypeError("broken implementation")), patch.object(ccsearch, "_flaresolverr_fetch") as browser:
            with self.assertRaisesRegex(TypeError, "broken implementation"):
                ccsearch.perform_fetch("https://example.com", config)
        browser.assert_not_called()

    def test_programming_error_is_not_retried_by_curl_client(self):
        client = MagicMock()
        client.Session.return_value.get.side_effect = TypeError("broken implementation")
        with patch.object(ccsearch, "HAS_CURL_CFFI", True), patch.object(ccsearch, "cffi_requests", client, create=True), patch.object(ccsearch.time, "sleep") as sleep:
            with self.assertRaises(TypeError):
                ccsearch._simple_fetch("https://example.com", maxRetries=3)
        client.Session.assert_called_once()
        sleep.assert_not_called()

    def test_invalid_browser_configuration_fails_before_network(self):
        for option, value in (("flaresolverr_mode", "typo"), ("flaresolverr_mode", "always"), ("flaresolverr_timeout", "0")):
            config = ccsearch.load_config("/nonexistent/config.ini")
            config.set("Fetch", option, value)
            with self.subTest(option=option, value=value), patch.object(ccsearch, "_simple_fetch") as direct:
                with self.assertRaises(ValueError):
                    ccsearch.perform_fetch("https://example.com", config)
                direct.assert_not_called()

    def test_document_mime_overrides_dynamic_url_extension(self):
        self.assertEqual(ccsearch._guess_extension("https://example.com/download.php", "application/pdf"), ".pdf")

    def test_browser_failure_keeps_original_cause_in_result(self):
        shell = response('<html><body><div id="root"></div><script>boot()</script></body></html>')
        config = ccsearch.load_config("/nonexistent/config.ini")
        config.set("Fetch", "flaresolverr_url", "http://localhost:8191/v1")
        with patch.object(ccsearch, "_simple_fetch", return_value=shell), patch.object(ccsearch, "_flaresolverr_fetch", side_effect=requests.Timeout("browser timed out")):
            result = ccsearch.perform_fetch(shell.url, config)
        self.assertIn("browser timed out", result["error"])

    def test_pdf_content_is_not_scanned_as_cloudflare_html(self):
        direct = response("PDF body", "application/pdf")
        config = ccsearch.load_config("/nonexistent/config.ini")
        config.set("Fetch", "flaresolverr_url", "http://localhost:8191/v1")
        with patch.object(ccsearch, "_simple_fetch", return_value=direct), patch.object(ccsearch, "_detect_cloudflare") as detect, patch.object(ccsearch, "_convert_with_markitdown", return_value=("Document text", None)):
            result = ccsearch.perform_fetch(direct.url, config)
        self.assertEqual(result["content"], "Document text")
        detect.assert_not_called()

    def test_converter_empty_content_is_failure_not_object_repr(self):
        converter = MagicMock()
        converter.convert.return_value.text_content = ""
        module = types.SimpleNamespace(MarkItDown=MagicMock(return_value=converter))
        with patch.dict(sys.modules, {"markitdown": module}):
            text, error = ccsearch._convert_with_markitdown(b"document", "https://example.com/doc.pdf", "application/pdf")
        self.assertIsNone(text)
        self.assertIn("no extractable content", error)
        converter.convert_stream.assert_not_called()
        self.assertFalse(os.path.exists(converter.convert.call_args.args[0]))

    def test_converter_type_error_not_retried(self):
        converter = MagicMock()
        converter.convert.side_effect = TypeError("converter implementation failure")
        module = types.SimpleNamespace(MarkItDown=MagicMock(return_value=converter))
        with patch.dict(sys.modules, {"markitdown": module}):
            text, error = ccsearch._convert_with_markitdown(b"document", "https://example.com/doc.pdf", "application/pdf")
        self.assertIsNone(text)
        self.assertIn("converter implementation failure", error)
        converter.convert.assert_called_once()
        converter.convert_stream.assert_not_called()
        self.assertFalse(os.path.exists(converter.convert.call_args.args[0]))


if __name__ == "__main__":
    unittest.main()
