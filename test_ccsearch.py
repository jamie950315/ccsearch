#!/usr/bin/env python3
"""
Comprehensive tests for ccsearch.py — every function, every code path.
Run: python3 -m pytest test_ccsearch.py -v
"""
import os
import sys
import json
import time
import hashlib
import tempfile
import unittest
from unittest.mock import patch, MagicMock, Mock, call
import configparser
import requests

# Import the module under test
import ccsearch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(**fetch_overrides):
    """Build a configparser object with Fetch section overrides."""
    config = configparser.ConfigParser()
    config['Brave'] = {'max_retries': '2', 'requests_per_second': '1',
                       'count': '10', 'safesearch': 'moderate', 'freshness': ''}
    config['Perplexity'] = {'model': 'perplexity/sonar', 'citations': 'true',
                            'temperature': '0.1', 'max_tokens': '1024', 'max_retries': '2'}
    config['Fetch'] = {
        'flaresolverr_url': fetch_overrides.get('flaresolverr_url', ''),
        'flaresolverr_timeout': str(fetch_overrides.get('flaresolverr_timeout', 60000)),
        'flaresolverr_mode': fetch_overrides.get('flaresolverr_mode', 'fallback'),
    }
    return config


def _mock_response(status_code=200, text='', content=None, headers=None, json_data=None):
    """Create a mock requests.Response."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.text = text
    resp.content = content if content is not None else text.encode('utf-8')
    resp.headers = headers or {}
    resp.json.return_value = json_data or {}
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        http_err = requests.exceptions.HTTPError(response=resp)
        resp.raise_for_status.side_effect = http_err
    return resp


# ===========================================================================
# 1. load_config
# ===========================================================================
class TestLoadConfig(unittest.TestCase):

    def test_defaults_when_file_missing(self):
        config = ccsearch.load_config('/nonexistent/path/config.ini')
        self.assertEqual(config.get('Brave', 'count'), '10')
        self.assertEqual(config.get('Brave', 'safesearch'), 'moderate')
        self.assertEqual(config.get('Brave', 'max_retries'), '2')
        self.assertEqual(config.get('Perplexity', 'model'), 'perplexity/sonar')
        self.assertEqual(config.get('Perplexity', 'citations'), 'true')
        self.assertEqual(config.get('Fetch', 'flaresolverr_url'), '')
        self.assertEqual(config.get('Fetch', 'flaresolverr_timeout'), '60000')
        self.assertEqual(config.get('Fetch', 'flaresolverr_mode'), 'fallback')

    def test_file_overrides_defaults(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.ini', delete=False) as f:
            f.write("[Brave]\ncount = 20\n[Fetch]\nflaresolverr_url = http://localhost:8191/v1\n")
            f.flush()
            config = ccsearch.load_config(f.name)
        os.unlink(f.name)
        self.assertEqual(config.get('Brave', 'count'), '20')
        # Defaults still present for keys not in file
        self.assertEqual(config.get('Brave', 'safesearch'), 'moderate')
        self.assertEqual(config.get('Fetch', 'flaresolverr_url'), 'http://localhost:8191/v1')
        # Default preserved
        self.assertEqual(config.get('Fetch', 'flaresolverr_mode'), 'fallback')

    def test_all_three_sections_exist(self):
        config = ccsearch.load_config('/nonexistent')
        self.assertTrue(config.has_section('Brave'))
        self.assertTrue(config.has_section('Perplexity'))
        self.assertTrue(config.has_section('Fetch'))


# ===========================================================================
# 2. Cache utilities
# ===========================================================================
class TestCacheDir(unittest.TestCase):

    def test_returns_expected_path(self):
        d = ccsearch.get_cache_dir()
        self.assertTrue(d.endswith(os.path.join('.cache', 'ccsearch')))
        self.assertTrue(os.path.isdir(d))


class TestCacheKey(unittest.TestCase):

    def test_deterministic(self):
        k1 = ccsearch.get_cache_key('q', 'brave', 0)
        k2 = ccsearch.get_cache_key('q', 'brave', 0)
        self.assertEqual(k1, k2)

    def test_different_inputs_different_keys(self):
        k1 = ccsearch.get_cache_key('q', 'brave', 0)
        k2 = ccsearch.get_cache_key('q', 'brave', 1)
        self.assertNotEqual(k1, k2)

    def test_ends_with_json(self):
        k = ccsearch.get_cache_key('test', 'fetch', None)
        self.assertTrue(k.endswith('.json'))

    def test_hash_format(self):
        k = ccsearch.get_cache_key('x', 'y', 'z')
        name = k.replace('.json', '')
        # md5 hex is 32 chars
        self.assertEqual(len(name), 32)


class TestReadWriteCache(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig = ccsearch.get_cache_dir
        ccsearch.get_cache_dir = lambda: self.tmpdir

    def tearDown(self):
        ccsearch.get_cache_dir = self._orig
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_write_then_read(self):
        data = {"engine": "brave", "results": [1, 2, 3]}
        ccsearch.write_to_cache('q', 'brave', 0, data)
        result = ccsearch.read_from_cache('q', 'brave', 0, ttl_minutes=10)
        self.assertEqual(result, data)

    def test_read_nonexistent_returns_none(self):
        self.assertIsNone(ccsearch.read_from_cache('nope', 'brave', 0, 10))

    def test_expired_cache_returns_none(self):
        data = {"x": 1}
        ccsearch.write_to_cache('q', 'brave', 0, data)
        # Manually set mtime to 20 minutes ago
        cache_file = os.path.join(self.tmpdir, ccsearch.get_cache_key('q', 'brave', 0))
        old_time = time.time() - 1200
        os.utime(cache_file, (old_time, old_time))
        self.assertIsNone(ccsearch.read_from_cache('q', 'brave', 0, ttl_minutes=10))

    def test_corrupted_cache_returns_none(self):
        cache_file = os.path.join(self.tmpdir, ccsearch.get_cache_key('q', 'brave', 0))
        with open(cache_file, 'w') as f:
            f.write('NOT JSON{{{')
        self.assertIsNone(ccsearch.read_from_cache('q', 'brave', 0, 10))

    def test_unicode_data_roundtrip(self):
        data = {"content": "日本語テスト 🎉"}
        ccsearch.write_to_cache('q', 'fetch', None, data)
        result = ccsearch.read_from_cache('q', 'fetch', None, 10)
        self.assertEqual(result["content"], "日本語テスト 🎉")

    def test_write_failure_no_crash(self):
        """write_to_cache should not raise even if write fails."""
        ccsearch.get_cache_dir = lambda: '/nonexistent/dir/that/cannot/exist'
        # Should not raise
        ccsearch.write_to_cache('q', 'brave', 0, {"x": 1})


# ===========================================================================
# 3. retry_request
# ===========================================================================
class TestRetryRequest(unittest.TestCase):

    @patch('ccsearch.requests.get')
    def test_success_first_try(self, mock_get):
        resp = _mock_response(200, text='ok')
        mock_get.return_value = resp
        result = ccsearch.retry_request('GET', 'http://x', 2)
        self.assertEqual(result, resp)
        self.assertEqual(mock_get.call_count, 1)

    @patch('ccsearch.requests.post')
    def test_post_method(self, mock_post):
        resp = _mock_response(200, text='ok')
        mock_post.return_value = resp
        result = ccsearch.retry_request('POST', 'http://x', 0)
        self.assertEqual(result, resp)
        mock_post.assert_called_once()

    @patch('ccsearch.time.sleep')
    @patch('ccsearch.requests.get')
    def test_retry_on_timeout(self, mock_get, mock_sleep):
        mock_get.side_effect = [
            requests.exceptions.Timeout("timed out"),
            _mock_response(200, text='ok')
        ]
        result = ccsearch.retry_request('GET', 'http://x', 2)
        self.assertEqual(mock_get.call_count, 2)
        mock_sleep.assert_called_once_with(1)  # 2^0 = 1

    @patch('ccsearch.time.sleep')
    @patch('ccsearch.requests.get')
    def test_retry_on_connection_error(self, mock_get, mock_sleep):
        mock_get.side_effect = [
            requests.exceptions.ConnectionError("conn refused"),
            requests.exceptions.ConnectionError("conn refused"),
            _mock_response(200, text='ok')
        ]
        result = ccsearch.retry_request('GET', 'http://x', 2)
        self.assertEqual(mock_get.call_count, 3)
        mock_sleep.assert_any_call(1)   # 2^0
        mock_sleep.assert_any_call(2)   # 2^1

    @patch('ccsearch.time.sleep')
    @patch('ccsearch.requests.get')
    def test_no_retry_on_4xx(self, mock_get, mock_sleep):
        """4xx errors (except 429) should raise immediately without retry."""
        resp = _mock_response(403, text='forbidden')
        mock_get.return_value = resp
        with self.assertRaises(requests.exceptions.HTTPError):
            ccsearch.retry_request('GET', 'http://x', 3)
        self.assertEqual(mock_get.call_count, 1)
        mock_sleep.assert_not_called()

    @patch('ccsearch.time.sleep')
    @patch('ccsearch.requests.get')
    def test_retry_on_429(self, mock_get, mock_sleep):
        """429 Too Many Requests should be retried."""
        resp_429 = _mock_response(429, text='rate limited')
        resp_ok = _mock_response(200, text='ok')
        mock_get.side_effect = [resp_429, resp_ok]
        result = ccsearch.retry_request('GET', 'http://x', 2)
        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual(result.status_code, 200)

    @patch('ccsearch.time.sleep')
    @patch('ccsearch.requests.get')
    def test_exhausted_retries_raises(self, mock_get, mock_sleep):
        mock_get.side_effect = requests.exceptions.Timeout("timeout")
        with self.assertRaises(requests.exceptions.Timeout):
            ccsearch.retry_request('GET', 'http://x', 2)
        self.assertEqual(mock_get.call_count, 3)  # initial + 2 retries

    @patch('ccsearch.requests.get')
    def test_zero_retries(self, mock_get):
        mock_get.side_effect = requests.exceptions.Timeout("timeout")
        with self.assertRaises(requests.exceptions.Timeout):
            ccsearch.retry_request('GET', 'http://x', 0)
        self.assertEqual(mock_get.call_count, 1)

    @patch('ccsearch.time.sleep')
    @patch('ccsearch.requests.get')
    def test_retry_on_500(self, mock_get, mock_sleep):
        """5xx errors should be retried."""
        resp_500 = _mock_response(500, text='server error')
        resp_ok = _mock_response(200, text='ok')
        mock_get.side_effect = [resp_500, resp_ok]
        result = ccsearch.retry_request('GET', 'http://x', 2)
        self.assertEqual(result.status_code, 200)

    @patch('ccsearch.time.sleep')
    @patch('ccsearch.requests.get')
    def test_exponential_backoff_timing(self, mock_get, mock_sleep):
        mock_get.side_effect = requests.exceptions.Timeout("t")
        with self.assertRaises(requests.exceptions.Timeout):
            ccsearch.retry_request('GET', 'http://x', 3)
        # Sleeps: 2^0=1, 2^1=2, 2^2=4
        mock_sleep.assert_has_calls([call(1), call(2), call(4)])


# ===========================================================================
# 4. perform_brave_search
# ===========================================================================
class TestPerformBraveSearch(unittest.TestCase):

    def _default_config(self):
        return _make_config()

    @patch('ccsearch.time.sleep')
    @patch('ccsearch.retry_request')
    def test_basic_search(self, mock_req, mock_sleep):
        mock_req.return_value = _mock_response(json_data={
            "web": {"results": [
                {"title": "T1", "url": "http://a.com", "description": "D1"},
                {"title": "T2", "url": "http://b.com", "description": "D2"},
            ]}
        })
        result = ccsearch.perform_brave_search("test", "key123", self._default_config())
        self.assertEqual(result["engine"], "brave")
        self.assertEqual(result["query"], "test")
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(result["results"][0]["title"], "T1")

    @patch('ccsearch.time.sleep')
    @patch('ccsearch.retry_request')
    def test_empty_results(self, mock_req, mock_sleep):
        mock_req.return_value = _mock_response(json_data={})
        result = ccsearch.perform_brave_search("test", "key", self._default_config())
        self.assertEqual(result["results"], [])

    @patch('ccsearch.time.sleep')
    @patch('ccsearch.retry_request')
    def test_offset_passed(self, mock_req, mock_sleep):
        mock_req.return_value = _mock_response(json_data={"web": {"results": []}})
        ccsearch.perform_brave_search("test", "key", self._default_config(), offset=5)
        call_kwargs = mock_req.call_args
        self.assertEqual(call_kwargs.kwargs['params']['offset'], 5)

    @patch('ccsearch.time.sleep')
    @patch('ccsearch.retry_request')
    def test_offset_none_not_in_params(self, mock_req, mock_sleep):
        mock_req.return_value = _mock_response(json_data={"web": {"results": []}})
        ccsearch.perform_brave_search("test", "key", self._default_config(), offset=None)
        call_kwargs = mock_req.call_args
        self.assertNotIn('offset', call_kwargs.kwargs['params'])

    @patch('ccsearch.time.sleep')
    @patch('ccsearch.retry_request')
    def test_safesearch_config(self, mock_req, mock_sleep):
        mock_req.return_value = _mock_response(json_data={"web": {"results": []}})
        config = self._default_config()
        config.set('Brave', 'safesearch', 'strict')
        ccsearch.perform_brave_search("test", "key", config)
        self.assertEqual(mock_req.call_args.kwargs['params']['safesearch'], 'strict')

    @patch('ccsearch.time.sleep')
    @patch('ccsearch.retry_request')
    def test_freshness_config(self, mock_req, mock_sleep):
        mock_req.return_value = _mock_response(json_data={"web": {"results": []}})
        config = self._default_config()
        config.set('Brave', 'freshness', 'pw')
        ccsearch.perform_brave_search("test", "key", config)
        self.assertEqual(mock_req.call_args.kwargs['params']['freshness'], 'pw')

    @patch('ccsearch.time.sleep')
    @patch('ccsearch.retry_request')
    def test_freshness_empty_not_in_params(self, mock_req, mock_sleep):
        mock_req.return_value = _mock_response(json_data={"web": {"results": []}})
        config = self._default_config()
        config.set('Brave', 'freshness', '')
        ccsearch.perform_brave_search("test", "key", config)
        self.assertNotIn('freshness', mock_req.call_args.kwargs['params'])

    @patch('ccsearch.time.sleep')
    @patch('ccsearch.retry_request')
    def test_invalid_safesearch_not_in_params(self, mock_req, mock_sleep):
        mock_req.return_value = _mock_response(json_data={"web": {"results": []}})
        config = self._default_config()
        config.set('Brave', 'safesearch', 'INVALID')
        ccsearch.perform_brave_search("test", "key", config)
        self.assertNotIn('safesearch', mock_req.call_args.kwargs['params'])

    @patch('ccsearch.time.sleep')
    @patch('ccsearch.retry_request')
    def test_rate_limiting_sleep(self, mock_req, mock_sleep):
        mock_req.return_value = _mock_response(json_data={"web": {"results": []}})
        config = self._default_config()
        config.set('Brave', 'requests_per_second', '2')
        ccsearch.perform_brave_search("test", "key", config)
        mock_sleep.assert_called_once_with(0.5)

    @patch('ccsearch.time.sleep')
    @patch('ccsearch.retry_request')
    def test_api_key_in_header(self, mock_req, mock_sleep):
        mock_req.return_value = _mock_response(json_data={"web": {"results": []}})
        ccsearch.perform_brave_search("test", "MY_KEY", self._default_config())
        headers = mock_req.call_args.kwargs['headers']
        self.assertEqual(headers['X-Subscription-Token'], 'MY_KEY')

    @patch('ccsearch.time.sleep')
    @patch('ccsearch.retry_request')
    def test_missing_fields_in_result_items(self, mock_req, mock_sleep):
        """Items missing title/url/description should get None."""
        mock_req.return_value = _mock_response(json_data={
            "web": {"results": [{}]}
        })
        result = ccsearch.perform_brave_search("test", "key", self._default_config())
        self.assertIsNone(result["results"][0]["title"])
        self.assertIsNone(result["results"][0]["url"])
        self.assertIsNone(result["results"][0]["description"])


# ===========================================================================
# 5. perform_perplexity_search
# ===========================================================================
class TestPerformPerplexitySearch(unittest.TestCase):

    def _default_config(self):
        return _make_config()

    @patch('ccsearch.retry_request')
    def test_basic_search(self, mock_req):
        mock_req.return_value = _mock_response(json_data={
            "choices": [{"message": {"content": "Answer here"}}]
        })
        result = ccsearch.perform_perplexity_search("test", "key", self._default_config())
        self.assertEqual(result["engine"], "perplexity")
        self.assertEqual(result["answer"], "Answer here")
        self.assertEqual(result["model"], "perplexity/sonar")

    @patch('ccsearch.retry_request')
    def test_missing_choices_key(self, mock_req):
        mock_req.return_value = _mock_response(json_data={})
        result = ccsearch.perform_perplexity_search("test", "key", self._default_config())
        self.assertEqual(result["answer"], "No response content found.")

    @patch('ccsearch.retry_request')
    def test_empty_choices_list(self, mock_req):
        mock_req.return_value = _mock_response(json_data={"choices": []})
        result = ccsearch.perform_perplexity_search("test", "key", self._default_config())
        self.assertEqual(result["answer"], "No response content found.")

    @patch('ccsearch.retry_request')
    def test_custom_model_config(self, mock_req):
        mock_req.return_value = _mock_response(json_data={
            "choices": [{"message": {"content": "ok"}}]
        })
        config = self._default_config()
        config.set('Perplexity', 'model', 'perplexity/sonar-pro')
        result = ccsearch.perform_perplexity_search("test", "key", config)
        self.assertEqual(result["model"], "perplexity/sonar-pro")

    @patch('ccsearch.retry_request')
    def test_citations_disabled(self, mock_req):
        mock_req.return_value = _mock_response(json_data={
            "choices": [{"message": {"content": "ok"}}]
        })
        config = self._default_config()
        config.set('Perplexity', 'citations', 'false')
        ccsearch.perform_perplexity_search("test", "key", config)
        payload = mock_req.call_args.kwargs['json']
        sys_msg = payload['messages'][0]['content']
        self.assertNotIn('citations', sys_msg)

    @patch('ccsearch.retry_request')
    def test_citations_enabled(self, mock_req):
        mock_req.return_value = _mock_response(json_data={
            "choices": [{"message": {"content": "ok"}}]
        })
        config = self._default_config()
        config.set('Perplexity', 'citations', 'true')
        ccsearch.perform_perplexity_search("test", "key", config)
        payload = mock_req.call_args.kwargs['json']
        sys_msg = payload['messages'][0]['content']
        self.assertIn('citations', sys_msg)

    @patch('ccsearch.retry_request')
    def test_temperature_and_max_tokens(self, mock_req):
        mock_req.return_value = _mock_response(json_data={
            "choices": [{"message": {"content": "ok"}}]
        })
        config = self._default_config()
        config.set('Perplexity', 'temperature', '0.7')
        config.set('Perplexity', 'max_tokens', '2048')
        ccsearch.perform_perplexity_search("test", "key", config)
        payload = mock_req.call_args.kwargs['json']
        self.assertAlmostEqual(payload['temperature'], 0.7)
        self.assertEqual(payload['max_tokens'], 2048)

    @patch('ccsearch.retry_request')
    def test_api_key_in_header(self, mock_req):
        mock_req.return_value = _mock_response(json_data={
            "choices": [{"message": {"content": "ok"}}]
        })
        ccsearch.perform_perplexity_search("test", "MY_KEY", self._default_config())
        headers = mock_req.call_args.kwargs['headers']
        self.assertEqual(headers['Authorization'], 'Bearer MY_KEY')


# ===========================================================================
# 6. perform_both_search
# ===========================================================================
class TestPerformBothSearch(unittest.TestCase):

    @patch('ccsearch.perform_perplexity_search')
    @patch('ccsearch.perform_brave_search')
    def test_both_succeed(self, mock_brave, mock_pplx):
        mock_brave.return_value = {"engine": "brave", "results": [{"title": "T"}]}
        mock_pplx.return_value = {"engine": "perplexity", "answer": "A"}
        result = ccsearch.perform_both_search("q", "bk", "pk", _make_config())
        self.assertEqual(result["engine"], "both")
        self.assertEqual(result["brave_results"], [{"title": "T"}])
        self.assertEqual(result["perplexity_answer"], "A")

    @patch('ccsearch.perform_perplexity_search')
    @patch('ccsearch.perform_brave_search')
    def test_brave_fails(self, mock_brave, mock_pplx):
        mock_brave.side_effect = Exception("brave down")
        mock_pplx.return_value = {"engine": "perplexity", "answer": "A"}
        result = ccsearch.perform_both_search("q", "bk", "pk", _make_config())
        self.assertEqual(result["brave_results"], [])
        self.assertEqual(result["perplexity_answer"], "A")

    @patch('ccsearch.perform_perplexity_search')
    @patch('ccsearch.perform_brave_search')
    def test_perplexity_fails(self, mock_brave, mock_pplx):
        mock_brave.return_value = {"engine": "brave", "results": [{"title": "T"}]}
        mock_pplx.side_effect = Exception("pplx down")
        result = ccsearch.perform_both_search("q", "bk", "pk", _make_config())
        self.assertEqual(result["brave_results"], [{"title": "T"}])
        self.assertEqual(result["perplexity_answer"], "")

    @patch('ccsearch.perform_perplexity_search')
    @patch('ccsearch.perform_brave_search')
    def test_both_fail(self, mock_brave, mock_pplx):
        mock_brave.side_effect = Exception("brave down")
        mock_pplx.side_effect = Exception("pplx down")
        result = ccsearch.perform_both_search("q", "bk", "pk", _make_config())
        self.assertEqual(result["brave_results"], [])
        self.assertEqual(result["perplexity_answer"], "")

    @patch('ccsearch.perform_perplexity_search')
    @patch('ccsearch.perform_brave_search')
    def test_offset_passed_to_brave(self, mock_brave, mock_pplx):
        mock_brave.return_value = {"engine": "brave", "results": []}
        mock_pplx.return_value = {"engine": "perplexity", "answer": ""}
        ccsearch.perform_both_search("q", "bk", "pk", _make_config(), offset=3)
        mock_brave.assert_called_once_with("q", "bk", unittest.mock.ANY, 3)


# ===========================================================================
# 7. _clean_html
# ===========================================================================
class TestCleanHtml(unittest.TestCase):

    def test_basic_html(self):
        html = '<html><head><title>Test Page</title></head><body><p>Hello World</p></body></html>'
        title, text = ccsearch._clean_html(html)
        self.assertEqual(title, "Test Page")
        self.assertIn("Hello World", text)

    def test_strips_script_and_style(self):
        html = '<html><head><title>T</title><style>body{color:red}</style></head><body><script>alert(1)</script><p>Content</p></body></html>'
        title, text = ccsearch._clean_html(html)
        self.assertNotIn('alert', text)
        self.assertNotIn('color:red', text)
        self.assertIn('Content', text)

    def test_strips_nav_footer_header_noscript(self):
        html = '<html><head><title>T</title></head><body><nav>NAV</nav><header>HDR</header><p>Main</p><footer>FTR</footer><noscript>NS</noscript></body></html>'
        title, text = ccsearch._clean_html(html)
        self.assertNotIn('NAV', text)
        self.assertNotIn('HDR', text)
        self.assertNotIn('FTR', text)
        self.assertNotIn('NS', text)
        self.assertIn('Main', text)

    def test_no_title_tag(self):
        html = '<html><body><p>No title here</p></body></html>'
        title, text = ccsearch._clean_html(html)
        self.assertEqual(title, "No Title")
        self.assertIn("No title here", text)

    def test_empty_title_tag(self):
        html = '<html><head><title></title></head><body>X</body></html>'
        title, text = ccsearch._clean_html(html)
        self.assertEqual(title, "No Title")

    def test_whitespace_title(self):
        html = '<html><head><title>  Spaced  </title></head><body>X</body></html>'
        title, text = ccsearch._clean_html(html)
        self.assertEqual(title, "Spaced")

    def test_blank_lines_removed(self):
        html = '<html><head><title>T</title></head><body><p>A</p>\n\n\n<p>B</p></body></html>'
        title, text = ccsearch._clean_html(html)
        lines = text.split('\n')
        for line in lines:
            self.assertTrue(len(line.strip()) > 0)

    def test_multi_space_split(self):
        html = '<html><head><title>T</title></head><body><p>Hello  World  Test</p></body></html>'
        title, text = ccsearch._clean_html(html)
        self.assertIn('Hello', text)
        self.assertIn('World', text)

    def test_bytes_input(self):
        """_clean_html should also work with bytes (from response.content)."""
        html = b'<html><head><title>Bytes</title></head><body><p>Works</p></body></html>'
        title, text = ccsearch._clean_html(html)
        self.assertEqual(title, "Bytes")
        self.assertIn("Works", text)

    def test_unicode_content(self):
        html = '<html><head><title>中文</title></head><body><p>日本語テスト</p></body></html>'
        title, text = ccsearch._clean_html(html)
        self.assertEqual(title, "中文")
        self.assertIn("日本語テスト", text)

    def test_nested_tags_stripped(self):
        html = '<html><head><title>T</title></head><body><nav><ul><li>Menu</li></ul></nav><div>Content</div></body></html>'
        title, text = ccsearch._clean_html(html)
        self.assertNotIn('Menu', text)
        self.assertIn('Content', text)

    def test_empty_body(self):
        html = '<html><head><title>Empty</title></head><body></body></html>'
        title, text = ccsearch._clean_html(html)
        self.assertEqual(title, "Empty")
        # Title text leaks into get_text() — expected BeautifulSoup behavior
        self.assertEqual(text, "Empty")


# ===========================================================================
# 8. _detect_cloudflare
# ===========================================================================
class TestDetectCloudflare(unittest.TestCase):

    def _resp(self, text='', content_len=None, headers=None):
        resp = MagicMock()
        resp.text = text
        if content_len is not None:
            resp.content = b'x' * content_len
        else:
            resp.content = text.encode('utf-8')
        resp.headers = headers or {}
        return resp

    def test_just_a_moment_title(self):
        r = self._resp('<html><head><title>Just a moment...</title></head><body>CF</body></html>')
        self.assertTrue(ccsearch._detect_cloudflare(r))

    def test_checking_your_browser(self):
        r = self._resp('<html><body>Checking your browser before accessing</body></html>')
        self.assertTrue(ccsearch._detect_cloudflare(r))

    def test_cf_browser_verification(self):
        r = self._resp('<html><body><div id="cf-browser-verification">V</div></body></html>')
        self.assertTrue(ccsearch._detect_cloudflare(r))

    def test_challenge_platform(self):
        r = self._resp('<html><body><div class="challenge-platform">C</div></body></html>')
        self.assertTrue(ccsearch._detect_cloudflare(r))

    def test_short_body_with_cfray(self):
        r = self._resp('short', content_len=500, headers={'cf-ray': '12345'})
        self.assertTrue(ccsearch._detect_cloudflare(r))

    def test_short_body_without_cfray(self):
        r = self._resp('short', content_len=500, headers={})
        self.assertFalse(ccsearch._detect_cloudflare(r))

    def test_long_body_with_cfray_not_detected(self):
        r = self._resp('x' * 2000, content_len=2000, headers={'cf-ray': '12345'})
        self.assertFalse(ccsearch._detect_cloudflare(r))

    def test_normal_page_not_detected(self):
        r = self._resp('<html><head><title>Normal</title></head><body><p>Real content here</p></body></html>')
        self.assertFalse(ccsearch._detect_cloudflare(r))

    def test_exactly_1024_bytes_not_short(self):
        """1024 bytes is NOT < 1024, so should not trigger the short-body heuristic."""
        r = self._resp('x' * 1024, content_len=1024, headers={'cf-ray': 'abc'})
        self.assertFalse(ccsearch._detect_cloudflare(r))

    def test_1023_bytes_is_short(self):
        r = self._resp('x' * 1023, content_len=1023, headers={'cf-ray': 'abc'})
        self.assertTrue(ccsearch._detect_cloudflare(r))

    def test_multiple_indicators_still_true(self):
        r = self._resp('<html><head><title>Just a moment...</title></head><body>Checking your browser cf-browser-verification challenge-platform</body></html>')
        self.assertTrue(ccsearch._detect_cloudflare(r))


# ===========================================================================
# 9. _simple_fetch
# ===========================================================================
class TestSimpleFetch(unittest.TestCase):

    @patch('ccsearch.retry_request')
    def test_calls_retry_request_with_get(self, mock_req):
        resp = _mock_response(200, text='<html>ok</html>')
        mock_req.return_value = resp
        result = ccsearch._simple_fetch('http://example.com', maxRetries=3)
        mock_req.assert_called_once_with('GET', 'http://example.com', 3,
                                          headers=ccsearch.FETCH_HEADERS, timeout=(10, 30))
        self.assertEqual(result, resp)

    @patch('ccsearch.retry_request')
    def test_default_retries(self, mock_req):
        resp = _mock_response(200)
        mock_req.return_value = resp
        ccsearch._simple_fetch('http://x')
        self.assertEqual(mock_req.call_args[0][2], 2)  # default maxRetries

    @patch('ccsearch.retry_request')
    def test_propagates_exception(self, mock_req):
        mock_req.side_effect = requests.exceptions.Timeout("t")
        with self.assertRaises(requests.exceptions.Timeout):
            ccsearch._simple_fetch('http://x')


# ===========================================================================
# 10. _flaresolverr_fetch
# ===========================================================================
class TestFlaresolverrFetch(unittest.TestCase):

    @patch('ccsearch.requests.post')
    def test_success(self, mock_post):
        mock_post.return_value = _mock_response(json_data={
            "status": "ok",
            "message": "Challenge solved!",
            "solution": {"response": "<html><body>Solved</body></html>", "status": 200}
        })
        html = ccsearch._flaresolverr_fetch('http://target.com', 'http://localhost:8191/v1', 60000)
        self.assertIn('Solved', html)
        # Verify POST payload
        call_kwargs = mock_post.call_args
        payload = call_kwargs.kwargs['json']
        self.assertEqual(payload['cmd'], 'request.get')
        self.assertEqual(payload['url'], 'http://target.com')
        self.assertEqual(payload['maxTimeout'], 60000)

    @patch('ccsearch.requests.post')
    def test_error_status(self, mock_post):
        mock_post.return_value = _mock_response(json_data={
            "status": "error",
            "message": "Timeout after 60000ms"
        })
        with self.assertRaises(Exception) as ctx:
            ccsearch._flaresolverr_fetch('http://x', 'http://localhost:8191/v1')
        self.assertIn('Timeout after 60000ms', str(ctx.exception))

    @patch('ccsearch.requests.post')
    def test_unknown_error_message(self, mock_post):
        mock_post.return_value = _mock_response(json_data={"status": "error"})
        with self.assertRaises(Exception) as ctx:
            ccsearch._flaresolverr_fetch('http://x', 'http://localhost:8191/v1')
        self.assertIn('Unknown error', str(ctx.exception))

    @patch('ccsearch.requests.post')
    def test_timeout_calculation(self, mock_post):
        mock_post.return_value = _mock_response(json_data={
            "status": "ok", "solution": {"response": "<html></html>"}
        })
        ccsearch._flaresolverr_fetch('http://x', 'http://fs:8191/v1', timeout=30000)
        call_kwargs = mock_post.call_args
        # httpTimeout should be (10, 30000/1000 + 10) = (10, 40.0)
        self.assertEqual(call_kwargs.kwargs['timeout'], (10, 40.0))

    @patch('ccsearch.requests.post')
    def test_network_error(self, mock_post):
        mock_post.side_effect = requests.exceptions.ConnectionError("refused")
        with self.assertRaises(requests.exceptions.ConnectionError):
            ccsearch._flaresolverr_fetch('http://x', 'http://fs:8191/v1')


# ===========================================================================
# 11. perform_fetch — orchestrator (all modes & paths)
# ===========================================================================
class TestPerformFetch(unittest.TestCase):

    # ---- Mode: never (or no URL) ----

    @patch('ccsearch._simple_fetch')
    def test_never_mode_direct_success(self, mock_sf):
        mock_sf.return_value = _mock_response(200,
            text='<html><head><title>Hi</title></head><body><p>Content</p></body></html>')
        config = _make_config(flaresolverr_mode='never', flaresolverr_url='http://fs:8191/v1')
        result = ccsearch.perform_fetch('http://x', config)
        self.assertEqual(result["fetched_via"], "direct")
        self.assertEqual(result["title"], "Hi")
        self.assertIn("Content", result["content"])

    @patch('ccsearch._simple_fetch')
    def test_no_url_configured_direct_success(self, mock_sf):
        mock_sf.return_value = _mock_response(200,
            text='<html><head><title>OK</title></head><body><p>Text</p></body></html>')
        config = _make_config(flaresolverr_url='')
        result = ccsearch.perform_fetch('http://x', config)
        self.assertEqual(result["fetched_via"], "direct")

    @patch('ccsearch._simple_fetch')
    def test_never_mode_direct_fails(self, mock_sf):
        mock_sf.side_effect = Exception("connection refused")
        config = _make_config(flaresolverr_mode='never', flaresolverr_url='http://fs:8191/v1')
        result = ccsearch.perform_fetch('http://x', config)
        self.assertIn("error", result)
        self.assertIn("connection refused", result["error"])

    @patch('ccsearch._simple_fetch')
    def test_no_url_direct_fails(self, mock_sf):
        mock_sf.side_effect = Exception("timeout")
        config = _make_config(flaresolverr_url='')
        result = ccsearch.perform_fetch('http://x', config)
        self.assertIn("error", result)
        self.assertIn("timeout", result["error"])

    # ---- Mode: always ----

    @patch('ccsearch._flaresolverr_fetch')
    def test_always_mode_success(self, mock_ff):
        mock_ff.return_value = '<html><head><title>FS</title></head><body><p>Solved</p></body></html>'
        config = _make_config(flaresolverr_mode='always', flaresolverr_url='http://fs:8191/v1')
        result = ccsearch.perform_fetch('http://x', config)
        self.assertEqual(result["fetched_via"], "flaresolverr")
        self.assertEqual(result["title"], "FS")
        self.assertIn("Solved", result["content"])

    @patch('ccsearch._flaresolverr_fetch')
    def test_always_mode_failure(self, mock_ff):
        mock_ff.side_effect = Exception("FS timeout")
        config = _make_config(flaresolverr_mode='always', flaresolverr_url='http://fs:8191/v1')
        result = ccsearch.perform_fetch('http://x', config)
        self.assertIn("error", result)
        self.assertIn("FlareSolverr failed", result["error"])

    @patch('ccsearch._simple_fetch')
    @patch('ccsearch._flaresolverr_fetch')
    def test_always_mode_does_not_call_simple_fetch(self, mock_ff, mock_sf):
        mock_ff.return_value = '<html><head><title>T</title></head><body>ok</body></html>'
        config = _make_config(flaresolverr_mode='always', flaresolverr_url='http://fs:8191/v1')
        ccsearch.perform_fetch('http://x', config)
        mock_sf.assert_not_called()

    @patch('ccsearch._flaresolverr_fetch')
    def test_always_mode_passes_timeout(self, mock_ff):
        mock_ff.return_value = '<html><head><title>T</title></head><body>ok</body></html>'
        config = _make_config(flaresolverr_mode='always', flaresolverr_url='http://fs:8191/v1',
                              flaresolverr_timeout=30000)
        ccsearch.perform_fetch('http://x', config)
        mock_ff.assert_called_once_with('http://x', 'http://fs:8191/v1', 30000)

    # ---- Mode: always but no URL → falls through to direct ----

    @patch('ccsearch._simple_fetch')
    def test_always_mode_no_url_falls_to_direct(self, mock_sf):
        mock_sf.return_value = _mock_response(200,
            text='<html><head><title>D</title></head><body><p>Direct</p></body></html>')
        config = _make_config(flaresolverr_mode='always', flaresolverr_url='')
        result = ccsearch.perform_fetch('http://x', config)
        self.assertEqual(result["fetched_via"], "direct")

    # ---- Mode: fallback — direct succeeds, no CF ----

    @patch('ccsearch._simple_fetch')
    def test_fallback_direct_success_no_cf(self, mock_sf):
        mock_sf.return_value = _mock_response(200,
            text='<html><head><title>Page</title></head><body><p>Real content</p></body></html>')
        config = _make_config(flaresolverr_mode='fallback', flaresolverr_url='http://fs:8191/v1')
        result = ccsearch.perform_fetch('http://x', config)
        self.assertEqual(result["fetched_via"], "direct")
        self.assertEqual(result["title"], "Page")

    # ---- Mode: fallback — direct succeeds but CF detected → FS succeeds ----

    @patch('ccsearch._flaresolverr_fetch')
    @patch('ccsearch._simple_fetch')
    def test_fallback_cf_detected_fs_succeeds(self, mock_sf, mock_ff):
        cf_html = '<html><head><title>Just a moment...</title></head><body>Checking your browser</body></html>'
        mock_sf.return_value = _mock_response(200, text=cf_html)
        mock_ff.return_value = '<html><head><title>Real</title></head><body><p>Actual content</p></body></html>'
        config = _make_config(flaresolverr_mode='fallback', flaresolverr_url='http://fs:8191/v1')
        result = ccsearch.perform_fetch('http://x', config)
        self.assertEqual(result["fetched_via"], "flaresolverr")
        self.assertEqual(result["title"], "Real")

    # ---- Mode: fallback — direct succeeds but CF detected → FS also fails ----

    @patch('ccsearch._flaresolverr_fetch')
    @patch('ccsearch._simple_fetch')
    def test_fallback_cf_detected_fs_fails(self, mock_sf, mock_ff):
        cf_html = '<html><head><title>Just a moment...</title></head><body>CF</body></html>'
        mock_sf.return_value = _mock_response(200, text=cf_html)
        mock_ff.side_effect = Exception("FS down")
        config = _make_config(flaresolverr_mode='fallback', flaresolverr_url='http://fs:8191/v1')
        result = ccsearch.perform_fetch('http://x', config)
        self.assertIn("error", result)
        self.assertIn("Cloudflare detected", result["error"])
        self.assertIn("FlareSolverr also failed", result["error"])

    # ---- Mode: fallback — direct fails → FS succeeds ----

    @patch('ccsearch._flaresolverr_fetch')
    @patch('ccsearch._simple_fetch')
    def test_fallback_direct_fails_fs_succeeds(self, mock_sf, mock_ff):
        mock_sf.side_effect = requests.exceptions.Timeout("timeout")
        mock_ff.return_value = '<html><head><title>FS</title></head><body><p>Rescued</p></body></html>'
        config = _make_config(flaresolverr_mode='fallback', flaresolverr_url='http://fs:8191/v1')
        result = ccsearch.perform_fetch('http://x', config)
        self.assertEqual(result["fetched_via"], "flaresolverr")
        self.assertIn("Rescued", result["content"])

    # ---- Mode: fallback — direct fails → FS also fails ----

    @patch('ccsearch._flaresolverr_fetch')
    @patch('ccsearch._simple_fetch')
    def test_fallback_both_fail(self, mock_sf, mock_ff):
        mock_sf.side_effect = Exception("direct error")
        mock_ff.side_effect = Exception("fs error")
        config = _make_config(flaresolverr_mode='fallback', flaresolverr_url='http://fs:8191/v1')
        result = ccsearch.perform_fetch('http://x', config)
        self.assertIn("error", result)
        self.assertIn("Direct fetch failed: direct error", result["error"])
        self.assertIn("FlareSolverr also failed: fs error", result["error"])

    # ---- Mode: fallback but no URL — CF detected but no fallback available ----

    @patch('ccsearch._simple_fetch')
    def test_fallback_no_url_cf_detected_returns_direct(self, mock_sf):
        cf_html = '<html><head><title>Just a moment...</title></head><body>CF</body></html>'
        mock_sf.return_value = _mock_response(200, text=cf_html)
        config = _make_config(flaresolverr_mode='fallback', flaresolverr_url='')
        result = ccsearch.perform_fetch('http://x', config)
        # No fallback URL, so returns CF page content as-is
        self.assertEqual(result["fetched_via"], "direct")

    # ---- Stderr messages ----

    @patch('ccsearch._flaresolverr_fetch')
    @patch('ccsearch._simple_fetch')
    def test_stderr_messages_always_mode(self, mock_sf, mock_ff):
        mock_ff.return_value = '<html><head><title>T</title></head><body>ok</body></html>'
        config = _make_config(flaresolverr_mode='always', flaresolverr_url='http://fs:8191/v1')
        from io import StringIO
        captured = StringIO()
        with patch('sys.stderr', captured):
            ccsearch.perform_fetch('http://x', config)
        output = captured.getvalue()
        self.assertIn("Using FlareSolverr (always mode)", output)
        self.assertIn("solved challenge successfully", output)

    @patch('ccsearch._flaresolverr_fetch')
    @patch('ccsearch._simple_fetch')
    def test_stderr_messages_cf_fallback(self, mock_sf, mock_ff):
        cf_html = '<html><head><title>Just a moment...</title></head><body>CF</body></html>'
        mock_sf.return_value = _mock_response(200, text=cf_html)
        mock_ff.return_value = '<html><head><title>T</title></head><body>ok</body></html>'
        config = _make_config(flaresolverr_mode='fallback', flaresolverr_url='http://fs:8191/v1')
        from io import StringIO
        captured = StringIO()
        with patch('sys.stderr', captured):
            ccsearch.perform_fetch('http://x', config)
        output = captured.getvalue()
        self.assertIn("Cloudflare detected, falling back to FlareSolverr", output)
        self.assertIn("solved challenge successfully", output)

    @patch('ccsearch._flaresolverr_fetch')
    @patch('ccsearch._simple_fetch')
    def test_stderr_messages_direct_fail_fallback(self, mock_sf, mock_ff):
        mock_sf.side_effect = Exception("conn refused")
        mock_ff.return_value = '<html><head><title>T</title></head><body>ok</body></html>'
        config = _make_config(flaresolverr_mode='fallback', flaresolverr_url='http://fs:8191/v1')
        from io import StringIO
        captured = StringIO()
        with patch('sys.stderr', captured):
            ccsearch.perform_fetch('http://x', config)
        output = captured.getvalue()
        self.assertIn("Direct fetch failed", output)
        self.assertIn("conn refused", output)

    # ---- Result structure ----

    @patch('ccsearch._simple_fetch')
    def test_result_has_all_fields_on_success(self, mock_sf):
        mock_sf.return_value = _mock_response(200,
            text='<html><head><title>T</title></head><body><p>C</p></body></html>')
        config = _make_config()
        result = ccsearch.perform_fetch('http://x', config)
        self.assertIn("engine", result)
        self.assertIn("url", result)
        self.assertIn("title", result)
        self.assertIn("content", result)
        self.assertIn("fetched_via", result)
        self.assertEqual(result["engine"], "fetch")
        self.assertEqual(result["url"], "http://x")

    @patch('ccsearch._simple_fetch')
    def test_result_has_error_field_on_failure(self, mock_sf):
        mock_sf.side_effect = Exception("fail")
        config = _make_config()
        result = ccsearch.perform_fetch('http://x', config)
        self.assertIn("error", result)
        self.assertEqual(result["engine"], "fetch")
        self.assertEqual(result["url"], "http://x")

    # ---- CF detection with various indicators in fallback mode ----

    @patch('ccsearch._flaresolverr_fetch')
    @patch('ccsearch._simple_fetch')
    def test_fallback_cf_browser_verification(self, mock_sf, mock_ff):
        html = '<html><body><div id="cf-browser-verification">V</div></body></html>'
        mock_sf.return_value = _mock_response(200, text=html)
        mock_ff.return_value = '<html><head><title>OK</title></head><body>ok</body></html>'
        config = _make_config(flaresolverr_mode='fallback', flaresolverr_url='http://fs:8191/v1')
        result = ccsearch.perform_fetch('http://x', config)
        self.assertEqual(result["fetched_via"], "flaresolverr")

    @patch('ccsearch._flaresolverr_fetch')
    @patch('ccsearch._simple_fetch')
    def test_fallback_challenge_platform(self, mock_sf, mock_ff):
        html = '<html><body><div class="challenge-platform">C</div></body></html>'
        mock_sf.return_value = _mock_response(200, text=html)
        mock_ff.return_value = '<html><head><title>OK</title></head><body>ok</body></html>'
        config = _make_config(flaresolverr_mode='fallback', flaresolverr_url='http://fs:8191/v1')
        result = ccsearch.perform_fetch('http://x', config)
        self.assertEqual(result["fetched_via"], "flaresolverr")

    @patch('ccsearch._flaresolverr_fetch')
    @patch('ccsearch._simple_fetch')
    def test_fallback_short_body_cfray(self, mock_sf, mock_ff):
        resp = _mock_response(200, text='short')
        resp.content = b'x' * 500
        resp.headers = {'cf-ray': 'abc'}
        mock_sf.return_value = resp
        mock_ff.return_value = '<html><head><title>OK</title></head><body>ok</body></html>'
        config = _make_config(flaresolverr_mode='fallback', flaresolverr_url='http://fs:8191/v1')
        result = ccsearch.perform_fetch('http://x', config)
        self.assertEqual(result["fetched_via"], "flaresolverr")


# ===========================================================================
# 12. main() — CLI integration
# ===========================================================================
class TestMainCLI(unittest.TestCase):

    def _run_main(self, args, env=None):
        """Helper to call main() with given argv and return (stdout, stderr, exit_code)."""
        from io import StringIO
        captured_out = StringIO()
        captured_err = StringIO()
        exit_code = 0
        old_argv = sys.argv
        old_env = os.environ.copy()
        try:
            sys.argv = ['ccsearch'] + args
            if env:
                os.environ.update(env)
            with patch('sys.stdout', captured_out), patch('sys.stderr', captured_err):
                try:
                    ccsearch.main()
                except SystemExit as e:
                    exit_code = e.code if e.code is not None else 0
        finally:
            sys.argv = old_argv
            os.environ.clear()
            os.environ.update(old_env)
        return captured_out.getvalue(), captured_err.getvalue(), exit_code

    # ---- Fetch engine ----

    @patch('ccsearch.perform_fetch')
    def test_fetch_engine_json(self, mock_pf):
        mock_pf.return_value = {"engine": "fetch", "url": "http://x", "title": "T",
                                "content": "C", "fetched_via": "direct"}
        out, err, code = self._run_main(['http://x', '-e', 'fetch', '--format', 'json'])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["fetched_via"], "direct")

    @patch('ccsearch.perform_fetch')
    def test_fetch_engine_text_success(self, mock_pf):
        mock_pf.return_value = {"engine": "fetch", "url": "http://x", "title": "Title",
                                "content": "Body", "fetched_via": "direct"}
        out, err, code = self._run_main(['http://x', '-e', 'fetch', '--format', 'text'])
        self.assertEqual(code, 0)
        self.assertIn("Fetched Content: Title", out)
        self.assertIn("Body", out)

    @patch('ccsearch.perform_fetch')
    def test_fetch_engine_text_error(self, mock_pf):
        mock_pf.return_value = {"engine": "fetch", "url": "http://x", "error": "fail"}
        out, err, code = self._run_main(['http://x', '-e', 'fetch', '--format', 'text'])
        self.assertIn("Error fetching URL: fail", out)

    def test_fetch_engine_invalid_url(self):
        out, err, code = self._run_main(['notaurl', '-e', 'fetch', '--format', 'json'])
        self.assertEqual(code, 1)
        self.assertIn("must be a valid HTTP or HTTPS URL", err)

    @patch('ccsearch.perform_fetch')
    def test_flaresolverr_flag_sets_always_mode(self, mock_pf):
        mock_pf.return_value = {"engine": "fetch", "url": "http://x", "title": "T",
                                "content": "C", "fetched_via": "flaresolverr"}
        out, err, code = self._run_main(['http://x', '-e', 'fetch', '--format', 'json', '--flaresolverr'])
        # Verify config was modified: mock_pf should have been called with config that has mode=always
        call_config = mock_pf.call_args[0][1]
        self.assertEqual(call_config.get('Fetch', 'flaresolverr_mode'), 'always')

    def test_flaresolverr_flag_no_url_warning(self):
        """--flaresolverr with no URL should warn and still work."""
        with patch('ccsearch.perform_fetch') as mock_pf:
            mock_pf.return_value = {"engine": "fetch", "url": "http://x", "title": "T",
                                    "content": "C", "fetched_via": "direct"}
            out, err, code = self._run_main(['http://x', '-e', 'fetch', '--format', 'json', '--flaresolverr'])
        self.assertIn("WARNING", err)
        self.assertIn("no flaresolverr_url configured", err)

    # ---- Brave engine ----

    def test_brave_missing_api_key(self):
        env_backup = os.environ.pop('BRAVE_API_KEY', None)
        try:
            out, err, code = self._run_main(['test', '-e', 'brave'])
            self.assertEqual(code, 1)
            self.assertIn("BRAVE_API_KEY", err)
        finally:
            if env_backup:
                os.environ['BRAVE_API_KEY'] = env_backup

    @patch('ccsearch.perform_brave_search')
    def test_brave_json_output(self, mock_bs):
        mock_bs.return_value = {"engine": "brave", "query": "test",
                                "results": [{"title": "T", "url": "http://a", "description": "D"}]}
        out, err, code = self._run_main(['test', '-e', 'brave', '--format', 'json'],
                                         env={'BRAVE_API_KEY': 'k'})
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(len(data["results"]), 1)

    @patch('ccsearch.perform_brave_search')
    def test_brave_text_output(self, mock_bs):
        mock_bs.return_value = {"engine": "brave", "query": "test",
                                "results": [{"title": "Title", "url": "http://a", "description": "Desc"}]}
        out, err, code = self._run_main(['test', '-e', 'brave', '--format', 'text'],
                                         env={'BRAVE_API_KEY': 'k'})
        self.assertIn("Brave Search Results for: test", out)
        self.assertIn("Title", out)

    @patch('ccsearch.perform_brave_search')
    def test_brave_offset_passed(self, mock_bs):
        mock_bs.return_value = {"engine": "brave", "query": "test", "results": []}
        self._run_main(['test', '-e', 'brave', '--format', 'json', '--offset', '2'],
                        env={'BRAVE_API_KEY': 'k'})
        self.assertEqual(mock_bs.call_args.kwargs['offset'], 2)

    # ---- Perplexity engine ----

    def test_perplexity_missing_api_key(self):
        env_backup = os.environ.pop('OPENROUTER_API_KEY', None)
        try:
            out, err, code = self._run_main(['test', '-e', 'perplexity'])
            self.assertEqual(code, 1)
            self.assertIn("OPENROUTER_API_KEY", err)
        finally:
            if env_backup:
                os.environ['OPENROUTER_API_KEY'] = env_backup

    @patch('ccsearch.perform_perplexity_search')
    def test_perplexity_json_output(self, mock_ps):
        mock_ps.return_value = {"engine": "perplexity", "model": "perplexity/sonar",
                                "query": "test", "answer": "The answer"}
        out, err, code = self._run_main(['test', '-e', 'perplexity', '--format', 'json'],
                                         env={'OPENROUTER_API_KEY': 'k'})
        data = json.loads(out)
        self.assertEqual(data["answer"], "The answer")

    @patch('ccsearch.perform_perplexity_search')
    def test_perplexity_text_output(self, mock_ps):
        mock_ps.return_value = {"engine": "perplexity", "model": "perplexity/sonar",
                                "query": "test", "answer": "Answer text"}
        out, err, code = self._run_main(['test', '-e', 'perplexity', '--format', 'text'],
                                         env={'OPENROUTER_API_KEY': 'k'})
        self.assertIn("Perplexity Search Answer", out)
        self.assertIn("Answer text", out)

    # ---- Both engine ----

    def test_both_missing_keys(self):
        env_backup_b = os.environ.pop('BRAVE_API_KEY', None)
        env_backup_p = os.environ.pop('OPENROUTER_API_KEY', None)
        try:
            out, err, code = self._run_main(['test', '-e', 'both'])
            self.assertEqual(code, 1)
            self.assertIn("Both BRAVE_API_KEY and OPENROUTER_API_KEY", err)
        finally:
            if env_backup_b:
                os.environ['BRAVE_API_KEY'] = env_backup_b
            if env_backup_p:
                os.environ['OPENROUTER_API_KEY'] = env_backup_p

    @patch('ccsearch.perform_both_search')
    def test_both_json_output(self, mock_both):
        mock_both.return_value = {"engine": "both", "query": "test",
                                  "brave_results": [], "perplexity_answer": "A"}
        out, err, code = self._run_main(['test', '-e', 'both', '--format', 'json'],
                                         env={'BRAVE_API_KEY': 'bk', 'OPENROUTER_API_KEY': 'pk'})
        data = json.loads(out)
        self.assertEqual(data["engine"], "both")

    @patch('ccsearch.perform_both_search')
    def test_both_text_output(self, mock_both):
        mock_both.return_value = {"engine": "both", "query": "test",
                                  "brave_results": [{"title": "T", "url": "U", "description": "D"}],
                                  "perplexity_answer": "Synthesized"}
        out, err, code = self._run_main(['test', '-e', 'both', '--format', 'text'],
                                         env={'BRAVE_API_KEY': 'bk', 'OPENROUTER_API_KEY': 'pk'})
        self.assertIn("Synthesized Answer (Perplexity)", out)
        self.assertIn("Source Reference Links (Brave)", out)

    # ---- Cache integration ----

    @patch('ccsearch.perform_brave_search')
    @patch('ccsearch.write_to_cache')
    @patch('ccsearch.read_from_cache')
    def test_cache_miss_then_write(self, mock_rc, mock_wc, mock_bs):
        mock_rc.return_value = None
        mock_bs.return_value = {"engine": "brave", "query": "test", "results": []}
        out, err, code = self._run_main(['test', '-e', 'brave', '--format', 'json', '--cache'],
                                         env={'BRAVE_API_KEY': 'k'})
        mock_wc.assert_called_once()

    @patch('ccsearch.perform_brave_search')
    @patch('ccsearch.read_from_cache')
    def test_cache_hit(self, mock_rc, mock_bs):
        mock_rc.return_value = {"engine": "brave", "query": "test", "results": [{"title": "cached"}]}
        out, err, code = self._run_main(['test', '-e', 'brave', '--format', 'json', '--cache'],
                                         env={'BRAVE_API_KEY': 'k'})
        mock_bs.assert_not_called()
        data = json.loads(out)
        self.assertTrue(data.get("_from_cache"))

    @patch('ccsearch.perform_brave_search')
    @patch('ccsearch.read_from_cache')
    def test_cache_hit_text_shows_label(self, mock_rc, mock_bs):
        mock_rc.return_value = {"engine": "brave", "query": "test", "results": []}
        out, err, code = self._run_main(['test', '-e', 'brave', '--format', 'text', '--cache'],
                                         env={'BRAVE_API_KEY': 'k'})
        self.assertIn("Returning Cached Result", out)

    @patch('ccsearch.perform_brave_search')
    @patch('ccsearch.read_from_cache')
    def test_no_cache_flag_no_cache_read(self, mock_rc, mock_bs):
        mock_bs.return_value = {"engine": "brave", "query": "test", "results": []}
        out, err, code = self._run_main(['test', '-e', 'brave', '--format', 'json'],
                                         env={'BRAVE_API_KEY': 'k'})
        mock_rc.assert_not_called()

    # ---- Error handling in main ----

    @patch('ccsearch.perform_brave_search')
    def test_http_error_handling(self, mock_bs):
        resp = _mock_response(500, text='Internal Server Error')
        mock_bs.side_effect = requests.exceptions.HTTPError(response=resp)
        out, err, code = self._run_main(['test', '-e', 'brave', '--format', 'json'],
                                         env={'BRAVE_API_KEY': 'k'})
        self.assertEqual(code, 1)
        self.assertIn("HTTP Error", err)

    @patch('ccsearch.perform_brave_search')
    def test_timeout_error_handling(self, mock_bs):
        mock_bs.side_effect = requests.exceptions.Timeout("timed out")
        out, err, code = self._run_main(['test', '-e', 'brave', '--format', 'json'],
                                         env={'BRAVE_API_KEY': 'k'})
        self.assertEqual(code, 1)
        self.assertIn("Timeout Error", err)

    @patch('ccsearch.perform_brave_search')
    def test_unexpected_error_handling(self, mock_bs):
        mock_bs.side_effect = RuntimeError("unexpected")
        out, err, code = self._run_main(['test', '-e', 'brave', '--format', 'json'],
                                         env={'BRAVE_API_KEY': 'k'})
        self.assertEqual(code, 1)
        self.assertIn("Unexpected error", err)


# ===========================================================================
# 13. Constants & globals sanity
# ===========================================================================
class TestConstants(unittest.TestCase):

    def test_fetch_headers_has_user_agent(self):
        self.assertIn('User-Agent', ccsearch.FETCH_HEADERS)
        self.assertIn('Accept', ccsearch.FETCH_HEADERS)
        self.assertIn('Accept-Language', ccsearch.FETCH_HEADERS)

    def test_cloudflare_indicators_list(self):
        self.assertIsInstance(ccsearch.CLOUDFLARE_INDICATORS, list)
        self.assertGreater(len(ccsearch.CLOUDFLARE_INDICATORS), 0)
        self.assertIn("Checking your browser", ccsearch.CLOUDFLARE_INDICATORS)
        self.assertIn("cf-browser-verification", ccsearch.CLOUDFLARE_INDICATORS)
        self.assertIn("challenge-platform", ccsearch.CLOUDFLARE_INDICATORS)


if __name__ == '__main__':
    unittest.main()
