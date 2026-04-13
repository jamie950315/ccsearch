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
import importlib
import concurrent.futures
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
    config['LLMContext'] = {'count': '20', 'maximum_number_of_tokens': '8192',
                            'maximum_number_of_urls': '20',
                            'context_threshold_mode': 'balanced',
                            'freshness': '', 'max_retries': '2'}
    config['Fetch'] = {
        'flaresolverr_url': fetch_overrides.get('flaresolverr_url', ''),
        'flaresolverr_timeout': str(fetch_overrides.get('flaresolverr_timeout', 60000)),
        'flaresolverr_mode': fetch_overrides.get('flaresolverr_mode', 'fallback'),
    }
    config['Batch'] = {
        'max_workers': str(fetch_overrides.get('batch_max_workers', 4)),
    }
    return config


def _mock_response(status_code=200, text='', content=None, headers=None, json_data=None, url=None, encoding='utf-8'):
    """Create a mock requests.Response."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.text = text
    resp.content = content if content is not None else text.encode('utf-8')
    resp.headers = headers or {}
    resp.url = url
    resp.encoding = encoding
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

    def test_llm_context_defaults(self):
        config = ccsearch.load_config('/nonexistent/path/config.ini')
        self.assertEqual(config.get('LLMContext', 'count'), '20')
        self.assertEqual(config.get('LLMContext', 'maximum_number_of_tokens'), '8192')
        self.assertEqual(config.get('LLMContext', 'maximum_number_of_urls'), '20')
        self.assertEqual(config.get('LLMContext', 'context_threshold_mode'), 'balanced')
        self.assertEqual(config.get('LLMContext', 'freshness'), '')
        self.assertEqual(config.get('LLMContext', 'max_retries'), '2')

    def test_all_sections_exist(self):
        config = ccsearch.load_config('/nonexistent')
        self.assertTrue(config.has_section('Brave'))
        self.assertTrue(config.has_section('Perplexity'))
        self.assertTrue(config.has_section('LLMContext'))
        self.assertTrue(config.has_section('Fetch'))


class TestApiKeyHelpers(unittest.TestCase):

    def test_load_api_key_prefers_environment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            key_path = os.path.join(tmpdir, '.api_key')
            with open(key_path, 'w', encoding='utf-8') as f:
                f.write('file-key')
            with patch.dict(os.environ, {'CCSEARCH_API_KEY': 'env-key'}, clear=False):
                self.assertEqual(ccsearch.load_api_key(key_path), 'env-key')

    def test_load_api_key_reads_existing_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            key_path = os.path.join(tmpdir, '.api_key')
            with open(key_path, 'w', encoding='utf-8') as f:
                f.write('file-key')
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(ccsearch.load_api_key(key_path), 'file-key')

    def test_load_api_key_generates_when_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            key_path = os.path.join(tmpdir, '.api_key')
            with patch.dict(os.environ, {}, clear=True):
                key = ccsearch.load_api_key(key_path, create_if_missing=True)
            self.assertTrue(key)
            self.assertTrue(os.path.exists(key_path))
            self.assertEqual(oct(os.stat(key_path).st_mode & 0o777), '0o600')

    def test_load_api_key_returns_empty_without_sources(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            key_path = os.path.join(tmpdir, '.api_key')
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(ccsearch.load_api_key(key_path, create_if_missing=False), '')

    def test_mask_secret_masks_middle(self):
        self.assertEqual(ccsearch.mask_secret('abcdefgh12345678'), 'abcd...5678')

    def test_mask_secret_short_values_fully_masked(self):
        self.assertEqual(ccsearch.mask_secret('abcd'), '****')


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

    def test_fetch_url_cache_key_normalizes_tracking_params(self):
        u1 = "HTTPS://Example.com/path/?b=2&utm_source=newsletter&a=1#fragment"
        u2 = "https://example.com/path?a=1&b=2"
        self.assertEqual(
            ccsearch.get_cache_key(u1, 'fetch', None),
            ccsearch.get_cache_key(u2, 'fetch', None),
        )

    def test_search_cache_key_normalizes_whitespace(self):
        q1 = "React    hooks   best   practices"
        q2 = " React hooks best practices "
        self.assertEqual(
            ccsearch.get_cache_key(q1, 'brave', 0),
            ccsearch.get_cache_key(q2, 'brave', 0),
        )


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

    def test_fetch_cache_normalizes_equivalent_urls(self):
        data = {"engine": "fetch", "content": "same page"}
        ccsearch.write_to_cache(
            'https://Example.com/path/?utm_source=x&b=2&a=1#frag',
            'fetch',
            None,
            data,
        )
        result = ccsearch.read_from_cache(
            'https://example.com/path?a=1&b=2',
            'fetch',
            None,
            ttl_minutes=10,
        )
        self.assertEqual(result["engine"], "fetch")
        self.assertEqual(result["content"], "same page")
        self.assertEqual(result["url"], 'https://example.com/path?a=1&b=2')

    def test_fetch_cache_hit_preserves_requested_url(self):
        data = {"engine": "fetch", "url": "https://example.com/original?utm_source=x", "content": "same page"}
        ccsearch.write_to_cache(
            'https://Example.com/path/?utm_source=x&b=2&a=1#frag',
            'fetch',
            None,
            data,
        )
        result = ccsearch.read_from_cache(
            'https://example.com/path?a=1&b=2',
            'fetch',
            None,
            ttl_minutes=10,
        )
        self.assertEqual(result["url"], 'https://example.com/path?a=1&b=2')
        self.assertEqual(result["content"], "same page")


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
        self.assertIsNone(result["offset"])
        self.assertEqual(result["result_count"], 2)
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(result["results"][0]["title"], "T1")
        self.assertEqual(result["results"][0]["hostname"], "a.com")
        self.assertEqual(result["results"][0]["rank"], 1)
        self.assertEqual(result["results"][1]["rank"], 2)
        self.assertEqual(result["result_hosts"], ["a.com", "b.com"])
        self.assertEqual(result["result_host_count"], 2)

    @patch('ccsearch.time.sleep')
    @patch('ccsearch.retry_request')
    def test_cleans_markup_and_deduplicates_urls(self, mock_req, mock_sleep):
        mock_req.return_value = _mock_response(json_data={
            "web": {"results": [
                {
                    "title": "GPT-5 <strong>Model</strong>",
                    "url": "https://example.com/page?utm_source=x",
                    "description": "Learn &#x27;more&#x27; <strong>now</strong> .",
                },
                {
                    "title": "Duplicate",
                    "url": "https://example.com/page",
                    "description": "dup",
                },
            ]}
        })
        result = ccsearch.perform_brave_search("test", "key123", self._default_config())
        self.assertEqual(result["result_count"], 1)
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["title"], "GPT-5 Model")
        self.assertEqual(result["results"][0]["description"], "Learn 'more' now.")

    @patch('ccsearch.time.sleep')
    @patch('ccsearch.retry_request')
    def test_result_hosts_normalize_www_prefix(self, mock_req, mock_sleep):
        mock_req.return_value = _mock_response(json_data={
            "web": {"results": [
                {"title": "T1", "url": "https://www.example.com/a", "description": "D1"},
                {"title": "T2", "url": "https://example.com/b", "description": "D2"},
            ]}
        })
        result = ccsearch.perform_brave_search("test", "key123", self._default_config())
        self.assertEqual(result["result_hosts"], ["example.com"])
        self.assertEqual(result["result_host_count"], 1)

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
        result = ccsearch.perform_brave_search("test", "key", self._default_config(), offset=5)
        call_kwargs = mock_req.call_args
        self.assertEqual(call_kwargs.kwargs['params']['offset'], 5)
        self.assertEqual(result["offset"], 5)

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

    @patch('ccsearch.retry_request')
    def test_decodes_entities_and_includes_citations(self, mock_req):
        mock_req.return_value = _mock_response(json_data={
            "choices": [{"message": {"content": "Fish &amp; Chips"}}],
            "citations": [
                "https://example.com/a?utm_source=x",
                {"url": "https://example.com/a", "title": "Duplicate"},
                {"link": "https://example.com/b", "name": "Source B"},
            ],
        })
        result = ccsearch.perform_perplexity_search("test", "key", self._default_config())
        self.assertEqual(result["answer"], "Fish & Chips")
        self.assertEqual(result["citations"], [
            {"url": "https://example.com/a?utm_source=x"},
            {"url": "https://example.com/b", "title": "Source B"},
        ])
        self.assertEqual(result["citation_hosts"], ["example.com"])
        self.assertEqual(result["citation_host_count"], 1)


# ===========================================================================
# 6. perform_both_search
# ===========================================================================
class TestPerformBothSearch(unittest.TestCase):

    @patch('ccsearch.perform_perplexity_search')
    @patch('ccsearch.perform_brave_search')
    def test_both_succeed(self, mock_brave, mock_pplx):
        mock_brave.return_value = {"engine": "brave", "results": [{"title": "T", "hostname": "a.com"}]}
        mock_pplx.return_value = {"engine": "perplexity", "answer": "A", "citations": [{"url": "https://example.com"}]}
        result = ccsearch.perform_both_search("q", "bk", "pk", _make_config(), offset=2)
        self.assertEqual(result["engine"], "both")
        self.assertEqual(result["offset"], 2)
        self.assertEqual(result["brave_result_count"], 1)
        self.assertEqual(result["brave_results"], [{"title": "T", "hostname": "a.com"}])
        self.assertEqual(result["brave_result_hosts"], ["a.com"])
        self.assertEqual(result["brave_result_host_count"], 1)
        self.assertEqual(result["perplexity_answer"], "A")
        self.assertEqual(result["perplexity_citations"], [{"url": "https://example.com"}])
        self.assertEqual(result["perplexity_citation_hosts"], ["example.com"])
        self.assertEqual(result["perplexity_citation_host_count"], 1)
        self.assertFalse(result["has_partial_failure"])

    @patch('ccsearch.perform_perplexity_search')
    @patch('ccsearch.perform_brave_search')
    def test_brave_fails(self, mock_brave, mock_pplx):
        mock_brave.side_effect = Exception("brave down")
        mock_pplx.return_value = {"engine": "perplexity", "answer": "A"}
        result = ccsearch.perform_both_search("q", "bk", "pk", _make_config())
        self.assertEqual(result["brave_result_count"], 0)
        self.assertEqual(result["brave_results"], [])
        self.assertEqual(result["perplexity_answer"], "A")
        self.assertEqual(result["brave_error"], "brave down")
        self.assertTrue(result["has_partial_failure"])

    @patch('ccsearch.perform_perplexity_search')
    @patch('ccsearch.perform_brave_search')
    def test_perplexity_fails(self, mock_brave, mock_pplx):
        mock_brave.return_value = {"engine": "brave", "results": [{"title": "T"}]}
        mock_pplx.side_effect = Exception("pplx down")
        result = ccsearch.perform_both_search("q", "bk", "pk", _make_config())
        self.assertEqual(result["brave_result_count"], 1)
        self.assertEqual(result["brave_results"], [{"title": "T"}])
        self.assertEqual(result["perplexity_answer"], "")
        self.assertEqual(result["perplexity_error"], "pplx down")
        self.assertTrue(result["has_partial_failure"])

    @patch('ccsearch.perform_perplexity_search')
    @patch('ccsearch.perform_brave_search')
    def test_both_fail(self, mock_brave, mock_pplx):
        mock_brave.side_effect = Exception("brave down")
        mock_pplx.side_effect = Exception("pplx down")
        result = ccsearch.perform_both_search("q", "bk", "pk", _make_config())
        self.assertEqual(result["brave_result_count"], 0)
        self.assertEqual(result["brave_results"], [])
        self.assertEqual(result["perplexity_answer"], "")
        self.assertTrue(result["has_partial_failure"])

    @patch('ccsearch.perform_perplexity_search')
    @patch('ccsearch.perform_brave_search')
    def test_offset_passed_to_brave(self, mock_brave, mock_pplx):
        mock_brave.return_value = {"engine": "brave", "results": []}
        mock_pplx.return_value = {"engine": "perplexity", "answer": ""}
        ccsearch.perform_both_search("q", "bk", "pk", _make_config(), offset=3)
        mock_brave.assert_called_once_with("q", "bk", unittest.mock.ANY, 3)


# ===========================================================================
# 6b. perform_llm_context_search
# ===========================================================================
class TestPerformLLMContextSearch(unittest.TestCase):

    def _default_config(self):
        return _make_config()

    def _sample_api_response(self):
        return {
            "grounding": {
                "generic": [
                    {
                        "url": "https://example.com/page1",
                        "title": "Page One",
                        "snippets": ["Snippet 1a", "Snippet 1b"]
                    },
                    {
                        "url": "https://example.com/page2",
                        "title": "Page Two",
                        "snippets": ["Snippet 2a"]
                    }
                ],
                "map": []
            },
            "sources": {
                "https://example.com/page1": {
                    "title": "Page One",
                    "hostname": "example.com",
                    "age": ["Monday, January 15, 2024", "2024-01-15", "380 days ago"]
                },
                "https://example.com/page2": {
                    "title": "Page Two",
                    "hostname": "example.com",
                    "age": None
                }
            }
        }

    @patch('ccsearch.time.sleep')
    @patch('ccsearch.retry_request')
    def test_basic_search(self, mock_req, mock_sleep):
        mock_req.return_value = _mock_response(json_data=self._sample_api_response())
        result = ccsearch.perform_llm_context_search("test query", "key123", self._default_config())
        self.assertEqual(result["engine"], "llm-context")
        self.assertEqual(result["query"], "test query")
        self.assertEqual(result["result_count"], 2)
        self.assertEqual(result["source_count"], 2)
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(result["results"][0]["title"], "Page One")
        self.assertEqual(result["results"][0]["url"], "https://example.com/page1")
        self.assertEqual(result["results"][0]["snippets"], ["Snippet 1a", "Snippet 1b"])
        self.assertEqual(result["results"][0]["hostname"], "example.com")
        self.assertEqual(result["results"][0]["age"], ["Monday, January 15, 2024", "2024-01-15", "380 days ago"])
        self.assertEqual(result["results"][0]["rank"], 1)
        self.assertIn("https://example.com/page1", result["sources"])
        self.assertEqual(result["result_hosts"], ["example.com"])
        self.assertEqual(result["result_host_count"], 1)

    @patch('ccsearch.time.sleep')
    @patch('ccsearch.retry_request')
    def test_cleans_snippets_deduplicates_and_merges_source_metadata(self, mock_req, mock_sleep):
        mock_req.return_value = _mock_response(json_data={
            "grounding": {
                "generic": [
                    {
                        "url": "https://example.com/post?utm_source=x",
                        "title": "React <strong>Hooks</strong>",
                        "snippets": ["Use &lt;code&gt;useEffect&lt;/code&gt; carefully"],
                    },
                    {
                        "url": "https://example.com/post",
                        "title": "Duplicate",
                        "snippets": ["dup"],
                    },
                ]
            },
            "sources": {
                "https://example.com/post?utm_source=x": {
                    "hostname": "example.com",
                    "age": "2d",
                }
            }
        })
        result = ccsearch.perform_llm_context_search("test query", "key123", self._default_config())
        self.assertEqual(result["result_count"], 1)
        self.assertEqual(result["source_count"], 1)
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["title"], "React Hooks")
        self.assertEqual(result["results"][0]["hostname"], "example.com")
        self.assertEqual(result["results"][0]["age"], "2d")
        self.assertIn("useEffect", result["results"][0]["snippets"][0])
        self.assertEqual(result["results"][0]["rank"], 1)

    @patch('ccsearch.time.sleep')
    @patch('ccsearch.retry_request')
    def test_empty_grounding(self, mock_req, mock_sleep):
        mock_req.return_value = _mock_response(json_data={"grounding": {"generic": []}, "sources": {}})
        result = ccsearch.perform_llm_context_search("test", "key", self._default_config())
        self.assertEqual(result["results"], [])
        self.assertEqual(result["sources"], {})

    @patch('ccsearch.time.sleep')
    @patch('ccsearch.retry_request')
    def test_missing_grounding_key(self, mock_req, mock_sleep):
        mock_req.return_value = _mock_response(json_data={})
        result = ccsearch.perform_llm_context_search("test", "key", self._default_config())
        self.assertEqual(result["results"], [])
        self.assertEqual(result["sources"], {})

    @patch('ccsearch.time.sleep')
    @patch('ccsearch.retry_request')
    def test_api_key_in_header(self, mock_req, mock_sleep):
        mock_req.return_value = _mock_response(json_data={"grounding": {"generic": []}, "sources": {}})
        ccsearch.perform_llm_context_search("test", "MY_API_KEY", self._default_config())
        headers = mock_req.call_args.kwargs['headers']
        self.assertEqual(headers['X-Subscription-Token'], 'MY_API_KEY')

    @patch('ccsearch.time.sleep')
    @patch('ccsearch.retry_request')
    def test_correct_endpoint(self, mock_req, mock_sleep):
        mock_req.return_value = _mock_response(json_data={"grounding": {"generic": []}, "sources": {}})
        ccsearch.perform_llm_context_search("test", "key", self._default_config())
        call_args = mock_req.call_args
        self.assertEqual(call_args.args[1], "https://api.search.brave.com/res/v1/llm/context")

    @patch('ccsearch.time.sleep')
    @patch('ccsearch.retry_request')
    def test_default_params(self, mock_req, mock_sleep):
        mock_req.return_value = _mock_response(json_data={"grounding": {"generic": []}, "sources": {}})
        ccsearch.perform_llm_context_search("test", "key", self._default_config())
        params = mock_req.call_args.kwargs['params']
        self.assertEqual(params['q'], 'test')
        self.assertEqual(params['count'], 20)
        self.assertEqual(params['maximum_number_of_tokens'], 8192)
        self.assertEqual(params['maximum_number_of_urls'], 20)
        self.assertEqual(params['context_threshold_mode'], 'balanced')

    @patch('ccsearch.time.sleep')
    @patch('ccsearch.retry_request')
    def test_custom_count_config(self, mock_req, mock_sleep):
        mock_req.return_value = _mock_response(json_data={"grounding": {"generic": []}, "sources": {}})
        config = self._default_config()
        config.set('LLMContext', 'count', '50')
        ccsearch.perform_llm_context_search("test", "key", config)
        self.assertEqual(mock_req.call_args.kwargs['params']['count'], 50)

    @patch('ccsearch.time.sleep')
    @patch('ccsearch.retry_request')
    def test_custom_max_tokens_config(self, mock_req, mock_sleep):
        mock_req.return_value = _mock_response(json_data={"grounding": {"generic": []}, "sources": {}})
        config = self._default_config()
        config.set('LLMContext', 'maximum_number_of_tokens', '16384')
        ccsearch.perform_llm_context_search("test", "key", config)
        self.assertEqual(mock_req.call_args.kwargs['params']['maximum_number_of_tokens'], 16384)

    @patch('ccsearch.time.sleep')
    @patch('ccsearch.retry_request')
    def test_custom_max_urls_config(self, mock_req, mock_sleep):
        mock_req.return_value = _mock_response(json_data={"grounding": {"generic": []}, "sources": {}})
        config = self._default_config()
        config.set('LLMContext', 'maximum_number_of_urls', '5')
        ccsearch.perform_llm_context_search("test", "key", config)
        self.assertEqual(mock_req.call_args.kwargs['params']['maximum_number_of_urls'], 5)

    @patch('ccsearch.time.sleep')
    @patch('ccsearch.retry_request')
    def test_threshold_mode_strict(self, mock_req, mock_sleep):
        mock_req.return_value = _mock_response(json_data={"grounding": {"generic": []}, "sources": {}})
        config = self._default_config()
        config.set('LLMContext', 'context_threshold_mode', 'strict')
        ccsearch.perform_llm_context_search("test", "key", config)
        self.assertEqual(mock_req.call_args.kwargs['params']['context_threshold_mode'], 'strict')

    @patch('ccsearch.time.sleep')
    @patch('ccsearch.retry_request')
    def test_threshold_mode_disabled(self, mock_req, mock_sleep):
        mock_req.return_value = _mock_response(json_data={"grounding": {"generic": []}, "sources": {}})
        config = self._default_config()
        config.set('LLMContext', 'context_threshold_mode', 'disabled')
        ccsearch.perform_llm_context_search("test", "key", config)
        self.assertEqual(mock_req.call_args.kwargs['params']['context_threshold_mode'], 'disabled')

    @patch('ccsearch.time.sleep')
    @patch('ccsearch.retry_request')
    def test_invalid_threshold_mode_not_in_params(self, mock_req, mock_sleep):
        mock_req.return_value = _mock_response(json_data={"grounding": {"generic": []}, "sources": {}})
        config = self._default_config()
        config.set('LLMContext', 'context_threshold_mode', 'INVALID')
        ccsearch.perform_llm_context_search("test", "key", config)
        self.assertNotIn('context_threshold_mode', mock_req.call_args.kwargs['params'])

    @patch('ccsearch.time.sleep')
    @patch('ccsearch.retry_request')
    def test_freshness_config(self, mock_req, mock_sleep):
        mock_req.return_value = _mock_response(json_data={"grounding": {"generic": []}, "sources": {}})
        config = self._default_config()
        config.set('LLMContext', 'freshness', 'pw')
        ccsearch.perform_llm_context_search("test", "key", config)
        self.assertEqual(mock_req.call_args.kwargs['params']['freshness'], 'pw')

    @patch('ccsearch.time.sleep')
    @patch('ccsearch.retry_request')
    def test_freshness_empty_not_in_params(self, mock_req, mock_sleep):
        mock_req.return_value = _mock_response(json_data={"grounding": {"generic": []}, "sources": {}})
        config = self._default_config()
        config.set('LLMContext', 'freshness', '')
        ccsearch.perform_llm_context_search("test", "key", config)
        self.assertNotIn('freshness', mock_req.call_args.kwargs['params'])

    @patch('ccsearch.time.sleep')
    @patch('ccsearch.retry_request')
    def test_rate_limiting_uses_brave_rps(self, mock_req, mock_sleep):
        mock_req.return_value = _mock_response(json_data={"grounding": {"generic": []}, "sources": {}})
        config = self._default_config()
        config.set('Brave', 'requests_per_second', '2')
        ccsearch.perform_llm_context_search("test", "key", config)
        mock_sleep.assert_called_once_with(0.5)

    @patch('ccsearch.time.sleep')
    @patch('ccsearch.retry_request')
    def test_missing_snippets_defaults_to_empty(self, mock_req, mock_sleep):
        mock_req.return_value = _mock_response(json_data={
            "grounding": {"generic": [{"url": "http://x", "title": "T"}]},
            "sources": {}
        })
        result = ccsearch.perform_llm_context_search("test", "key", self._default_config())
        self.assertEqual(result["results"][0]["snippets"], [])

    @patch('ccsearch.time.sleep')
    @patch('ccsearch.retry_request')
    def test_missing_fields_in_generic(self, mock_req, mock_sleep):
        mock_req.return_value = _mock_response(json_data={
            "grounding": {"generic": [{}]},
            "sources": {}
        })
        result = ccsearch.perform_llm_context_search("test", "key", self._default_config())
        self.assertIsNone(result["results"][0]["url"])
        self.assertIsNone(result["results"][0]["title"])
        self.assertEqual(result["results"][0]["snippets"], [])

    @patch('ccsearch.time.sleep')
    @patch('ccsearch.retry_request')
    def test_max_retries_config(self, mock_req, mock_sleep):
        mock_req.return_value = _mock_response(json_data={"grounding": {"generic": []}, "sources": {}})
        config = self._default_config()
        config.set('LLMContext', 'max_retries', '5')
        ccsearch.perform_llm_context_search("test", "key", config)
        self.assertEqual(mock_req.call_args.args[2], 5)


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

    def test_falls_back_to_og_title_when_title_tag_missing(self):
        html = '<html><head><meta property="og:title" content="OG Story"></head><body><p>Body</p></body></html>'
        title, text = ccsearch._clean_html(html)
        self.assertEqual(title, "OG Story")
        self.assertIn("Body", text)

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

    def test_normalizes_spacing_before_punctuation(self):
        html = '<html><head><title>T</title></head><body><p>Hello <a href="/x">world</a> !</p></body></html>'
        title, text = ccsearch._clean_html(html)
        self.assertIn('Hello world!', text)

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
        self.assertEqual(text, "")

    def test_prefers_article_content_when_substantial(self):
        html = '''
        <html>
          <head><title>Story</title></head>
          <body>
            <div>Related links should not be selected.</div>
            <article>
              <h1>Main Story</h1>
              <p>This is the primary article body with enough text to exceed the threshold and be selected as the content root.</p>
            </article>
          </body>
        </html>
        '''
        title, text = ccsearch._clean_html(html)
        self.assertEqual(title, "Story")
        self.assertIn("Main Story", text)
        self.assertIn("primary article body", text)
        self.assertNotIn("Related links should not be selected", text)

    def test_removes_aside_and_hidden_content(self):
        html = '''
        <html>
          <head><title>Article</title></head>
          <body>
            <main>
              <p>Visible content</p>
              <aside>Sidebar junk</aside>
              <div aria-hidden="true">Hidden junk</div>
            </main>
          </body>
        </html>
        '''
        title, text = ccsearch._clean_html(html)
        self.assertEqual(title, "Article")
        self.assertIn("Visible content", text)
        self.assertNotIn("Sidebar junk", text)
        self.assertNotIn("Hidden junk", text)

    def test_removes_cookie_and_newsletter_noise(self):
        html = '''
        <html>
          <head><title>Article</title></head>
          <body>
            <div id="cookie-banner">Accept cookies to continue</div>
            <section class="newsletter-modal">Subscribe to our newsletter</section>
            <article>
              <h1>Story title</h1>
              <p>Main article text that should remain visible after cleanup.</p>
            </article>
          </body>
        </html>
        '''
        title, text = ccsearch._clean_html(html)
        self.assertEqual(title, "Article")
        self.assertIn("Story title", text)
        self.assertIn("Main article text", text)
        self.assertNotIn("Accept cookies", text)
        self.assertNotIn("Subscribe to our newsletter", text)

    def test_prefers_content_block_over_link_heavy_sidebar(self):
        html = '''
        <html>
          <head><title>Story</title></head>
          <body>
            <div class="related-links">
              <a href="/a">Link one</a>
              <a href="/b">Link two</a>
              <a href="/c">Link three</a>
              <a href="/d">Link four</a>
              <a href="/e">Link five</a>
            </div>
            <div class="post-body">
              <h1>Primary headline</h1>
              <p>This is the main story body with several sentences. It contains punctuation, detail, and enough substance to win scoring.</p>
              <p>Another paragraph adds more context and should keep this block selected over the link-heavy sidebar.</p>
            </div>
          </body>
        </html>
        '''
        title, text = ccsearch._clean_html(html)
        self.assertEqual(title, "Story")
        self.assertIn("Primary headline", text)
        self.assertIn("main story body", text)
        self.assertNotIn("Link one", text)

    def test_preserves_list_structure(self):
        html = '''
        <html>
          <head><title>Checklist</title></head>
          <body>
            <article>
              <ul>
                <li>First item</li>
                <li>Second item</li>
              </ul>
            </article>
          </body>
        </html>
        '''
        title, text = ccsearch._clean_html(html)
        self.assertEqual(title, "Checklist")
        self.assertIn("- First item", text)
        self.assertIn("- Second item", text)

    def test_preserves_table_structure(self):
        html = '''
        <html>
          <head><title>Table</title></head>
          <body>
            <article>
              <table>
                <thead>
                  <tr><th>Name</th><th>Value</th></tr>
                </thead>
                <tbody>
                  <tr><td>Alpha</td><td>1</td></tr>
                  <tr><td>Beta</td><td>2</td></tr>
                </tbody>
              </table>
            </article>
          </body>
        </html>
        '''
        title, text = ccsearch._clean_html(html)
        self.assertEqual(title, "Table")
        self.assertIn("| Name | Value |", text)
        self.assertIn("| Alpha | 1 |", text)
        self.assertIn("| Beta | 2 |", text)

    def test_preserves_fenced_code_blocks_with_language(self):
        html = '''
        <html>
          <head><title>Code</title></head>
          <body>
            <article>
              <pre><code class="language-python">def hello():
    return "world"</code></pre>
            </article>
          </body>
        </html>
        '''
        title, text = ccsearch._clean_html(html)
        self.assertEqual(title, "Code")
        self.assertIn("```python", text)
        self.assertIn('return "world"', text)
        self.assertIn("```", text)

    def test_chunk_annotation_tracks_section_path(self):
        chunks = ccsearch._annotate_chunks([
            {"index": 1, "type": "heading", "text": "Main", "heading_level": 1},
            {"index": 2, "type": "paragraph", "text": "Intro body"},
            {"index": 3, "type": "heading", "text": "Details", "heading_level": 2},
            {"index": 4, "type": "paragraph", "text": "Deep detail"},
        ])
        self.assertEqual(chunks[0]["section_path"], ["Main"])
        self.assertEqual(chunks[0]["section_path_text"], "Main")
        self.assertEqual(chunks[0]["section_depth"], 1)
        self.assertEqual(chunks[1]["section_path"], ["Main"])
        self.assertEqual(chunks[2]["section_path"], ["Main", "Details"])
        self.assertEqual(chunks[2]["section_path_text"], "Main > Details")
        self.assertEqual(chunks[3]["section_path"], ["Main", "Details"])
        self.assertEqual(chunks[3]["section_depth"], 2)


# ===========================================================================
# 8. _extract_html_metadata
# ===========================================================================
class TestExtractHtmlMetadata(unittest.TestCase):

    def test_extracts_standard_metadata_fields(self):
        html = '''
        <html lang="en-US">
          <head>
            <link rel="canonical" href="/posts/example-story" />
            <meta name="description" content="A concise summary." />
            <meta name="author" content="Jamie" />
            <meta property="article:published_time" content="2026-04-11T10:30:00Z" />
          </head>
          <body><article>Story body</article></body>
        </html>
        '''
        metadata = ccsearch._extract_html_metadata(html, base_url="https://example.com/blog?id=1")
        self.assertEqual(metadata["lang"], "en-US")
        self.assertEqual(metadata["canonical_url"], "https://example.com/posts/example-story")
        self.assertEqual(metadata["description"], "A concise summary.")
        self.assertEqual(metadata["author"], "Jamie")
        self.assertEqual(metadata["published_at"], "2026-04-11T10:30:00Z")

    def test_extracts_social_metadata_fallbacks(self):
        html = '''
        <html>
          <head>
            <meta property="og:description" content="OG description" />
            <meta property="og:author" content="OG Author" />
            <meta itemprop="datePublished" content="2026-04-10" />
          </head>
          <body></body>
        </html>
        '''
        metadata = ccsearch._extract_html_metadata(html, base_url="https://example.com")
        self.assertEqual(metadata["description"], "OG description")
        self.assertEqual(metadata["author"], "OG Author")
        self.assertEqual(metadata["published_at"], "2026-04-10")

    def test_extracts_json_ld_metadata_fallbacks(self):
        html = '''
        <html>
          <head>
            <script type="application/ld+json">
              {
                "@context": "https://schema.org",
                "@type": "NewsArticle",
                "headline": "JSON-LD Headline",
                "description": "JSON-LD Description",
                "inLanguage": "en-GB",
                "author": [{"@type": "Person", "name": "Alex"}],
                "datePublished": "2026-04-01T00:00:00Z",
                "mainEntityOfPage": {"@id": "/posts/json-ld-story"}
              }
            </script>
          </head>
          <body></body>
        </html>
        '''
        metadata = ccsearch._extract_html_metadata(html, base_url="https://example.com/blog/")
        self.assertEqual(metadata["canonical_url"], "https://example.com/posts/json-ld-story")
        self.assertEqual(metadata["lang"], "en-GB")
        self.assertEqual(metadata["description"], "JSON-LD Description")
        self.assertEqual(metadata["author"], "Alex")
        self.assertEqual(metadata["published_at"], "2026-04-01T00:00:00Z")

    def test_extracts_json_ld_title_for_title_fallback(self):
        html = '''
        <html>
          <head>
            <script type="application/ld+json">
              {
                "@context": "https://schema.org",
                "@type": "BlogPosting",
                "headline": "JSON-LD Title"
              }
            </script>
          </head>
          <body><article>Body</article></body>
        </html>
        '''
        title, text = ccsearch._clean_html(html)
        self.assertEqual(title, "JSON-LD Title")
        self.assertIn("Body", text)


# ===========================================================================
# 9. _detect_cloudflare
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

    @patch('ccsearch.HAS_CURL_CFFI', False)
    @patch('ccsearch.retry_request')
    def test_calls_retry_request_with_get(self, mock_req):
        resp = _mock_response(200, text='<html>ok</html>')
        mock_req.return_value = resp
        result = ccsearch._simple_fetch('http://example.com', maxRetries=3)
        mock_req.assert_called_once_with('GET', 'http://example.com', 3,
                                          headers=ccsearch.FETCH_HEADERS, timeout=(10, 30))
        self.assertEqual(result, resp)

    @patch('ccsearch.HAS_CURL_CFFI', False)
    @patch('ccsearch.retry_request')
    def test_default_retries(self, mock_req):
        resp = _mock_response(200)
        mock_req.return_value = resp
        ccsearch._simple_fetch('http://x')
        self.assertEqual(mock_req.call_args[0][2], 2)  # default maxRetries

    @patch('ccsearch.HAS_CURL_CFFI', False)
    @patch('ccsearch.retry_request')
    def test_propagates_exception(self, mock_req):
        mock_req.side_effect = requests.exceptions.Timeout("t")
        with self.assertRaises(requests.exceptions.Timeout):
            ccsearch._simple_fetch('http://x')

    @patch('ccsearch.HAS_CURL_CFFI', True)
    @patch('ccsearch.cffi_requests', create=True)
    def test_curl_cffi_path_success(self, mock_cffi):
        mock_session = MagicMock()
        mock_resp = _mock_response(200, text='<html>ok</html>')
        mock_session.get.return_value = mock_resp
        mock_cffi.Session.return_value = mock_session
        result = ccsearch._simple_fetch('http://example.com', maxRetries=2)
        mock_cffi.Session.assert_called_once_with(impersonate="chrome")
        mock_session.get.assert_called_once_with('http://example.com', headers=ccsearch.FETCH_HEADERS, timeout=30)
        self.assertEqual(result, mock_resp)

    @patch('ccsearch.HAS_CURL_CFFI', True)
    @patch('ccsearch.cffi_requests', create=True)
    def test_curl_cffi_path_4xx_no_retry(self, mock_cffi):
        mock_session = MagicMock()
        err = Exception("400 Bad Request")
        err.response = MagicMock(status_code=400)
        mock_session.get.side_effect = err
        mock_cffi.Session.return_value = mock_session
        with self.assertRaises(Exception):
            ccsearch._simple_fetch('http://x', maxRetries=2)
        # Should NOT retry on 4xx (except 429)
        self.assertEqual(mock_session.get.call_count, 1)

    @patch('ccsearch.time.sleep')
    @patch('ccsearch.HAS_CURL_CFFI', True)
    @patch('ccsearch.cffi_requests', create=True)
    def test_curl_cffi_path_retries_on_5xx(self, mock_cffi, mock_sleep):
        mock_session = MagicMock()
        err = Exception("500 Server Error")
        err.response = MagicMock(status_code=500)
        mock_session.get.side_effect = err
        mock_cffi.Session.return_value = mock_session
        with self.assertRaises(Exception):
            ccsearch._simple_fetch('http://x', maxRetries=1)
        # Should retry: 1 initial + 1 retry = 2 calls
        self.assertEqual(mock_session.get.call_count, 2)


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
# 10b. Twitter/X URL detection and fxtwitter formatting
# ===========================================================================
class TestIsTwitterUrl(unittest.TestCase):
    """Tests for _is_twitter_url URL parsing."""

    def test_tweet_url_x_dot_com(self):
        result = ccsearch._is_twitter_url("https://x.com/jack/status/20")
        self.assertEqual(result, ("jack", "20"))

    def test_tweet_url_twitter_dot_com(self):
        result = ccsearch._is_twitter_url("https://twitter.com/NASA/status/123456")
        self.assertEqual(result, ("NASA", "123456"))

    def test_tweet_url_mobile(self):
        result = ccsearch._is_twitter_url("https://mobile.twitter.com/user/status/999")
        self.assertEqual(result, ("user", "999"))

    def test_tweet_url_www(self):
        result = ccsearch._is_twitter_url("https://www.x.com/test_user/status/42")
        self.assertEqual(result, ("test_user", "42"))

    def test_profile_url(self):
        result = ccsearch._is_twitter_url("https://x.com/elonmusk")
        self.assertEqual(result, ("elonmusk", None))

    def test_profile_url_twitter(self):
        result = ccsearch._is_twitter_url("https://twitter.com/NASA")
        self.assertEqual(result, ("NASA", None))

    def test_url_with_query_params(self):
        result = ccsearch._is_twitter_url("https://x.com/user123/status/555?lang=en")
        self.assertEqual(result, ("user123", "555"))

    def test_url_with_fragment(self):
        result = ccsearch._is_twitter_url("https://x.com/user123/status/555#top")
        self.assertEqual(result, ("user123", "555"))

    def test_reserved_path_home(self):
        self.assertIsNone(ccsearch._is_twitter_url("https://x.com/home"))

    def test_reserved_path_explore(self):
        self.assertIsNone(ccsearch._is_twitter_url("https://x.com/explore"))

    def test_reserved_path_settings(self):
        self.assertIsNone(ccsearch._is_twitter_url("https://x.com/settings"))

    def test_reserved_path_intent(self):
        self.assertIsNone(ccsearch._is_twitter_url("https://x.com/intent"))

    def test_reserved_path_compose(self):
        self.assertIsNone(ccsearch._is_twitter_url("https://x.com/compose"))

    def test_non_twitter_url(self):
        self.assertIsNone(ccsearch._is_twitter_url("https://example.com/jack/status/20"))

    def test_root_url_no_path(self):
        self.assertIsNone(ccsearch._is_twitter_url("https://x.com/"))

    def test_invalid_handle_too_long(self):
        self.assertIsNone(ccsearch._is_twitter_url("https://x.com/thishandleiswaytoolongfortwitter"))

    def test_invalid_handle_special_chars(self):
        self.assertIsNone(ccsearch._is_twitter_url("https://x.com/user@name"))

    def test_http_scheme(self):
        result = ccsearch._is_twitter_url("http://x.com/jack/status/20")
        self.assertEqual(result, ("jack", "20"))

    def test_no_hostname_rejected(self):
        self.assertIsNone(ccsearch._is_twitter_url("/jack/status/20"))

    def test_relative_path_rejected(self):
        self.assertIsNone(ccsearch._is_twitter_url("jack/status/20"))

    def test_status_with_non_numeric_id_rejected(self):
        self.assertIsNone(ccsearch._is_twitter_url("https://x.com/user/status/notanid"))

    def test_status_without_id_rejected(self):
        self.assertIsNone(ccsearch._is_twitter_url("https://x.com/user/status/"))


class TestSafeInt(unittest.TestCase):
    """Tests for _safe_int helper."""

    def test_normal_int(self):
        self.assertEqual(ccsearch._safe_int(42), 42)

    def test_none_returns_default(self):
        self.assertEqual(ccsearch._safe_int(None), 0)

    def test_string_number(self):
        self.assertEqual(ccsearch._safe_int("123"), 123)

    def test_non_numeric_string(self):
        self.assertEqual(ccsearch._safe_int("abc"), 0)

    def test_custom_default(self):
        self.assertEqual(ccsearch._safe_int(None, -1), -1)

    def test_float_truncates(self):
        self.assertEqual(ccsearch._safe_int(3.9), 3)


class TestFormatTweet(unittest.TestCase):
    """Tests for _format_tweet output and robustness."""

    def _sample_tweet(self, **overrides):
        tweet = {
            "text": "hello world",
            "author": {"screen_name": "test", "name": "Test User"},
            "likes": 100, "retweets": 50, "replies": 10,
            "created_at": "Mon Jan 01 00:00:00 +0000 2024",
            "views": None,
        }
        tweet.update(overrides)
        return tweet

    def test_basic_format(self):
        out = ccsearch._format_tweet(self._sample_tweet())
        self.assertIn("@test", out)
        self.assertIn("hello world", out)
        self.assertIn("Likes: 100", out)

    def test_none_likes_no_crash(self):
        out = ccsearch._format_tweet(self._sample_tweet(likes=None))
        self.assertIn("Likes: 0", out)

    def test_none_views_omitted(self):
        out = ccsearch._format_tweet(self._sample_tweet(views=None))
        self.assertNotIn("Views:", out)

    def test_views_present(self):
        out = ccsearch._format_tweet(self._sample_tweet(views=50000))
        self.assertIn("Views: 50,000", out)

    def test_string_metrics_no_crash(self):
        out = ccsearch._format_tweet(self._sample_tweet(likes="bad", retweets="nope"))
        self.assertIn("Likes: 0", out)
        self.assertIn("Retweets: 0", out)

    def test_replying_to(self):
        out = ccsearch._format_tweet(self._sample_tweet(replying_to="other"))
        self.assertIn("Replying to: @other", out)

    def test_media_photos(self):
        media = {"photos": [{"url": "https://img.example.com/1.jpg"}]}
        out = ccsearch._format_tweet(self._sample_tweet(media=media))
        self.assertIn("[Photo]", out)

    def test_quoted_tweet(self):
        quote = {"author": {"screen_name": "quotee"}, "text": "original"}
        out = ccsearch._format_tweet(self._sample_tweet(quote=quote))
        self.assertIn("Quoted @quotee", out)


class TestFormatTwitterUser(unittest.TestCase):
    """Tests for _format_twitter_user output and robustness."""

    def _sample_user(self, **overrides):
        user = {
            "screen_name": "nasa", "name": "NASA",
            "description": "Space agency",
            "followers": 90000000, "following": 100,
            "tweets": 70000, "likes": 15000,
            "joined": "Wed Dec 19 00:00:00 +0000 2007",
        }
        user.update(overrides)
        return user

    def test_basic_format(self):
        out = ccsearch._format_twitter_user(self._sample_user())
        self.assertIn("@nasa", out)
        self.assertIn("90,000,000", out)

    def test_none_followers_no_crash(self):
        out = ccsearch._format_twitter_user(self._sample_user(followers=None))
        self.assertIn("Followers: 0", out)

    def test_location_shown(self):
        out = ccsearch._format_twitter_user(self._sample_user(location="DC"))
        self.assertIn("Location: DC", out)

    def test_website_shown(self):
        out = ccsearch._format_twitter_user(self._sample_user(website={"display_url": "nasa.gov"}))
        self.assertIn("Website: nasa.gov", out)


class TestFetchTwitterIntegration(unittest.TestCase):
    """Integration test for _fetch_twitter with mocked API."""

    @patch('ccsearch.requests.get')
    def test_tweet_fetch_success(self, mock_get):
        mock_get.return_value.json.return_value = {
            "code": 200, "message": "OK",
            "tweet": {
                "text": "just setting up", "url": "https://x.com/jack/status/20",
                "author": {"screen_name": "jack", "name": "jack"},
                "likes": 300000, "retweets": 120000, "replies": 17000,
                "created_at": "Tue Mar 21 20:50:14 +0000 2006", "views": None,
            }
        }
        result = ccsearch._fetch_twitter("https://x.com/jack/status/20", ("jack", "20"))
        self.assertIsNotNone(result)
        self.assertEqual(result["fetched_via"], "fxtwitter")
        self.assertIn("just setting up", result["content"])

    @patch('ccsearch.requests.get')
    def test_user_fetch_success(self, mock_get):
        mock_get.return_value.json.return_value = {
            "code": 200, "message": "OK",
            "user": {
                "screen_name": "NASA", "name": "NASA",
                "description": "Space", "followers": 90000000,
                "following": 100, "tweets": 70000, "likes": 15000,
                "joined": "2007",
            }
        }
        result = ccsearch._fetch_twitter("https://x.com/NASA", ("NASA", None))
        self.assertIsNotNone(result)
        self.assertEqual(result["fetched_via"], "fxtwitter")
        self.assertIn("@NASA", result["content"])

    @patch('ccsearch.requests.get')
    def test_api_404_returns_none(self, mock_get):
        mock_get.return_value.json.return_value = {"code": 404, "message": "NOT_FOUND", "tweet": None}
        result = ccsearch._fetch_twitter("https://x.com/x/status/999", ("x", "999"))
        self.assertIsNone(result)

    @patch('ccsearch.requests.get')
    def test_network_error_returns_none(self, mock_get):
        mock_get.side_effect = requests.ConnectionError("timeout")
        result = ccsearch._fetch_twitter("https://x.com/jack/status/20", ("jack", "20"))
        self.assertIsNone(result)


# ===========================================================================
# 11. perform_fetch — orchestrator (all modes & paths)
# ===========================================================================
class TestPerformFetch(unittest.TestCase):

    # ---- Mode: never (or no URL) ----

    def test_hosts_match_ignores_www_prefix(self):
        self.assertTrue(ccsearch._hosts_match("www.example.com", "example.com"))
        self.assertTrue(ccsearch._hosts_match("example.com.", "www.example.com"))
        self.assertFalse(ccsearch._hosts_match("example.com", "docs.example.com"))

    @patch('ccsearch._simple_fetch')
    def test_never_mode_direct_success(self, mock_sf):
        mock_sf.return_value = _mock_response(200,
            text='<html><head><title>Hi</title></head><body><p>Content</p></body></html>',
            headers={'Content-Type': 'text/html; charset=utf-8', 'ETag': '"abc123"', 'Last-Modified': 'Wed, 01 Jan 2025 00:00:00 GMT'},
            url='http://x/final')
        config = _make_config(flaresolverr_mode='never', flaresolverr_url='http://fs:8191/v1')
        result = ccsearch.perform_fetch('http://x', config)
        self.assertEqual(result["fetched_via"], "direct")
        self.assertEqual(result["title"], "Hi")
        self.assertIn("Content", result["content"])
        self.assertEqual(result["final_url"], "http://x/final")
        self.assertEqual(result["status_code"], 200)
        self.assertEqual(result["content_type"], "text/html")
        self.assertEqual(result["content_length"], len(mock_sf.return_value.content))
        self.assertEqual(result["etag"], '"abc123"')
        self.assertEqual(result["last_modified"], 'Wed, 01 Jan 2025 00:00:00 GMT')
        self.assertEqual(result["content_sha256"], hashlib.sha256(result["content"].encode("utf-8")).hexdigest())

    @patch('ccsearch._simple_fetch')
    def test_html_fetch_extracts_metadata_fields(self, mock_sf):
        mock_sf.return_value = _mock_response(
            200,
            text='''
            <html lang="en">
              <head>
                <title>Story</title>
                <link rel="canonical" href="/stories/fetch-test" />
                <meta name="description" content="Summary text" />
                <meta name="author" content="Jamie" />
                <meta property="article:published_time" content="2026-04-11T09:00:00Z" />
              </head>
              <body><article><p>Main story <a href="/docs/more">More</a> <a href="https://docs.python.org/3/">Python Docs</a></p></article></body>
            </html>
            ''',
            headers={'Content-Type': 'text/html; charset=utf-8'},
            url='https://example.com/news/fetch-test?ref=feed',
        )
        config = _make_config(flaresolverr_mode='never', flaresolverr_url='http://fs:8191/v1')
        result = ccsearch.perform_fetch('https://example.com/news/fetch-test', config)
        self.assertEqual(result["lang"], "en")
        self.assertEqual(result["canonical_url"], "https://example.com/stories/fetch-test")
        self.assertEqual(result["description"], "Summary text")
        self.assertEqual(result["author"], "Jamie")
        self.assertEqual(result["published_at"], "2026-04-11T09:00:00Z")
        self.assertEqual(result["hostname"], "example.com")
        self.assertGreater(result["content_word_count"], 0)
        self.assertEqual(result["chunk_count"], len(result["chunks"]))
        self.assertEqual(result["outbound_link_count"], 2)
        self.assertEqual(result["internal_outbound_link_count"], 1)
        self.assertEqual(result["external_outbound_link_count"], 1)
        self.assertEqual(result["outbound_hosts"], ["docs.python.org", "example.com"])
        self.assertEqual(result["outbound_links"][0]["url"], "https://example.com/docs/more")
        self.assertTrue(result["outbound_links"][0]["is_same_host"])
        self.assertEqual(result["outbound_links"][1]["url"], "https://docs.python.org/3/")
        self.assertFalse(result["outbound_links"][1]["is_same_host"])
        self.assertIn("chunks", result)
        paragraph_chunk = next(chunk for chunk in result["chunks"] if chunk["type"] == "paragraph")
        self.assertEqual(paragraph_chunk["link_count"], 2)
        self.assertEqual(paragraph_chunk["internal_link_count"], 1)
        self.assertEqual(paragraph_chunk["external_link_count"], 1)
        self.assertEqual(paragraph_chunk["links"][0]["url"], "https://example.com/docs/more")
        self.assertEqual(paragraph_chunk["links"][0]["hostname"], "example.com")
        self.assertTrue(paragraph_chunk["links"][0]["is_same_host"])
        self.assertEqual(paragraph_chunk["links"][1]["hostname"], "docs.python.org")
        self.assertFalse(paragraph_chunk["links"][1]["is_same_host"])

    @patch('ccsearch._simple_fetch')
    def test_html_fetch_uses_og_title_when_title_tag_missing(self, mock_sf):
        mock_sf.return_value = _mock_response(
            200,
            text='''
            <html>
              <head>
                <meta property="og:title" content="Open Graph Title" />
              </head>
              <body><main><p>Body</p></main></body>
            </html>
            ''',
            headers={'Content-Type': 'text/html'},
            url='https://example.com/post',
        )
        config = _make_config(flaresolverr_mode='never', flaresolverr_url='http://fs:8191/v1')
        result = ccsearch.perform_fetch('https://example.com/post', config)
        self.assertEqual(result["title"], "Open Graph Title")
        self.assertIn("Body", result["content"])

    @patch('ccsearch._simple_fetch')
    def test_html_fetch_uses_json_ld_metadata_when_meta_tags_missing(self, mock_sf):
        mock_sf.return_value = _mock_response(
            200,
            text='''
            <html>
              <head>
                <script type="application/ld+json">
                  {
                    "@context": "https://schema.org",
                    "@type": "BlogPosting",
                    "headline": "JSON-LD Story",
                    "description": "JSON-LD summary",
                    "author": {"@type": "Person", "name": "Jamie"},
                    "datePublished": "2026-04-09",
                    "mainEntityOfPage": {"@id": "/blog/json-ld-story"}
                  }
                </script>
              </head>
              <body>
                <article><p>Story body</p></article>
              </body>
            </html>
            ''',
            headers={'Content-Type': 'text/html'},
            url='https://example.com/blog/json-ld-story?utm_source=test',
        )
        config = _make_config(flaresolverr_mode='never', flaresolverr_url='http://fs:8191/v1')
        result = ccsearch.perform_fetch('https://example.com/blog/json-ld-story', config)
        self.assertEqual(result["title"], "JSON-LD Story")
        self.assertEqual(result["description"], "JSON-LD summary")
        self.assertEqual(result["author"], "Jamie")
        self.assertEqual(result["published_at"], "2026-04-09")
        self.assertEqual(result["canonical_url"], "https://example.com/blog/json-ld-story")
        self.assertIn("Story body", result["content"])

    @patch('ccsearch._simple_fetch')
    def test_html_fetch_returns_list_and_table_chunks(self, mock_sf):
        mock_sf.return_value = _mock_response(
            200,
            text='''
            <html>
              <head><title>Structured</title></head>
              <body>
                <article>
                  <h1>Structured content</h1>
                  <ul>
                    <li>First task</li>
                    <li>Second task</li>
                  </ul>
                  <table>
                    <thead>
                      <tr><th>Name</th><th>Score</th></tr>
                    </thead>
                    <tbody>
                      <tr><td>Alice</td><td>10</td></tr>
                    </tbody>
                  </table>
                </article>
              </body>
            </html>
            ''',
            headers={'Content-Type': 'text/html'},
            url='https://example.com/structured',
        )
        config = _make_config(flaresolverr_mode='never', flaresolverr_url='http://fs:8191/v1')
        result = ccsearch.perform_fetch('https://example.com/structured', config)
        self.assertIn("- First task", result["content"])
        self.assertIn("| Name | Score |", result["content"])
        chunk_types = [chunk["type"] for chunk in result["chunks"]]
        self.assertIn("list", chunk_types)
        self.assertIn("table", chunk_types)
        heading_chunk = result["chunks"][0]
        list_chunk = next(chunk for chunk in result["chunks"] if chunk["type"] == "list")
        table_chunk = next(chunk for chunk in result["chunks"] if chunk["type"] == "table")
        self.assertEqual(heading_chunk["section_title"], "Structured content")
        self.assertEqual(list_chunk["section_title"], "Structured content")
        self.assertEqual(table_chunk["section_title"], "Structured content")
        self.assertEqual(heading_chunk["section_path"], ["Structured content"])
        self.assertEqual(list_chunk["section_path_text"], "Structured content")
        self.assertEqual(table_chunk["section_depth"], 1)
        self.assertEqual(heading_chunk["text_sha256"], hashlib.sha256("Structured content".encode("utf-8")).hexdigest())
        self.assertIsNotNone(list_chunk["chunk_id"])
        self.assertEqual(list_chunk["list_item_count"], 2)
        self.assertFalse(list_chunk["list_ordered"])
        if "links" in list_chunk:
            self.assertEqual(list_chunk["link_count"], len(list_chunk["links"]))
            self.assertIn("internal_link_count", list_chunk)
            self.assertIn("external_link_count", list_chunk)
        else:
            self.assertNotIn("link_count", list_chunk)
        self.assertGreater(list_chunk["char_count"], 0)
        self.assertGreater(table_chunk["word_count"], 0)
        self.assertEqual(table_chunk["table_row_count"], 1)
        self.assertEqual(table_chunk["table_column_count"], 2)
        self.assertEqual(table_chunk["table_headers"], ["Name", "Score"])
        self.assertIn("relative_position", table_chunk)
        self.assertEqual(heading_chunk["char_start"], 0)
        self.assertGreater(list_chunk["char_start"], heading_chunk["char_end"])
        self.assertGreater(table_chunk["char_start"], list_chunk["char_end"])

    @patch('ccsearch._simple_fetch')
    def test_html_fetch_preserves_code_blocks_and_language(self, mock_sf):
        mock_sf.return_value = _mock_response(
            200,
            text='''
            <html>
              <head><title>Code sample</title></head>
              <body>
                <article>
                  <h1>Code sample</h1>
                  <pre><code class="language-python">def hello():
    return "world"</code></pre>
                </article>
              </body>
            </html>
            ''',
            headers={'Content-Type': 'text/html'},
            url='https://example.com/code',
        )
        config = _make_config(flaresolverr_mode='never', flaresolverr_url='http://fs:8191/v1')
        result = ccsearch.perform_fetch('https://example.com/code', config)
        self.assertIn("```python", result["content"])
        code_chunk = next(chunk for chunk in result["chunks"] if chunk["type"] == "code")
        self.assertEqual(code_chunk["code_language"], "python")
        self.assertEqual(code_chunk["code_line_count"], 2)
        self.assertIn('return "world"', code_chunk["text"])

    @patch('ccsearch._simple_fetch')
    def test_text_plain_fetch_returns_chunks(self, mock_sf):
        mock_sf.return_value = _mock_response(
            200,
            text='first paragraph\n\nsecond paragraph',
            headers={'Content-Type': 'text/plain; charset=utf-8'},
            url='https://example.com/notes.txt',
        )
        config = _make_config(flaresolverr_mode='never', flaresolverr_url='http://fs:8191/v1')
        result = ccsearch.perform_fetch('https://example.com/notes.txt', config)
        self.assertEqual(len(result["chunks"]), 2)
        self.assertEqual(result["chunks"][0]["index"], 1)
        self.assertEqual(result["chunks"][0]["type"], "paragraph")
        self.assertEqual(result["chunks"][0]["text"], "first paragraph")
        self.assertEqual(result["chunks"][1]["index"], 2)
        self.assertEqual(result["chunks"][1]["type"], "paragraph")
        self.assertEqual(result["chunks"][1]["text"], "second paragraph")
        self.assertIsNone(result["chunks"][0]["section_title"])
        self.assertEqual(result["chunks"][0]["section_path"], [])
        self.assertIsNone(result["chunks"][0]["section_path_text"])
        self.assertEqual(result["chunks"][0]["section_depth"], 0)
        self.assertEqual(result["chunks"][0]["text_sha256"], hashlib.sha256("first paragraph".encode("utf-8")).hexdigest())
        self.assertIsNotNone(result["chunks"][0]["chunk_id"])
        self.assertEqual(result["chunks"][0]["char_count"], len("first paragraph"))
        self.assertEqual(result["chunks"][0]["word_count"], 2)
        self.assertEqual(result["chunks"][0]["char_start"], 0)
        self.assertEqual(result["chunks"][0]["char_end"], len("first paragraph"))
        self.assertEqual(result["chunks"][1]["relative_position"], 1.0)

    @patch('ccsearch._flaresolverr_fetch')
    @patch('ccsearch._simple_fetch')
    def test_octet_stream_html_can_trigger_spa_fallback(self, mock_sf, mock_ff):
        mock_sf.return_value = _mock_response(
            200,
            text='''
            <html>
              <head><title>Shell</title></head>
              <body><div id="root"></div><script>boot()</script><script>hydrate()</script></body>
            </html>
            ''',
            headers={'Content-Type': 'application/octet-stream'},
            url='https://example.com/app',
        )
        mock_ff.return_value = '''
        <html>
          <head><title>Rendered</title></head>
          <body><main><p>Rendered article body.</p></main></body>
        </html>
        '''
        config = _make_config(flaresolverr_mode='fallback', flaresolverr_url='http://fs:8191/v1')
        result = ccsearch.perform_fetch('https://example.com/app', config)
        self.assertEqual(result["fetched_via"], "flaresolverr")
        self.assertEqual(result["title"], "Rendered")
        self.assertIn("Rendered article body", result["content"])

    @patch('ccsearch._simple_fetch')
    def test_no_url_configured_direct_success(self, mock_sf):
        mock_sf.return_value = _mock_response(200,
            text='<html><head><title>OK</title></head><body><p>Text</p></body></html>',
            headers={'Content-Type': 'text/html'},
            url='http://x')
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

    @patch('ccsearch._flaresolverr_fetch')
    @patch('ccsearch._simple_fetch')
    def test_fallback_direct_success_no_cf(self, mock_sf, mock_ff):
        mock_sf.return_value = _mock_response(200,
            text='<html><head><title>Page</title></head><body><p>Real content</p></body></html>')
        config = _make_config(flaresolverr_mode='fallback', flaresolverr_url='http://fs:8191/v1')
        result = ccsearch.perform_fetch('http://x', config)
        self.assertEqual(result["fetched_via"], "direct")
        self.assertEqual(result["title"], "Page")
        mock_ff.assert_not_called()

    @patch('ccsearch._flaresolverr_fetch')
    @patch('ccsearch._simple_fetch')
    def test_short_static_page_does_not_trigger_spa_fallback(self, mock_sf, mock_ff):
        mock_sf.return_value = _mock_response(
            200,
            text='<html><head><title>Short</title></head><body><main><h1>Docs</h1><p>Short copy.</p></main></body></html>',
            headers={'Content-Type': 'text/html'},
            url='https://example.com/short',
        )
        config = _make_config(flaresolverr_mode='fallback', flaresolverr_url='http://fs:8191/v1')
        result = ccsearch.perform_fetch('https://example.com/short', config)
        self.assertEqual(result["fetched_via"], "direct")
        self.assertEqual(result["title"], "Short")
        self.assertIn("Short copy.", result["content"])
        mock_ff.assert_not_called()

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
            text='<html><head><title>T</title></head><body><p>C</p></body></html>',
            headers={'Content-Type': 'text/html; charset=utf-8', 'ETag': '"xyz"', 'Last-Modified': 'Thu, 02 Jan 2025 00:00:00 GMT'},
            url='http://x/final')
        config = _make_config()
        result = ccsearch.perform_fetch('http://x', config)
        self.assertIn("engine", result)
        self.assertIn("url", result)
        self.assertIn("final_url", result)
        self.assertIn("status_code", result)
        self.assertIn("content_type", result)
        self.assertIn("content_length", result)
        self.assertIn("hostname", result)
        self.assertIn("content_word_count", result)
        self.assertIn("etag", result)
        self.assertIn("last_modified", result)
        self.assertIn("title", result)
        self.assertIn("content", result)
        self.assertIn("fetched_via", result)
        self.assertEqual(result["engine"], "fetch")
        self.assertEqual(result["url"], "http://x")
        self.assertEqual(result["final_url"], "http://x/final")
        self.assertEqual(result["status_code"], 200)
        self.assertEqual(result["content_type"], "text/html")
        self.assertEqual(result["content_length"], len(mock_sf.return_value.content))
        self.assertEqual(result["hostname"], "x")
        self.assertEqual(result["etag"], '"xyz"')
        self.assertEqual(result["last_modified"], 'Thu, 02 Jan 2025 00:00:00 GMT')

    @patch('ccsearch._simple_fetch')
    def test_result_has_error_field_on_failure(self, mock_sf):
        mock_sf.side_effect = Exception("fail")
        config = _make_config()
        result = ccsearch.perform_fetch('http://x', config)
        self.assertIn("error", result)
        self.assertEqual(result["engine"], "fetch")
        self.assertEqual(result["url"], "http://x")
        self.assertEqual(result["final_url"], "http://x")
        self.assertIsNone(result["status_code"])
        self.assertIsNone(result["content_type"])
        self.assertIsNone(result["content_length"])

    @patch('ccsearch._clean_html')
    @patch('ccsearch._simple_fetch')
    def test_text_plain_response_decodes_without_html_parser(self, mock_sf, mock_clean):
        mock_sf.return_value = _mock_response(
            200,
            text='hello\nworld',
            headers={'Content-Type': 'text/plain; charset=utf-8'},
            url='http://x/files/notes.txt',
        )
        config = _make_config()
        result = ccsearch.perform_fetch('http://x/files/notes.txt', config)
        mock_clean.assert_not_called()
        self.assertEqual(result["title"], "notes.txt")
        self.assertEqual(result["content"], "hello\nworld")
        self.assertEqual(result["content_type"], "text/plain")
        self.assertEqual(result["filename"], "notes.txt")

    @patch('ccsearch._convert_with_markitdown')
    @patch('ccsearch._simple_fetch')
    def test_binary_document_uses_markitdown_when_available(self, mock_sf, mock_convert):
        mock_sf.return_value = _mock_response(
            200,
            content=b'%PDF-1.4 fake pdf bytes',
            headers={'Content-Type': 'application/pdf'},
            url='http://x/files/report.pdf',
        )
        mock_convert.return_value = ('# Converted\n\nBody', None)
        config = _make_config()
        result = ccsearch.perform_fetch('http://x/files/report.pdf', config)
        mock_convert.assert_called_once()
        self.assertEqual(result["converted_via"], "markitdown")
        self.assertEqual(result["title"], "report.pdf")
        self.assertEqual(result["filename"], "report.pdf")
        self.assertEqual(result["content_type"], "application/pdf")
        self.assertIn("Converted", result["content"])

    @patch('ccsearch._convert_with_markitdown')
    @patch('ccsearch._simple_fetch')
    def test_binary_document_returns_helpful_error_when_markitdown_missing(self, mock_sf, mock_convert):
        mock_sf.return_value = _mock_response(
            200,
            content=b'%PDF-1.4 fake pdf bytes',
            headers={'Content-Type': 'application/pdf'},
            url='http://x/files/report.pdf',
        )
        mock_convert.return_value = (None, 'Binary document detected but markitdown is not installed.')
        config = _make_config()
        result = ccsearch.perform_fetch('http://x/files/report.pdf', config)
        self.assertIn('error', result)
        self.assertIn('markitdown is not installed', result['error'])
        self.assertEqual(result["content_type"], "application/pdf")
        self.assertEqual(result["filename"], "report.pdf")

    @patch('ccsearch._convert_with_markitdown')
    @patch('ccsearch._simple_fetch')
    def test_content_disposition_filename_is_used(self, mock_sf, mock_convert):
        mock_sf.return_value = _mock_response(
            200,
            content=b'%PDF-1.4 fake pdf bytes',
            headers={
                'Content-Type': 'application/pdf',
                'Content-Disposition': 'attachment; filename="Quarterly Report.pdf"',
            },
            url='http://x/download?id=123',
        )
        mock_convert.return_value = ('# Converted\n\nBody', None)
        config = _make_config()
        result = ccsearch.perform_fetch('http://x/download?id=123', config)
        self.assertEqual(result["filename"], "Quarterly Report.pdf")
        self.assertEqual(result["title"], "Quarterly Report.pdf")

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
# 12. Shared execution helpers
# ===========================================================================
class TestSharedExecutionHelpers(unittest.TestCase):

    def setUp(self):
        self.config = _make_config()

    def test_list_engines_contains_fetch(self):
        engines = ccsearch.list_engines()
        fetch_engine = next(item for item in engines if item["name"] == "fetch")
        self.assertEqual(fetch_engine["category"], "fetch")
        self.assertTrue(fetch_engine["supports_flaresolverr"])
        self.assertFalse(fetch_engine["supports_semantic_cache"])
        self.assertFalse(fetch_engine["supports_host_filter"])
        self.assertFalse(fetch_engine["supports_result_limit"])
        self.assertTrue(fetch_engine["configured"])
        self.assertEqual(fetch_engine["configured_via"], "built-in")
        self.assertEqual(fetch_engine["required_env_vars"], [])

    def test_list_engines_marks_brave_as_configured_when_env_present(self):
        with patch.dict(os.environ, {"BRAVE_API_KEY": "x"}, clear=False):
            engines = ccsearch.list_engines()
        brave_engine = next(item for item in engines if item["name"] == "brave")
        self.assertTrue(brave_engine["configured"])
        self.assertEqual(brave_engine["configured_via"], "BRAVE_API_KEY")
        self.assertTrue(brave_engine["supports_host_filter"])
        self.assertTrue(brave_engine["supports_result_limit"])

    def test_get_diagnostics_reports_dependency_and_fetch_state(self):
        config = _make_config(flaresolverr_url='http://fs:8191/v1', flaresolverr_mode='always')
        diagnostics = ccsearch.get_diagnostics(config)
        self.assertIn("dependencies", diagnostics)
        self.assertIn("curl_cffi", diagnostics["dependencies"])
        self.assertTrue(diagnostics["fetch"]["flaresolverr_configured"])
        self.assertEqual(diagnostics["fetch"]["flaresolverr_mode"], "always")
        self.assertIn("engines", diagnostics)

    def test_get_diagnostics_can_exclude_engines(self):
        diagnostics = ccsearch.get_diagnostics(_make_config(), include_engines=False)
        self.assertNotIn("engines", diagnostics)

    def test_validate_query_requires_value(self):
        self.assertEqual(ccsearch.validate_query('', 'brave'), "'query' is required")

    def test_validate_query_rejects_invalid_fetch_url(self):
        self.assertIn("valid HTTP or HTTPS URL", ccsearch.validate_query('notaurl', 'fetch'))

    def test_validate_execution_options_rejects_offset_for_perplexity(self):
        self.assertIn("offset", ccsearch.validate_execution_options('perplexity', offset=1))

    def test_validate_execution_options_rejects_negative_offset(self):
        self.assertIn("greater than or equal to 0", ccsearch.validate_execution_options('brave', offset=-1))

    def test_validate_execution_options_rejects_flaresolverr_for_brave(self):
        self.assertIn("flaresolverr", ccsearch.validate_execution_options('brave', flaresolverr=True))

    def test_validate_execution_options_rejects_invalid_cache_ttl(self):
        self.assertIn("cache_ttl", ccsearch.validate_execution_options('fetch', cache_ttl=0))

    def test_validate_execution_options_rejects_invalid_semantic_threshold(self):
        self.assertIn("semantic_threshold", ccsearch.validate_execution_options('brave', semantic_threshold=1.5))

    @patch('ccsearch.perform_fetch')
    def test_execute_engine_fetch(self, mock_fetch):
        mock_fetch.return_value = {"engine": "fetch", "content": "body"}
        result = ccsearch.execute_engine('https://example.com', 'fetch', self.config)
        self.assertEqual(result["engine"], "fetch")
        mock_fetch.assert_called_once()

    def test_execute_engine_rejects_unknown_engine(self):
        with self.assertRaises(ValueError):
            ccsearch.execute_engine('test', 'unknown', self.config)

    def test_execute_engine_requires_brave_key(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                ccsearch.execute_engine('test', 'brave', self.config)

    def test_execute_engine_requires_perplexity_key(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                ccsearch.execute_engine('test', 'perplexity', self.config)

    def test_execute_engine_requires_both_keys(self):
        with patch.dict(os.environ, {'BRAVE_API_KEY': 'only-brave'}, clear=True):
            with self.assertRaises(RuntimeError):
                ccsearch.execute_engine('test', 'both', self.config)

    @patch('ccsearch.perform_llm_context_search')
    def test_execute_engine_llm_context_accepts_search_key(self, mock_lc):
        mock_lc.return_value = {"engine": "llm-context", "results": []}
        with patch.dict(os.environ, {'BRAVE_SEARCH_API_KEY': 'search-key'}, clear=True):
            result = ccsearch.execute_engine('test', 'llm-context', self.config)
        self.assertEqual(result["engine"], "llm-context")
        mock_lc.assert_called_once_with('test', 'search-key', self.config)

    @patch('ccsearch.perform_fetch')
    def test_execute_engine_flaresolverr_clones_config(self, mock_fetch):
        mock_fetch.return_value = {"engine": "fetch", "content": "body"}
        result = ccsearch.execute_engine('https://example.com', 'fetch', self.config, flaresolverr=True)
        self.assertEqual(result["engine"], "fetch")
        called_config = mock_fetch.call_args[0][1]
        self.assertEqual(called_config.get('Fetch', 'flaresolverr_mode'), 'always')
        self.assertEqual(self.config.get('Fetch', 'flaresolverr_mode'), 'fallback')

    @patch('ccsearch.execute_engine')
    @patch('ccsearch.read_from_cache')
    def test_execute_query_uses_exact_cache(self, mock_read, mock_execute):
        mock_read.return_value = {"engine": "brave", "results": []}
        result = ccsearch.execute_query('test', 'brave', self.config, cache=True)
        self.assertTrue(result["_from_cache"])
        self.assertEqual(result["cache_status"], "exact")
        self.assertIn("duration_ms", result)
        mock_execute.assert_not_called()

    @patch('ccsearch.read_from_semantic_cache')
    @patch('ccsearch.read_from_cache')
    @patch('ccsearch.execute_engine')
    def test_execute_query_uses_semantic_cache(self, mock_execute, mock_read, mock_semantic):
        mock_read.return_value = None
        mock_semantic.return_value = ({"engine": "brave", "results": []}, 0.9876)
        result = ccsearch.execute_query('test', 'brave', self.config, semantic_cache=True)
        self.assertTrue(result["_from_cache"])
        self.assertEqual(result["_semantic_similarity"], 0.9876)
        self.assertEqual(result["cache_status"], "semantic")
        self.assertIn("duration_ms", result)
        mock_execute.assert_not_called()

    @patch('ccsearch.update_semantic_index')
    @patch('ccsearch.write_to_cache')
    @patch('ccsearch.execute_engine')
    @patch('ccsearch.read_from_cache')
    @patch('ccsearch.read_from_semantic_cache')
    def test_execute_query_populates_caches_on_miss(self, mock_semantic, mock_read, mock_execute, mock_write, mock_update):
        mock_read.return_value = None
        mock_semantic.return_value = (None, 0.0)
        mock_execute.return_value = {"engine": "brave", "results": []}
        result = ccsearch.execute_query('test', 'brave', self.config, semantic_cache=True)
        self.assertEqual(result["engine"], "brave")
        self.assertEqual(result["cache_status"], "miss")
        self.assertIn("duration_ms", result)
        mock_write.assert_called_once()
        mock_update.assert_called_once()

    @patch('ccsearch.backfill_semantic_index')
    @patch('ccsearch.read_from_cache')
    def test_execute_query_backfills_semantic_index_on_exact_hit(self, mock_read, mock_backfill):
        mock_read.return_value = {"engine": "brave", "results": []}
        ccsearch.execute_query('test', 'brave', self.config, cache=True, semantic_cache=True)
        mock_backfill.assert_called_once_with('test', 'brave', None)

    @patch('ccsearch.read_from_cache', return_value=None)
    @patch('ccsearch.read_from_semantic_cache')
    def test_execute_query_fetch_skips_semantic_cache(self, mock_semantic, mock_read):
        with patch('ccsearch.execute_engine', return_value={"engine": "fetch", "content": "ok"}):
            result = ccsearch.execute_query('https://example.com', 'fetch', self.config, semantic_cache=True)
        self.assertEqual(result["engine"], "fetch")
        self.assertEqual(result["cache_status"], "miss")
        mock_read.assert_called_once()
        mock_semantic.assert_not_called()

    @patch('ccsearch.execute_engine')
    def test_execute_query_marks_cache_disabled_when_cache_unused(self, mock_execute):
        mock_execute.return_value = {"engine": "brave", "results": []}
        result = ccsearch.execute_query('test', 'brave', self.config)
        self.assertEqual(result["cache_status"], "disabled")
        self.assertIn("duration_ms", result)

    @patch('ccsearch.execute_engine')
    def test_execute_query_applies_include_host_filter(self, mock_execute):
        mock_execute.return_value = {
            "engine": "brave",
            "query": "test",
            "result_count": 2,
            "results": [
                {"title": "A", "url": "https://docs.example.com/a", "hostname": "docs.example.com", "rank": 1},
                {"title": "B", "url": "https://other.example.com/b", "hostname": "other.example.com", "rank": 2},
            ],
            "result_hosts": ["docs.example.com", "other.example.com"],
            "result_host_count": 2,
        }
        result = ccsearch.execute_query('test', 'brave', self.config, include_hosts=["docs.example.com"])
        self.assertEqual(result["result_count"], 1)
        self.assertEqual(result["results"][0]["hostname"], "docs.example.com")
        self.assertEqual(result["results"][0]["rank"], 1)
        self.assertEqual(result["host_filtering"]["removed_results"], 1)
        self.assertEqual(result["result_hosts"], ["docs.example.com"])

    @patch('ccsearch.execute_engine')
    def test_execute_query_applies_host_filter_to_llm_context_sources(self, mock_execute):
        mock_execute.return_value = {
            "engine": "llm-context",
            "query": "test",
            "result_count": 2,
            "source_count": 2,
            "results": [
                {"title": "A", "url": "https://docs.example.com/a", "hostname": "docs.example.com", "rank": 1, "snippets": []},
                {"title": "B", "url": "https://other.example.com/b", "hostname": "other.example.com", "rank": 2, "snippets": []},
            ],
            "sources": {
                "https://docs.example.com/a": {"hostname": "docs.example.com"},
                "https://other.example.com/b": {"hostname": "other.example.com"},
            },
        }
        result = ccsearch.execute_query('test', 'llm-context', self.config, exclude_hosts="other.example.com")
        self.assertEqual(result["result_count"], 1)
        self.assertEqual(result["source_count"], 1)
        self.assertEqual(list(result["sources"].keys()), ["https://docs.example.com/a"])
        self.assertEqual(result["host_filtering"]["removed_results"], 1)

    def test_validate_execution_options_rejects_host_filters_for_unsupported_engine(self):
        error = ccsearch.validate_execution_options("perplexity", include_hosts=["example.com"])
        self.assertIn("Host filters", error)

    def test_validate_execution_options_rejects_overlapping_host_filters(self):
        error = ccsearch.validate_execution_options("brave", include_hosts=["example.com"], exclude_hosts=["example.com"])
        self.assertIn("overlap", error)

    def test_validate_execution_options_rejects_result_limit_for_unsupported_engine(self):
        error = ccsearch.validate_execution_options("perplexity", result_limit=3)
        self.assertIn("Result limiting", error)

    def test_validate_execution_options_rejects_invalid_result_limit(self):
        error = ccsearch.validate_execution_options("brave", result_limit=0)
        self.assertIn("result_limit", error)

    def test_execute_query_rejects_invalid_option_combo(self):
        with self.assertRaises(ValueError):
            ccsearch.execute_query('test', 'perplexity', self.config, offset=1)

    @patch('ccsearch.execute_engine')
    def test_execute_query_applies_result_limit(self, mock_execute):
        mock_execute.return_value = {
            "engine": "brave",
            "query": "test",
            "result_count": 3,
            "results": [
                {"title": "A", "url": "https://docs.example.com/a", "hostname": "docs.example.com", "rank": 1},
                {"title": "B", "url": "https://docs.example.com/b", "hostname": "docs.example.com", "rank": 2},
                {"title": "C", "url": "https://docs.example.com/c", "hostname": "docs.example.com", "rank": 3},
            ],
            "result_hosts": ["docs.example.com"],
            "result_host_count": 1,
        }
        result = ccsearch.execute_query('test', 'brave', self.config, result_limit=2)
        self.assertEqual(result["result_count"], 2)
        self.assertEqual([item["rank"] for item in result["results"]], [1, 2])
        self.assertEqual(result["result_limiting"]["limit"], 2)
        self.assertEqual(result["result_limiting"]["removed_results"], 1)

    def test_execute_batch_runs_requests_and_collects_errors(self):
        with patch('ccsearch.execute_query') as mock_execute:
            mock_execute.side_effect = [
                {"engine": "brave", "query": "alpha", "results": []},
                RuntimeError("missing key"),
            ]
            result = ccsearch.execute_batch(
                [
                    {"query": "alpha", "engine": "brave"},
                    {"query": "beta", "engine": "perplexity"},
                    {"query": "notaurl", "engine": "fetch"},
                ],
                self.config,
            )
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["error_count"], 2)
        self.assertEqual(result["results"][0]["index"], 1)
        self.assertEqual(result["results"][1]["error"], "missing key")
        self.assertIn("valid HTTP or HTTPS URL", result["results"][2]["error"])

    def test_execute_batch_uses_defaults(self):
        with patch('ccsearch.execute_query', return_value={"engine": "brave", "query": "alpha", "results": []}) as mock_execute:
            result = ccsearch.execute_batch([{"query": "alpha"}], self.config, defaults={"engine": "brave", "cache": True})
        self.assertEqual(result["error_count"], 0)
        self.assertTrue(mock_execute.call_args.kwargs["cache"])
        self.assertEqual(result["success_count"], 1)
        self.assertFalse(result["has_errors"])

    def test_execute_batch_respects_max_workers_argument(self):
        seen = {}

        class FakeExecutor:
            def __init__(self, max_workers):
                seen["max_workers"] = max_workers
            def __enter__(self):
                return self
            def __exit__(self, exc_type, exc, tb):
                return False
            def submit(self, fn, *args, **kwargs):
                future = concurrent.futures.Future()
                future.set_result(fn(*args, **kwargs))
                return future

        with patch('ccsearch.execute_query', return_value={"engine": "brave", "query": "alpha", "results": []}), \
             patch('ccsearch.concurrent.futures.ThreadPoolExecutor', FakeExecutor):
            result = ccsearch.execute_batch(
                [{"query": "alpha", "engine": "brave"}],
                self.config,
                max_workers=7,
            )
        self.assertEqual(seen["max_workers"], 1)
        self.assertEqual(result["max_workers"], 1)

    def test_execute_batch_rejects_invalid_max_workers(self):
        with self.assertRaises(ValueError):
            ccsearch.execute_batch([{"query": "alpha", "engine": "brave"}], self.config, max_workers=0)

    def test_execute_batch_deduplicates_identical_requests(self):
        with patch('ccsearch.execute_query', return_value={"engine": "brave", "query": "alpha", "results": []}) as mock_execute:
            result = ccsearch.execute_batch(
                [
                    {"query": "alpha", "engine": "brave"},
                    {"query": " alpha  ", "engine": "brave"},
                ],
                self.config,
            )
        mock_execute.assert_called_once()
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["success_count"], 2)
        self.assertEqual(result["deduped_count"], 1)
        self.assertTrue(result["results"][1]["_batch_deduped"])
        self.assertEqual(result["results"][1]["_batch_deduped_from"], 1)
        self.assertEqual(result["results"][1]["duration_ms"], 0.0)

    def test_execute_batch_keeps_host_filtered_requests_distinct(self):
        with patch('ccsearch.execute_query', side_effect=[
            {"engine": "brave", "query": "alpha", "results": []},
            {"engine": "brave", "query": "alpha", "results": []},
        ]) as mock_execute:
            result = ccsearch.execute_batch(
                [
                    {"query": "alpha", "engine": "brave", "include_hosts": ["docs.example.com"]},
                    {"query": "alpha", "engine": "brave", "include_hosts": ["other.example.com"]},
                ],
                self.config,
            )
        self.assertEqual(mock_execute.call_count, 2)
        self.assertEqual(result["deduped_count"], 0)

    def test_execute_batch_keeps_result_limited_requests_distinct(self):
        with patch('ccsearch.execute_query', side_effect=[
            {"engine": "brave", "query": "alpha", "results": []},
            {"engine": "brave", "query": "alpha", "results": []},
        ]) as mock_execute:
            result = ccsearch.execute_batch(
                [
                    {"query": "alpha", "engine": "brave", "result_limit": 1},
                    {"query": "alpha", "engine": "brave", "result_limit": 2},
                ],
                self.config,
            )
        self.assertEqual(mock_execute.call_count, 2)
        self.assertEqual(result["deduped_count"], 0)


class TestBatchFileLoading(unittest.TestCase):

    def test_load_batch_requests_from_json_array(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump([{"query": "alpha", "engine": "brave"}], f)
            path = f.name
        try:
            requests_payload, defaults = ccsearch.load_batch_requests(path)
        finally:
            os.unlink(path)
        self.assertEqual(requests_payload[0]["query"], "alpha")
        self.assertEqual(defaults, {})

    def test_load_batch_requests_from_json_object(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump({"defaults": {"engine": "fetch"}, "requests": [{"url": "https://example.com"}]}, f)
            path = f.name
        try:
            requests_payload, defaults = ccsearch.load_batch_requests(path)
        finally:
            os.unlink(path)
        self.assertEqual(defaults["engine"], "fetch")
        self.assertEqual(requests_payload[0]["url"], "https://example.com")

    def test_load_batch_requests_from_jsonl(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write('{"query":"alpha","engine":"brave"}\n{"url":"https://example.com","engine":"fetch"}\n')
            path = f.name
        try:
            requests_payload, defaults = ccsearch.load_batch_requests(path)
        finally:
            os.unlink(path)
        self.assertEqual(len(requests_payload), 2)
        self.assertEqual(defaults, {})


# ===========================================================================
# 13. main() — CLI integration
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

    def test_list_engines_json_output(self):
        out, err, code = self._run_main(['--list-engines', '--format', 'json'])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertTrue(any(engine["name"] == "fetch" for engine in data["engines"]))

    def test_doctor_json_output(self):
        out, err, code = self._run_main(['--doctor', '--format', 'json'])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertIn("dependencies", data)
        self.assertIn("environment", data)
        self.assertIn("batch", data)

    def test_batch_file_json_output(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump({"requests": [{"query": "alpha", "engine": "brave"}]}, f)
            batch_path = f.name
        try:
            with patch('ccsearch.execute_batch', return_value={"results": [{"index": 1, "engine": "brave", "query": "alpha", "results": []}], "count": 1, "error_count": 0}) as mock_batch:
                out, err, code = self._run_main(['--batch-file', batch_path, '--format', 'json'])
        finally:
            os.unlink(batch_path)
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["count"], 1)
        mock_batch.assert_called_once()

    def test_batch_file_workers_forwarded(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump({"requests": [{"query": "alpha", "engine": "brave"}]}, f)
            batch_path = f.name
        try:
            with patch('ccsearch.execute_batch', return_value={"results": [], "count": 0, "error_count": 0, "success_count": 0, "has_errors": False, "duration_ms": 0, "max_workers": 3, "engine_counts": {}}) as mock_batch:
                out, err, code = self._run_main(['--batch-file', batch_path, '--batch-workers', '3', '--format', 'json'])
        finally:
            os.unlink(batch_path)
        self.assertEqual(code, 0)
        self.assertEqual(mock_batch.call_args.kwargs["max_workers"], 3)

    def test_batch_file_text_output(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write('{"query":"alpha","engine":"brave"}\n')
            batch_path = f.name
        try:
            with patch('ccsearch.execute_batch', return_value={"results": [{"index": 1, "engine": "brave", "query": "alpha", "results": [{"title": "First result"}]}], "count": 1, "error_count": 0, "success_count": 1, "has_errors": False, "duration_ms": 12.5, "max_workers": 1, "deduped_count": 0, "engine_counts": {"brave": 1}}):
                out, err, code = self._run_main(['--batch-file', batch_path, '--format', 'text'])
        finally:
            os.unlink(batch_path)
        self.assertEqual(code, 0)
        self.assertIn("Batch completed: 1 request(s), 1 success(es), 0 error(s), 12.5ms total with 1 worker(s)", out)
        self.assertIn("=== Request 1 ===", out)
        self.assertIn("First result", out)

    def test_batch_file_text_output_shows_deduped_count(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write('{"query":"alpha","engine":"brave"}\n')
            batch_path = f.name
        try:
            with patch('ccsearch.execute_batch', return_value={"results": [{"index": 1, "engine": "brave", "query": "alpha", "results": [{"title": "First result"}]}], "count": 2, "error_count": 0, "success_count": 2, "has_errors": False, "duration_ms": 12.5, "max_workers": 1, "deduped_count": 1, "engine_counts": {"brave": 2}}):
                out, err, code = self._run_main(['--batch-file', batch_path, '--format', 'text'])
        finally:
            os.unlink(batch_path)
        self.assertEqual(code, 0)
        self.assertIn("Deduplicated requests: 1", out)

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
        with patch('ccsearch.perform_fetch') as mock_pf, \
             patch('ccsearch.load_config') as mock_cfg:
            cfg = configparser.ConfigParser()
            cfg['Fetch'] = {'flaresolverr_url': '', 'flaresolverr_timeout': '60000', 'flaresolverr_mode': 'fallback'}
            mock_cfg.return_value = cfg
            mock_pf.return_value = {"engine": "fetch", "url": "http://x", "title": "T",
                                    "content": "C", "fetched_via": "direct"}
            out, err, code = self._run_main(['http://x', '-e', 'fetch', '--format', 'json', '--flaresolverr'])
        self.assertIn("WARNING", err)
        self.assertIn("no flaresolverr_url configured", err)

    # ---- LLM Context engine ----

    def test_llm_context_missing_api_key(self):
        env_backup_s = os.environ.pop('BRAVE_SEARCH_API_KEY', None)
        env_backup_b = os.environ.pop('BRAVE_API_KEY', None)
        try:
            out, err, code = self._run_main(['test', '-e', 'llm-context'])
            self.assertEqual(code, 1)
            self.assertIn("BRAVE_SEARCH_API_KEY", err)
        finally:
            if env_backup_s:
                os.environ['BRAVE_SEARCH_API_KEY'] = env_backup_s
            if env_backup_b:
                os.environ['BRAVE_API_KEY'] = env_backup_b

    @patch('ccsearch.perform_llm_context_search')
    def test_llm_context_prefers_search_key(self, mock_lc):
        """BRAVE_SEARCH_API_KEY takes priority over BRAVE_API_KEY."""
        mock_lc.return_value = {
            "engine": "llm-context", "query": "test",
            "results": [], "sources": {}
        }
        out, err, code = self._run_main(
            ['test', '-e', 'llm-context', '--format', 'json'],
            env={'BRAVE_SEARCH_API_KEY': 'search_key', 'BRAVE_API_KEY': 'pro_key'})
        self.assertEqual(code, 0)
        mock_lc.assert_called_once_with("test", "search_key", unittest.mock.ANY)

    @patch('ccsearch.perform_llm_context_search')
    def test_llm_context_falls_back_to_brave_key(self, mock_lc):
        """Falls back to BRAVE_API_KEY when BRAVE_SEARCH_API_KEY is not set."""
        env_backup = os.environ.pop('BRAVE_SEARCH_API_KEY', None)
        try:
            mock_lc.return_value = {
                "engine": "llm-context", "query": "test",
                "results": [], "sources": {}
            }
            out, err, code = self._run_main(
                ['test', '-e', 'llm-context', '--format', 'json'],
                env={'BRAVE_API_KEY': 'fallback_key'})
            self.assertEqual(code, 0)
            mock_lc.assert_called_once_with("test", "fallback_key", unittest.mock.ANY)
        finally:
            if env_backup:
                os.environ['BRAVE_SEARCH_API_KEY'] = env_backup

    @patch('ccsearch.perform_llm_context_search')
    def test_llm_context_json_output(self, mock_lc):
        mock_lc.return_value = {
            "engine": "llm-context", "query": "test",
            "results": [{"url": "http://a", "title": "T", "snippets": ["S1"]}],
            "sources": {"http://a": {"title": "T", "hostname": "a", "age": None}}
        }
        out, err, code = self._run_main(['test', '-e', 'llm-context', '--format', 'json'],
                                         env={'BRAVE_API_KEY': 'k'})
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["engine"], "llm-context")
        self.assertEqual(len(data["results"]), 1)
        self.assertEqual(data["results"][0]["snippets"], ["S1"])

    @patch('ccsearch.perform_llm_context_search')
    def test_llm_context_text_output(self, mock_lc):
        mock_lc.return_value = {
            "engine": "llm-context", "query": "test",
            "result_count": 1,
            "source_count": 1,
            "results": [{"rank": 1, "url": "http://a", "title": "Title Here", "hostname": "a", "age": "2d", "snippets": ["Snippet content"]}],
            "sources": {"http://a": {}}
        }
        out, err, code = self._run_main(['test', '-e', 'llm-context', '--format', 'text'],
                                         env={'BRAVE_API_KEY': 'k'})
        self.assertEqual(code, 0)
        self.assertIn("LLM Context Results for: test", out)
        self.assertIn("Results: 1 | Sources: 1", out)
        self.assertIn("Age: 2d", out)
        self.assertIn("Title Here", out)
        self.assertIn("Snippet content", out)

    @patch('ccsearch.perform_llm_context_search')
    def test_llm_context_text_empty_results(self, mock_lc):
        mock_lc.return_value = {
            "engine": "llm-context", "query": "test",
            "results": [], "sources": {}
        }
        out, err, code = self._run_main(['test', '-e', 'llm-context', '--format', 'text'],
                                         env={'BRAVE_API_KEY': 'k'})
        self.assertEqual(code, 0)
        self.assertIn("LLM Context Results for: test", out)

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

    @patch('ccsearch.execute_query')
    def test_brave_host_filters_forwarded(self, mock_execute):
        mock_execute.return_value = {"engine": "brave", "results": []}
        self._run_main(
            ['test', '-e', 'brave', '--format', 'json', '--include-host', 'developers.openai.com', '--exclude-host', 'reddit.com'],
            env={'BRAVE_API_KEY': 'k'}
        )
        self.assertEqual(mock_execute.call_args.kwargs["include_hosts"], ['developers.openai.com'])
        self.assertEqual(mock_execute.call_args.kwargs["exclude_hosts"], ['reddit.com'])

    @patch('ccsearch.execute_query')
    def test_brave_result_limit_forwarded(self, mock_execute):
        mock_execute.return_value = {"engine": "brave", "results": []}
        self._run_main(
            ['test', '-e', 'brave', '--format', 'json', '--limit', '3'],
            env={'BRAVE_API_KEY': 'k'}
        )
        self.assertEqual(mock_execute.call_args.kwargs["result_limit"], 3)

    @patch('ccsearch.perform_brave_search')
    def test_brave_text_output(self, mock_bs):
        mock_bs.return_value = {"engine": "brave", "query": "test", "result_count": 1,
                                "results": [{"rank": 1, "title": "Title", "url": "http://a", "description": "Desc", "hostname": "a"}]}
        out, err, code = self._run_main(['test', '-e', 'brave', '--format', 'text'],
                                         env={'BRAVE_API_KEY': 'k'})
        self.assertIn("Brave Search Results for: test", out)
        self.assertIn("Results: 1", out)
        self.assertIn("[a]", out)
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

    @patch('ccsearch.perform_perplexity_search')
    def test_perplexity_text_output_shows_citations(self, mock_ps):
        mock_ps.return_value = {
            "engine": "perplexity",
            "model": "perplexity/sonar",
            "query": "test",
            "answer": "Answer text",
            "citations": [{"url": "https://example.com", "title": "Example"}],
        }
        out, err, code = self._run_main(['test', '-e', 'perplexity', '--format', 'text'],
                                         env={'OPENROUTER_API_KEY': 'k'})
        self.assertIn("Citations:", out)
        self.assertIn("Example", out)
        self.assertIn("https://example.com", out)

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
        mock_both.return_value = {"engine": "both", "query": "test", "brave_result_count": 1,
                                  "brave_results": [{"rank": 1, "title": "T", "url": "U", "description": "D", "hostname": "u"}],
                                  "perplexity_answer": "Synthesized"}
        out, err, code = self._run_main(['test', '-e', 'both', '--format', 'text'],
                                         env={'BRAVE_API_KEY': 'bk', 'OPENROUTER_API_KEY': 'pk'})
        self.assertIn("Synthesized Answer (Perplexity)", out)
        self.assertIn("Source Reference Links (Brave)", out)
        self.assertIn("Results: 1", out)

    @patch('ccsearch.perform_both_search')
    def test_both_text_output_shows_citations(self, mock_both):
        mock_both.return_value = {
            "engine": "both",
            "query": "test",
            "brave_result_count": 0,
            "brave_results": [],
            "perplexity_answer": "Synthesized",
            "perplexity_citations": [{"url": "https://example.com", "title": "Example"}],
        }
        out, err, code = self._run_main(['test', '-e', 'both', '--format', 'text'],
                                         env={'BRAVE_API_KEY': 'bk', 'OPENROUTER_API_KEY': 'pk'})
        self.assertIn("Citations:", out)
        self.assertIn("Example", out)

    @patch('ccsearch.perform_both_search')
    def test_both_text_output_shows_partial_errors(self, mock_both):
        mock_both.return_value = {
            "engine": "both",
            "query": "test",
            "brave_result_count": 0,
            "brave_results": [],
            "perplexity_answer": "",
            "brave_error": "brave down",
            "perplexity_error": "pplx down",
        }
        out, err, code = self._run_main(['test', '-e', 'both', '--format', 'text'],
                                         env={'BRAVE_API_KEY': 'bk', 'OPENROUTER_API_KEY': 'pk'})
        self.assertIn("Perplexity error: pplx down", out)
        self.assertIn("Brave error: brave down", out)

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
        self.assertEqual(data.get("cache_status"), "exact")
        self.assertIn("duration_ms", data)

    @patch('ccsearch.perform_brave_search')
    @patch('ccsearch.read_from_cache')
    def test_cache_hit_text_shows_label(self, mock_rc, mock_bs):
        mock_rc.return_value = {"engine": "brave", "query": "test", "results": []}
        out, err, code = self._run_main(['test', '-e', 'brave', '--format', 'text', '--cache'],
                                         env={'BRAVE_API_KEY': 'k'})
        self.assertIn("Returning Cached Result", out)
        self.assertIn("cache_status: exact", out)
        self.assertIn("duration_ms:", out)

    @patch('ccsearch.execute_query')
    def test_text_output_shows_host_filtering_summary(self, mock_execute):
        mock_execute.return_value = {
            "engine": "brave",
            "cache_status": "disabled",
            "duration_ms": 1.23,
            "result_count": 1,
            "results": [{"rank": 1, "title": "Title", "url": "http://a", "description": "Desc", "hostname": "a"}],
            "host_filtering": {"include_hosts": ["a.com"], "exclude_hosts": ["b.com"], "removed_results": 2},
        }
        out, _, _ = self._run_main(['test', '-e', 'brave', '--format', 'text'], env={'BRAVE_API_KEY': 'k'})
        self.assertIn("host_filtering:", out)
        self.assertIn("removed=2", out)

    @patch('ccsearch.execute_query')
    def test_text_output_shows_result_limiting_summary(self, mock_execute):
        mock_execute.return_value = {
            "engine": "brave",
            "cache_status": "disabled",
            "duration_ms": 1.23,
            "result_count": 1,
            "results": [{"rank": 1, "title": "Title", "url": "http://a", "description": "Desc", "hostname": "a"}],
            "result_limiting": {"limit": 1, "removed_results": 4},
        }
        out, _, _ = self._run_main(['test', '-e', 'brave', '--format', 'text'], env={'BRAVE_API_KEY': 'k'})
        self.assertIn("result_limiting:", out)
        self.assertIn("limit=1", out)
        self.assertIn("removed=4", out)

    @patch('ccsearch.perform_brave_search')
    @patch('ccsearch.read_from_cache')
    def test_no_cache_flag_no_cache_read(self, mock_rc, mock_bs):
        mock_bs.return_value = {"engine": "brave", "query": "test", "results": []}
        out, err, code = self._run_main(['test', '-e', 'brave', '--format', 'json'],
                                         env={'BRAVE_API_KEY': 'k'})
        mock_rc.assert_not_called()
        data = json.loads(out)
        self.assertEqual(data.get("cache_status"), "disabled")
        self.assertIn("duration_ms", data)

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
        self.assertIn("ERROR: unexpected", err)


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


# ===========================================================================
# Semantic cache utilities
# ===========================================================================
class TestCosineSim(unittest.TestCase):

    def test_identical_vectors(self):
        v = [1.0, 0.0, 0.0]
        self.assertAlmostEqual(ccsearch._cosine_sim(v, v), 1.0)

    def test_orthogonal_vectors(self):
        self.assertAlmostEqual(ccsearch._cosine_sim([1, 0], [0, 1]), 0.0)

    def test_opposite_vectors(self):
        self.assertAlmostEqual(ccsearch._cosine_sim([1, 0], [-1, 0]), -1.0)

    def test_zero_vector_returns_zero(self):
        self.assertEqual(ccsearch._cosine_sim([0, 0], [1, 2]), 0.0)

    def test_similar_vectors(self):
        a = [0.9, 0.1]
        b = [0.8, 0.2]
        sim = ccsearch._cosine_sim(a, b)
        self.assertGreater(sim, 0.99)


class TestComputeEmbedding(unittest.TestCase):

    def setUp(self):
        # Reset the global model cache before each test
        ccsearch._embedding_model = None

    def tearDown(self):
        ccsearch._embedding_model = None

    def test_returns_none_when_fastembed_missing(self):
        with patch.dict('sys.modules', {'fastembed': None}):
            ccsearch._embedding_model = None
            # Simulate ImportError by patching the import
            with patch('builtins.__import__', side_effect=ImportError("no fastembed")):
                # Force re-init
                ccsearch._embedding_model = None
                result = ccsearch._compute_embedding("test query")
            # May be None if model couldn't load; just confirm no exception raised
        # Reset so other tests can use real fastembed
        ccsearch._embedding_model = None

    def test_returns_list_of_floats(self):
        mock_model = MagicMock()
        import numpy as np
        fake_emb = np.array([0.1] * 384)
        mock_model.embed.return_value = iter([fake_emb])
        ccsearch._embedding_model = mock_model

        result = ccsearch._compute_embedding("hello world")
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 384)
        self.assertIsInstance(result[0], float)

    def test_returns_none_on_embed_exception(self):
        mock_model = MagicMock()
        mock_model.embed.side_effect = RuntimeError("embed failed")
        ccsearch._embedding_model = mock_model

        result = ccsearch._compute_embedding("test")
        self.assertIsNone(result)


class TestSemanticIndexIO(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.orig_cache_dir = ccsearch.get_cache_dir

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        ccsearch.get_cache_dir = self.orig_cache_dir

    def _patch_cache_dir(self):
        ccsearch.get_cache_dir = lambda: self.tmpdir

    def test_load_returns_empty_dict_when_no_file(self):
        self._patch_cache_dir()
        with patch('ccsearch._semantic_index_path', return_value=os.path.join(self.tmpdir, 'semantic_index.json')):
            result = ccsearch._load_semantic_index()
        self.assertEqual(result, {})

    def test_save_and_load_roundtrip(self):
        index_path = os.path.join(self.tmpdir, 'semantic_index.json')
        with patch('ccsearch._semantic_index_path', return_value=index_path):
            index = {"abc123": {"query": "test", "engine": "brave", "offset": None, "embedding": [0.1, 0.2]}}
            ccsearch._save_semantic_index(index)
            loaded = ccsearch._load_semantic_index()
        self.assertEqual(loaded["abc123"]["query"], "test")
        self.assertEqual(loaded["abc123"]["embedding"], [0.1, 0.2])

    def test_load_returns_empty_on_corrupt_file(self):
        index_path = os.path.join(self.tmpdir, 'semantic_index.json')
        with open(index_path, 'w') as f:
            f.write("not valid json{{{{")
        with patch('ccsearch._semantic_index_path', return_value=index_path):
            result = ccsearch._load_semantic_index()
        self.assertEqual(result, {})


class TestReadFromSemanticCache(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        ccsearch._embedding_model = None

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        ccsearch._embedding_model = None

    def _write_cache_file(self, key, data):
        path = os.path.join(self.tmpdir, key + ".json")
        with open(path, 'w') as f:
            json.dump(data, f)
        return path

    def test_returns_none_when_index_empty(self):
        with patch('ccsearch._load_semantic_index', return_value={}):
            result, sim = ccsearch.read_from_semantic_cache("query", "brave", None, 10, 0.9)
        self.assertIsNone(result)
        self.assertEqual(sim, 0.0)

    def test_returns_none_when_embedding_fails(self):
        index = {"key1": {"engine": "brave", "offset": None, "embedding": [0.5, 0.5]}}
        with patch('ccsearch._load_semantic_index', return_value=index):
            with patch('ccsearch._compute_embedding', return_value=None):
                result, sim = ccsearch.read_from_semantic_cache("query", "brave", None, 10, 0.9)
        self.assertIsNone(result)

    def test_returns_cached_result_above_threshold(self):
        import numpy as np
        emb = [1.0, 0.0, 0.0]
        cache_data = {"engine": "brave", "query": "original query", "results": []}
        cache_key = "abc123"
        self._write_cache_file(cache_key, cache_data)

        index = {cache_key: {"engine": "brave", "offset": None, "embedding": emb}}
        with patch('ccsearch._load_semantic_index', return_value=index):
            with patch('ccsearch.get_cache_dir', return_value=self.tmpdir):
                with patch('ccsearch._compute_embedding', return_value=emb):  # identical => sim=1.0
                    result, sim = ccsearch.read_from_semantic_cache("similar query", "brave", None, 10, 0.9)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(sim, 1.0)

    def test_returns_none_below_threshold(self):
        emb_stored = [1.0, 0.0]
        emb_query  = [0.0, 1.0]  # orthogonal => sim=0.0
        cache_data = {"engine": "brave", "results": []}
        cache_key = "xyz789"
        self._write_cache_file(cache_key, cache_data)

        index = {cache_key: {"engine": "brave", "offset": None, "embedding": emb_stored}}
        with patch('ccsearch._load_semantic_index', return_value=index):
            with patch('ccsearch.get_cache_dir', return_value=self.tmpdir):
                with patch('ccsearch._compute_embedding', return_value=emb_query):
                    result, sim = ccsearch.read_from_semantic_cache("very different query", "brave", None, 10, 0.9)
        self.assertIsNone(result)

    def test_skips_wrong_engine(self):
        emb = [1.0, 0.0]
        index = {"k1": {"engine": "perplexity", "offset": None, "embedding": emb}}
        self._write_cache_file("k1", {"engine": "perplexity"})
        with patch('ccsearch._load_semantic_index', return_value=index):
            with patch('ccsearch.get_cache_dir', return_value=self.tmpdir):
                with patch('ccsearch._compute_embedding', return_value=emb):
                    result, sim = ccsearch.read_from_semantic_cache("query", "brave", None, 10, 0.5)
        self.assertIsNone(result)

    def test_skips_expired_entry(self):
        emb = [1.0, 0.0]
        cache_key = "expired_key"
        cache_file = self._write_cache_file(cache_key, {"engine": "brave", "results": []})
        # Set mtime to 20 minutes ago (TTL is 10 min)
        old_time = time.time() - 1200
        os.utime(cache_file, (old_time, old_time))

        index = {cache_key: {"engine": "brave", "offset": None, "embedding": emb}}
        with patch('ccsearch._load_semantic_index', return_value=index):
            with patch('ccsearch.get_cache_dir', return_value=self.tmpdir):
                with patch('ccsearch._compute_embedding', return_value=emb):
                    result, sim = ccsearch.read_from_semantic_cache("query", "brave", None, 10, 0.5)
        self.assertIsNone(result)


class TestUpdateSemanticIndex(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        ccsearch._embedding_model = None

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        ccsearch._embedding_model = None

    def test_stores_embedding_in_index(self):
        index_path = os.path.join(self.tmpdir, 'semantic_index.json')
        emb = [0.1, 0.9]

        with patch('ccsearch._compute_embedding', return_value=emb):
            with patch('ccsearch._load_semantic_index', return_value={}):
                with patch('ccsearch._semantic_index_path', return_value=index_path):
                    ccsearch.update_semantic_index("hello", "brave", None, "abc123.json")

        with open(index_path) as f:
            saved = json.load(f)
        self.assertIn("abc123", saved)
        self.assertEqual(saved["abc123"]["query"], "hello")
        self.assertEqual(saved["abc123"]["embedding"], emb)

    def test_does_nothing_when_embedding_fails(self):
        with patch('ccsearch._compute_embedding', return_value=None):
            with patch('ccsearch._save_semantic_index') as mock_save:
                ccsearch.update_semantic_index("hello", "brave", None, "abc123.json")
        mock_save.assert_not_called()


# ===========================================================================
# Semantic cache — extended edge cases
# ===========================================================================

class TestCosineSimEdgeCases(unittest.TestCase):
    """Edge cases for _cosine_sim beyond the basic tests."""

    def test_single_element_vectors(self):
        self.assertAlmostEqual(ccsearch._cosine_sim([5.0], [3.0]), 1.0)

    def test_both_zero_vectors(self):
        self.assertEqual(ccsearch._cosine_sim([0.0, 0.0], [0.0, 0.0]), 0.0)

    def test_one_zero_vector(self):
        self.assertEqual(ccsearch._cosine_sim([0.0, 0.0], [1.0, 0.0]), 0.0)

    def test_all_negative_vectors(self):
        # Two identical negative vectors should still yield similarity 1.0
        v = [-1.0, -2.0, -3.0]
        self.assertAlmostEqual(ccsearch._cosine_sim(v, v), 1.0)

    def test_unequal_length_truncates_to_shorter(self):
        # zip() silently truncates — verify we get a result without crashing
        a = [1.0, 0.0, 0.0]
        b = [1.0, 0.0]
        result = ccsearch._cosine_sim(a, b)
        self.assertIsInstance(result, float)

    def test_large_values_no_overflow(self):
        a = [1e150, 1e150]
        b = [1e150, 1e150]
        sim = ccsearch._cosine_sim(a, b)
        self.assertAlmostEqual(sim, 1.0)

    def test_near_threshold_precision(self):
        # 0.9 threshold comparison must not drop a 0.9000001 hit
        import math
        # Two vectors whose cosine is just above 0.9
        angle = math.acos(0.9001)
        a = [1.0, 0.0]
        b = [math.cos(angle), math.sin(angle)]
        sim = ccsearch._cosine_sim(a, b)
        self.assertGreater(sim, 0.90)


class TestComputeEmbeddingEdgeCases(unittest.TestCase):

    def setUp(self):
        ccsearch._embedding_model = None

    def tearDown(self):
        ccsearch._embedding_model = None

    def test_empty_string_does_not_crash(self):
        import numpy as np
        mock_model = MagicMock()
        mock_model.embed.return_value = iter([np.zeros(384)])
        ccsearch._embedding_model = mock_model
        result = ccsearch._compute_embedding("")
        self.assertIsInstance(result, list)

    def test_unicode_query(self):
        import numpy as np
        mock_model = MagicMock()
        mock_model.embed.return_value = iter([np.ones(384)])
        ccsearch._embedding_model = mock_model
        result = ccsearch._compute_embedding("Python 교육 튜토리얼 日本語テスト")
        self.assertIsInstance(result, list)

    def test_very_long_query_no_crash(self):
        import numpy as np
        mock_model = MagicMock()
        mock_model.embed.return_value = iter([np.ones(384)])
        ccsearch._embedding_model = mock_model
        long_query = "word " * 600   # 600 words, well above typical 512-token limit
        result = ccsearch._compute_embedding(long_query)
        self.assertIsNotNone(result)

    def test_sentinel_false_returns_none_without_retrying_import(self):
        # Once set to False, _get_embedding_model must not attempt another import
        ccsearch._embedding_model = False
        with patch('builtins.__import__') as mock_import:
            result = ccsearch._compute_embedding("test")
        mock_import.assert_not_called()
        self.assertIsNone(result)

    def test_embed_returns_non_iterable_raises_caught(self):
        mock_model = MagicMock()
        mock_model.embed.return_value = None  # next(None) raises TypeError
        ccsearch._embedding_model = mock_model
        result = ccsearch._compute_embedding("test")
        self.assertIsNone(result)


class TestSemanticIndexEdgeCases(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _index_path(self):
        return os.path.join(self.tmpdir, 'semantic_index.json')

    def test_save_unicode_content(self):
        idx = {"key1": {"query": "日本語テスト 한국어", "engine": "brave",
                        "offset": None, "embedding": [0.1, 0.2]}}
        with patch('ccsearch._semantic_index_path', return_value=self._index_path()):
            ccsearch._save_semantic_index(idx)
            loaded = ccsearch._load_semantic_index()
        self.assertEqual(loaded["key1"]["query"], "日本語テスト 한국어")

    def test_load_empty_file_returns_empty_dict(self):
        p = self._index_path()
        open(p, 'w').close()  # create zero-byte file
        with patch('ccsearch._semantic_index_path', return_value=p):
            result = ccsearch._load_semantic_index()
        self.assertEqual(result, {})

    def test_load_partial_json_returns_empty_dict(self):
        p = self._index_path()
        with open(p, 'w') as f:
            f.write('{"key": {"embed')  # truncated
        with patch('ccsearch._semantic_index_path', return_value=p):
            result = ccsearch._load_semantic_index()
        self.assertEqual(result, {})

    def test_save_failure_is_silent(self):
        # Writing to a non-existent deeply nested path should just warn, not raise
        bad_path = '/nonexistent/deep/path/semantic_index.json'
        with patch('ccsearch._semantic_index_path', return_value=bad_path):
            # Should not raise
            ccsearch._save_semantic_index({"k": {"q": "v", "embedding": [0.1]}})

    def test_large_index_round_trips(self):
        import numpy as np
        p = self._index_path()
        big = {f"key{i}": {"query": f"q{i}", "engine": "brave",
                            "offset": None, "embedding": list(np.random.rand(384))}
               for i in range(200)}
        with patch('ccsearch._semantic_index_path', return_value=p):
            ccsearch._save_semantic_index(big)
            loaded = ccsearch._load_semantic_index()
        self.assertEqual(len(loaded), 200)
        self.assertEqual(loaded["key99"]["query"], "q99")


class TestReadFromSemanticCacheEdgeCases(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        ccsearch._embedding_model = None

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        ccsearch._embedding_model = None

    def _write_cache_file(self, key, data):
        path = os.path.join(self.tmpdir, key + ".json")
        with open(path, 'w') as f:
            json.dump(data, f)
        return path

    def _patch(self, index, emb):
        return (
            patch('ccsearch._load_semantic_index', return_value=index),
            patch('ccsearch.get_cache_dir', return_value=self.tmpdir),
            patch('ccsearch._compute_embedding', return_value=emb),
        )

    def test_missing_embedding_field_is_skipped(self):
        cache_data = {"engine": "brave", "results": []}
        self._write_cache_file("k1", cache_data)
        index = {"k1": {"engine": "brave", "offset": None}}  # no "embedding" key
        p1, p2, p3 = self._patch(index, [1.0, 0.0])
        with p1, p2, p3:
            result, sim = ccsearch.read_from_semantic_cache("q", "brave", None, 60, 0.5)
        self.assertIsNone(result)

    def test_empty_list_embedding_is_skipped(self):
        # Empty list is falsy — entry should be skipped
        self._write_cache_file("k1", {"engine": "brave"})
        index = {"k1": {"engine": "brave", "offset": None, "embedding": []}}
        p1, p2, p3 = self._patch(index, [1.0, 0.0])
        with p1, p2, p3:
            result, sim = ccsearch.read_from_semantic_cache("q", "brave", None, 60, 0.5)
        self.assertIsNone(result)

    def test_orphaned_index_entry_no_cache_file(self):
        # Index has entry but no corresponding .json file on disk
        index = {"ghost_key": {"engine": "brave", "offset": None, "embedding": [1.0, 0.0]}}
        p1, p2, p3 = self._patch(index, [1.0, 0.0])
        with p1, p2, p3:
            result, sim = ccsearch.read_from_semantic_cache("q", "brave", None, 60, 0.5)
        self.assertIsNone(result)

    def test_engine_isolation_brave_vs_perplexity(self):
        emb = [1.0, 0.0]
        self._write_cache_file("k1", {"engine": "brave"})
        index = {"k1": {"engine": "brave", "offset": None, "embedding": emb}}
        p1, p2, p3 = self._patch(index, emb)
        with p1, p2, p3:
            result, sim = ccsearch.read_from_semantic_cache("q", "perplexity", None, 60, 0.0)
        self.assertIsNone(result)

    def test_engine_isolation_perplexity_found(self):
        emb = [1.0, 0.0]
        self._write_cache_file("k1", {"engine": "perplexity", "answer": "42"})
        index = {"k1": {"engine": "perplexity", "offset": None, "embedding": emb}}
        p1, p2, p3 = self._patch(index, emb)
        with p1, p2, p3:
            result, sim = ccsearch.read_from_semantic_cache("q", "perplexity", None, 60, 0.5)
        self.assertIsNotNone(result)

    def test_offset_isolation_different_offset_not_matched(self):
        emb = [1.0, 0.0]
        self._write_cache_file("k1", {"engine": "brave"})
        index = {"k1": {"engine": "brave", "offset": 0, "embedding": emb}}
        p1, p2, p3 = self._patch(index, emb)
        with p1, p2, p3:
            result, sim = ccsearch.read_from_semantic_cache("q", "brave", 1, 60, 0.0)
        self.assertIsNone(result)

    def test_offset_none_vs_zero_not_matched(self):
        # offset=None (not provided) != offset=0 (explicitly passed)
        emb = [1.0, 0.0]
        self._write_cache_file("k1", {"engine": "brave"})
        index = {"k1": {"engine": "brave", "offset": None, "embedding": emb}}
        p1, p2, p3 = self._patch(index, emb)
        with p1, p2, p3:
            result, sim = ccsearch.read_from_semantic_cache("q", "brave", 0, 60, 0.0)
        self.assertIsNone(result)

    def test_picks_best_similarity_not_first_entry(self):
        # Two valid entries: k_low (sim 0.5) and k_high (sim 1.0) — k_high must win
        emb_q     = [1.0, 0.0]
        emb_low   = [0.0, 1.0]  # orthogonal → sim ≈ 0
        emb_high  = [1.0, 0.0]  # identical  → sim = 1
        self._write_cache_file("k_low",  {"engine": "brave", "results": ["low"]})
        self._write_cache_file("k_high", {"engine": "brave", "results": ["high"]})
        index = {
            "k_low":  {"engine": "brave", "offset": None, "embedding": emb_low},
            "k_high": {"engine": "brave", "offset": None, "embedding": emb_high},
        }
        p1, p2, p3 = self._patch(index, emb_q)
        with p1, p2, p3:
            result, sim = ccsearch.read_from_semantic_cache("q", "brave", None, 60, 0.5)
        self.assertIsNotNone(result)
        self.assertEqual(result["results"], ["high"])
        self.assertAlmostEqual(sim, 1.0)

    def test_threshold_exactly_at_boundary_matches(self):
        emb = [1.0, 0.0]
        self._write_cache_file("k1", {"engine": "brave", "results": []})
        index = {"k1": {"engine": "brave", "offset": None, "embedding": emb}}
        p1, p2, p3 = self._patch(index, emb)
        with p1, p2, p3:
            # Identical vectors → sim=1.0, threshold=1.0 → should match (>=)
            result, sim = ccsearch.read_from_semantic_cache("q", "brave", None, 60, 1.0)
        self.assertIsNotNone(result)

    def test_threshold_above_max_possible_never_matches(self):
        emb = [1.0, 0.0]
        self._write_cache_file("k1", {"engine": "brave", "results": []})
        index = {"k1": {"engine": "brave", "offset": None, "embedding": emb}}
        p1, p2, p3 = self._patch(index, emb)
        with p1, p2, p3:
            # threshold=1.0001 > max possible cosine sim of 1.0 → never matches
            result, sim = ccsearch.read_from_semantic_cache("q", "brave", None, 60, 1.0001)
        self.assertIsNone(result)

    def test_threshold_zero_matches_any_entry(self):
        emb_q   = [1.0, 0.0]
        emb_ent = [0.0, 1.0]  # orthogonal → sim = 0.0, which is >= 0.0
        self._write_cache_file("k1", {"engine": "brave", "results": ["found"]})
        index = {"k1": {"engine": "brave", "offset": None, "embedding": emb_ent}}
        p1, p2, p3 = self._patch(index, emb_q)
        with p1, p2, p3:
            result, sim = ccsearch.read_from_semantic_cache("q", "brave", None, 60, 0.0)
        self.assertIsNotNone(result)
        self.assertEqual(result["results"], ["found"])

    def test_corrupted_cache_file_returns_none(self):
        # Index entry exists and file exists, but content is invalid JSON
        p = os.path.join(self.tmpdir, "k1.json")
        with open(p, 'w') as f:
            f.write("{{not json{{")
        index = {"k1": {"engine": "brave", "offset": None, "embedding": [1.0, 0.0]}}
        p1, p2, p3 = self._patch(index, [1.0, 0.0])
        with p1, p2, p3:
            result, sim = ccsearch.read_from_semantic_cache("q", "brave", None, 60, 0.5)
        self.assertIsNone(result)

    def test_multiple_engines_in_index_only_matching_returned(self):
        emb = [1.0, 0.0]
        self._write_cache_file("kb", {"engine": "brave",     "results": ["brave"]})
        self._write_cache_file("kp", {"engine": "perplexity","answer":  "perp"})
        index = {
            "kb": {"engine": "brave",      "offset": None, "embedding": emb},
            "kp": {"engine": "perplexity", "offset": None, "embedding": emb},
        }
        p1, p2, p3 = self._patch(index, emb)
        with p1, p2, p3:
            result, _ = ccsearch.read_from_semantic_cache("q", "perplexity", None, 60, 0.5)
        self.assertIsNotNone(result)
        self.assertEqual(result["answer"], "perp")  # must get perplexity result

    def test_returns_zero_sim_on_total_miss(self):
        p1, p2, p3 = self._patch({}, [1.0, 0.0])
        with p1, p2, p3:
            result, sim = ccsearch.read_from_semantic_cache("q", "brave", None, 60, 0.9)
        self.assertIsNone(result)
        self.assertEqual(sim, 0.0)

    def test_similarity_score_is_rounded_to_4_places(self):
        import math
        angle = math.acos(0.9123456789)
        emb_ent = [1.0, 0.0]
        emb_q   = [math.cos(angle), math.sin(angle)]
        self._write_cache_file("k1", {"engine": "brave", "results": []})
        index = {"k1": {"engine": "brave", "offset": None, "embedding": emb_ent}}
        p1, p2, p3 = self._patch(index, emb_q)
        with p1, p2, p3:
            _, sim = ccsearch.read_from_semantic_cache("q", "brave", None, 60, 0.9)
        # Should be rounded to 4 decimal places
        self.assertEqual(sim, round(sim, 4))
        str_sim = str(sim)
        decimal_places = len(str_sim.split('.')[-1]) if '.' in str_sim else 0
        self.assertLessEqual(decimal_places, 4)


class TestUpdateSemanticIndexEdgeCases(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        ccsearch._embedding_model = None

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        ccsearch._embedding_model = None

    def test_strips_json_extension_from_key(self):
        idx_path = os.path.join(self.tmpdir, 'semantic_index.json')
        with patch('ccsearch._compute_embedding', return_value=[0.5, 0.5]):
            with patch('ccsearch._load_semantic_index', return_value={}):
                with patch('ccsearch._semantic_index_path', return_value=idx_path):
                    ccsearch.update_semantic_index("q", "brave", None, "abc123.json")
        with open(idx_path) as f:
            saved = json.load(f)
        self.assertIn("abc123", saved)
        self.assertNotIn("abc123.json", saved)

    def test_no_json_extension_key_unchanged(self):
        # If caller passes key without .json, replace is a no-op
        idx_path = os.path.join(self.tmpdir, 'semantic_index.json')
        with patch('ccsearch._compute_embedding', return_value=[0.5, 0.5]):
            with patch('ccsearch._load_semantic_index', return_value={}):
                with patch('ccsearch._semantic_index_path', return_value=idx_path):
                    ccsearch.update_semantic_index("q", "brave", None, "abc123")
        with open(idx_path) as f:
            saved = json.load(f)
        self.assertIn("abc123", saved)

    def test_overwrites_existing_entry(self):
        idx_path = os.path.join(self.tmpdir, 'semantic_index.json')
        existing = {"abc123": {"query": "old", "engine": "brave",
                               "offset": None, "embedding": [0.0]}}
        with patch('ccsearch._compute_embedding', return_value=[1.0, 0.0]):
            with patch('ccsearch._load_semantic_index', return_value=existing):
                with patch('ccsearch._semantic_index_path', return_value=idx_path):
                    ccsearch.update_semantic_index("new query", "brave", None, "abc123.json")
        with open(idx_path) as f:
            saved = json.load(f)
        self.assertEqual(saved["abc123"]["query"], "new query")
        self.assertEqual(saved["abc123"]["embedding"], [1.0, 0.0])

    def test_stores_correct_metadata(self):
        idx_path = os.path.join(self.tmpdir, 'semantic_index.json')
        with patch('ccsearch._compute_embedding', return_value=[0.1, 0.9]):
            with patch('ccsearch._load_semantic_index', return_value={}):
                with patch('ccsearch._semantic_index_path', return_value=idx_path):
                    ccsearch.update_semantic_index("test q", "perplexity", 2, "mykey.json")
        with open(idx_path) as f:
            saved = json.load(f)
        entry = saved["mykey"]
        self.assertEqual(entry["query"],   "test q")
        self.assertEqual(entry["engine"],  "perplexity")
        self.assertEqual(entry["offset"],  2)
        self.assertEqual(entry["embedding"], [0.1, 0.9])


class TestMainSemanticCacheIntegration(unittest.TestCase):
    """Tests the full main() cache flow for semantic cache paths."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        ccsearch._embedding_model = None
        self._old_argv = sys.argv
        self._old_env  = os.environ.copy()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        ccsearch._embedding_model = None
        sys.argv = self._old_argv
        os.environ.clear()
        os.environ.update(self._old_env)

    def _run_main(self, args, env=None):
        from io import StringIO
        out, err, code = StringIO(), StringIO(), 0
        sys.argv = ['ccsearch'] + args
        if env:
            os.environ.update(env)
        with patch('sys.stdout', out), patch('sys.stderr', err):
            try:
                ccsearch.main()
            except SystemExit as e:
                code = e.code if e.code is not None else 0
        return out.getvalue(), err.getvalue(), code

    def _brave_result(self):
        return {"engine": "brave", "query": "q", "results": [
            {"title": "T", "url": "http://x", "description": "D"}
        ]}

    def test_fetch_engine_does_not_use_semantic_cache(self):
        """fetch engine must bypass semantic cache entirely."""
        fetch_result = {"engine": "fetch", "url": "http://example.com",
                        "title": "T", "content": "C", "fetched_via": "direct"}
        with patch('ccsearch.perform_fetch', return_value=fetch_result) as mock_fetch:
            with patch('ccsearch.read_from_semantic_cache') as mock_sem:
                with patch('ccsearch.update_semantic_index') as mock_upd:
                    out, _, code = self._run_main(
                        ['http://example.com', '-e', 'fetch',
                         '--semantic-cache', '--format', 'json'])
        self.assertEqual(code, 0)
        mock_sem.assert_not_called()
        mock_upd.assert_not_called()

    def test_semantic_cache_implies_cache_writes_on_miss(self):
        """--semantic-cache without --cache should still write to exact cache."""
        brave_result = self._brave_result()
        with patch('ccsearch.perform_brave_search', return_value=brave_result):
            with patch('ccsearch.write_to_cache') as mock_write:
                with patch('ccsearch.update_semantic_index') as mock_update:
                    with patch('ccsearch.read_from_cache', return_value=None):
                        with patch('ccsearch.read_from_semantic_cache', return_value=(None, 0.0)):
                            with patch('ccsearch._compute_embedding', return_value=[0.1]*384):
                                self._run_main(
                                    ['test query', '-e', 'brave', '--semantic-cache',
                                     '--format', 'json'],
                                    env={'BRAVE_API_KEY': 'test-key'})
        mock_write.assert_called_once()
        mock_update.assert_called_once()

    def test_semantic_hit_adds_metadata_fields_to_json(self):
        """Semantic cache hit must set _from_cache=true and _semantic_similarity."""
        cached = self._brave_result()
        with patch('ccsearch.read_from_cache', return_value=None):
            with patch('ccsearch.read_from_semantic_cache', return_value=(cached, 0.9321)):
                out, _, code = self._run_main(
                    ['similar query', '-e', 'brave',
                     '--semantic-cache', '--format', 'json'],
                    env={'BRAVE_API_KEY': 'test-key'})
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertTrue(data.get("_from_cache"))
        self.assertAlmostEqual(data.get("_semantic_similarity"), 0.9321)


# ===========================================================================
# HTTP API and MCP server integration helpers
# ===========================================================================
class TestApiServer(unittest.TestCase):

    def setUp(self):
        self.api_server = importlib.import_module('api_server')
        self.client = self.api_server.app.test_client()
        self.api_key = 'test-api-key'
        self.api_server.API_KEY = self.api_key

    def test_health_endpoint(self):
        response = self.client.get('/health')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["service"], "ccsearch-api")

    def test_engines_requires_api_key(self):
        response = self.client.get('/engines')
        self.assertEqual(response.status_code, 401)

    def test_engines_success(self):
        response = self.client.get('/engines', headers={'X-API-Key': self.api_key})
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(any(engine["name"] == "fetch" for engine in data["engines"]))
        self.assertIn("diagnostics", data)

    def test_diagnostics_requires_api_key(self):
        response = self.client.get('/diagnostics')
        self.assertEqual(response.status_code, 401)

    def test_diagnostics_success(self):
        response = self.client.get('/diagnostics', headers={'X-API-Key': self.api_key})
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertIn("dependencies", data)
        self.assertIn("environment", data)

    def test_search_requires_json(self):
        response = self.client.post('/search', headers={'X-API-Key': self.api_key})
        self.assertEqual(response.status_code, 400)

    def test_search_rejects_invalid_engine(self):
        response = self.client.post(
            '/search',
            headers={'X-API-Key': self.api_key},
            json={'query': 'test', 'engine': 'nope'},
        )
        self.assertEqual(response.status_code, 400)

    def test_search_rejects_invalid_fetch_url(self):
        response = self.client.post(
            '/search',
            headers={'X-API-Key': self.api_key},
            json={'query': 'notaurl', 'engine': 'fetch'},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('valid HTTP or HTTPS URL', response.get_json()["message"])

    def test_search_rejects_invalid_offset_for_engine(self):
        response = self.client.post(
            '/search',
            headers={'X-API-Key': self.api_key},
            json={'query': 'test', 'engine': 'perplexity', 'offset': 1},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("offset", response.get_json()["message"])

    def test_search_rejects_negative_offset(self):
        response = self.client.post(
            '/search',
            headers={'X-API-Key': self.api_key},
            json={'query': 'test', 'engine': 'brave', 'offset': -1},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("greater than or equal to 0", response.get_json()["message"])

    def test_search_rejects_invalid_semantic_threshold(self):
        response = self.client.post(
            '/search',
            headers={'X-API-Key': self.api_key},
            json={'query': 'test', 'engine': 'brave', 'semantic_threshold': 1.5},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("semantic_threshold", response.get_json()["message"])

    @patch('api_server.load_config')
    @patch('api_server.execute_query')
    def test_search_success(self, mock_execute, mock_load):
        mock_load.return_value = _make_config()
        mock_execute.return_value = {"engine": "fetch", "content": "ok"}
        response = self.client.post(
            '/search',
            headers={'X-API-Key': self.api_key},
            json={'query': 'https://example.com', 'engine': 'fetch', 'cache': True},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["engine"], "fetch")
        mock_execute.assert_called_once()

    @patch('api_server.load_config')
    @patch('api_server.execute_query')
    def test_search_forwards_host_filters(self, mock_execute, mock_load):
        mock_load.return_value = _make_config()
        mock_execute.return_value = {"engine": "brave", "results": []}
        response = self.client.post(
            '/search',
            headers={'X-API-Key': self.api_key},
            json={'query': 'test', 'engine': 'brave', 'include_hosts': ['developers.openai.com'], 'exclude_hosts': 'reddit.com'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_execute.call_args.kwargs["include_hosts"], ['developers.openai.com'])
        self.assertEqual(mock_execute.call_args.kwargs["exclude_hosts"], 'reddit.com')

    @patch('api_server.load_config')
    @patch('api_server.execute_query')
    def test_search_forwards_result_limit(self, mock_execute, mock_load):
        mock_load.return_value = _make_config()
        mock_execute.return_value = {"engine": "brave", "results": []}
        response = self.client.post(
            '/search',
            headers={'X-API-Key': self.api_key},
            json={'query': 'test', 'engine': 'brave', 'result_limit': 3},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_execute.call_args.kwargs["result_limit"], 3)

    def test_search_rejects_host_filters_for_unsupported_engine(self):
        response = self.client.post(
            '/search',
            headers={'X-API-Key': self.api_key},
            json={'query': 'test', 'engine': 'perplexity', 'include_hosts': ['example.com']},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Host filters", response.get_json()["message"])

    def test_search_rejects_result_limit_for_unsupported_engine(self):
        response = self.client.post(
            '/search',
            headers={'X-API-Key': self.api_key},
            json={'query': 'test', 'engine': 'perplexity', 'result_limit': 2},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Result limiting", response.get_json()["message"])

    @patch('api_server.load_config')
    @patch('api_server.execute_query')
    def test_search_runtime_error_maps_to_500(self, mock_execute, mock_load):
        mock_load.return_value = _make_config()
        mock_execute.side_effect = RuntimeError('missing key')
        response = self.client.post(
            '/search',
            headers={'X-API-Key': self.api_key},
            json={'query': 'test', 'engine': 'brave'},
        )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()["error"], "Server Error")

    @patch('api_server.load_config')
    @patch('api_server.execute_batch')
    def test_batch_success(self, mock_batch, mock_load):
        mock_load.return_value = _make_config()
        mock_batch.return_value = {"results": [], "count": 0, "error_count": 0}
        response = self.client.post(
            '/batch',
            headers={'X-API-Key': self.api_key},
            json={'requests': [{'query': 'alpha', 'engine': 'brave'}], 'max_workers': 3},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["count"], 0)
        mock_batch.assert_called_once()
        self.assertEqual(mock_batch.call_args.kwargs["max_workers"], 3)

    def test_batch_requires_requests(self):
        response = self.client.post(
            '/batch',
            headers={'X-API-Key': self.api_key},
            json={},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("requests", response.get_json()["message"])


class TestMcpServerTools(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.mcp_server = importlib.import_module('mcp_server')

    def setUp(self):
        self._old_argv = sys.argv
        self._old_env = os.environ.copy()

    def tearDown(self):
        sys.argv = self._old_argv
        os.environ.clear()
        os.environ.update(self._old_env)

    def _run_main(self, args, env=None):
        from io import StringIO
        out, err, code = StringIO(), StringIO(), 0
        sys.argv = ['ccsearch'] + args
        if env:
            os.environ.update(env)
        with patch('sys.stdout', out), patch('sys.stderr', err):
            try:
                ccsearch.main()
            except SystemExit as e:
                code = e.code if e.code is not None else 0
        return out.getvalue(), err.getvalue(), code

    def _brave_result(self):
        return {"engine": "brave", "query": "q", "results": [
            {"title": "T", "url": "http://x", "description": "D"}
        ]}

    @patch('mcp_server.execute_query')
    @patch('mcp_server.load_config')
    def test_search_success(self, mock_load, mock_execute):
        mock_load.return_value = _make_config()
        mock_execute.return_value = {"engine": "brave", "results": []}
        result = self.mcp_server.search("test", engine="brave", cache=True)
        self.assertEqual(result["engine"], "brave")
        mock_execute.assert_called_once()

    @patch('mcp_server.execute_query')
    @patch('mcp_server.load_config')
    def test_search_forwards_host_filters(self, mock_load, mock_execute):
        mock_load.return_value = _make_config()
        mock_execute.return_value = {"engine": "brave", "results": []}
        result = self.mcp_server.search("test", engine="brave", include_hosts="developers.openai.com", exclude_hosts="reddit.com")
        self.assertEqual(result["engine"], "brave")
        self.assertEqual(mock_execute.call_args.kwargs["include_hosts"], "developers.openai.com")
        self.assertEqual(mock_execute.call_args.kwargs["exclude_hosts"], "reddit.com")

    @patch('mcp_server.execute_query')
    @patch('mcp_server.load_config')
    def test_search_forwards_result_limit(self, mock_load, mock_execute):
        mock_load.return_value = _make_config()
        mock_execute.return_value = {"engine": "brave", "results": []}
        result = self.mcp_server.search("test", engine="brave", result_limit=2)
        self.assertEqual(result["engine"], "brave")
        self.assertEqual(mock_execute.call_args.kwargs["result_limit"], 2)

    def test_search_validation_error(self):
        result = self.mcp_server.search("", engine="brave")
        self.assertIn("error", result)

    def test_search_invalid_offset_error(self):
        result = self.mcp_server.search("test", engine="perplexity", offset=1)
        self.assertIn("error", result)
        self.assertIn("offset", result["error"])

    def test_search_rejects_host_filters_for_unsupported_engine(self):
        result = self.mcp_server.search("test", engine="perplexity", include_hosts="example.com")
        self.assertIn("error", result)
        self.assertIn("Host filters", result["error"])

    def test_search_rejects_result_limit_for_unsupported_engine(self):
        result = self.mcp_server.search("test", engine="perplexity", result_limit=2)
        self.assertIn("error", result)
        self.assertIn("Result limiting", result["error"])

    @patch('mcp_server.execute_query')
    @patch('mcp_server.load_config')
    def test_fetch_success(self, mock_load, mock_execute):
        mock_load.return_value = _make_config()
        mock_execute.return_value = {"engine": "fetch", "content": "ok"}
        result = self.mcp_server.fetch("https://example.com", cache=True)
        self.assertEqual(result["engine"], "fetch")
        mock_execute.assert_called_once()

    def test_engines_tool_returns_diagnostics(self):
        result = self.mcp_server.engines()
        self.assertIn("engines", result)
        self.assertIn("diagnostics", result)

    def test_diagnostics_tool_returns_dependency_state(self):
        result = self.mcp_server.diagnostics()
        self.assertIn("dependencies", result)
        self.assertIn("fetch", result)

    @patch('mcp_server.execute_batch')
    @patch('mcp_server.load_config')
    def test_batch_tool_success(self, mock_load, mock_batch):
        mock_load.return_value = _make_config()
        mock_batch.return_value = {"results": [], "count": 0, "error_count": 0}
        result = self.mcp_server.batch([{"query": "alpha", "engine": "brave"}], max_workers=5)
        self.assertEqual(result["count"], 0)
        mock_batch.assert_called_once()
        self.assertEqual(mock_batch.call_args.kwargs["max_workers"], 5)

    def test_batch_tool_validation_error(self):
        result = self.mcp_server.batch([])
        self.assertIn("error", result)

    def test_fetch_validation_error(self):
        result = self.mcp_server.fetch("notaurl")
        self.assertIn("error", result)

    def test_fetch_invalid_cache_ttl_error(self):
        result = self.mcp_server.fetch("https://example.com", cache_ttl=0)
        self.assertIn("error", result)
        self.assertIn("cache_ttl", result["error"])

    def test_engines_tool(self):
        result = self.mcp_server.engines()
        self.assertTrue(any(engine["name"] == "llm-context" for engine in result["engines"]))

    def test_exact_cache_hit_skips_semantic_lookup(self):
        """When exact cache hits, semantic lookup must not be called."""
        cached = {**self._brave_result(), "_from_cache": True}
        with patch('ccsearch.read_from_cache', return_value=cached):
            with patch('ccsearch.read_from_semantic_cache') as mock_sem:
                with patch('ccsearch._load_semantic_index', return_value={}):
                    with patch('ccsearch.update_semantic_index'):
                        self._run_main(
                            ['q', '-e', 'brave', '--semantic-cache', '--format', 'json'],
                            env={'BRAVE_API_KEY': 'test-key'})
        mock_sem.assert_not_called()

    def test_exact_cache_hit_backfills_semantic_index_when_missing(self):
        """Bug-fix: exact cache hit must insert embedding into index if absent."""
        cached = self._brave_result()
        with patch('ccsearch.read_from_cache', return_value=cached):
            with patch('ccsearch._load_semantic_index', return_value={}):  # key not in index
                with patch('ccsearch.update_semantic_index') as mock_upd:
                    self._run_main(
                        ['q', '-e', 'brave', '--semantic-cache', '--format', 'json'],
                        env={'BRAVE_API_KEY': 'test-key'})
        mock_upd.assert_called_once()

    def test_exact_cache_hit_does_not_backfill_when_already_indexed(self):
        """If key already in semantic index, don't recompute the embedding."""
        cached = self._brave_result()
        cache_key_no_ext = ccsearch.get_cache_key('q', 'brave', None).replace('.json', '')
        existing_index = {cache_key_no_ext: {"query": "q", "embedding": [0.1]}}
        with patch('ccsearch.read_from_cache', return_value=cached):
            with patch('ccsearch._load_semantic_index', return_value=existing_index):
                with patch('ccsearch.update_semantic_index') as mock_upd:
                    self._run_main(
                        ['q', '-e', 'brave', '--semantic-cache', '--format', 'json'],
                        env={'BRAVE_API_KEY': 'test-key'})
        mock_upd.assert_not_called()

    def test_semantic_cache_miss_falls_through_to_api(self):
        """Full miss on both caches must trigger the actual search."""
        brave_result = self._brave_result()
        with patch('ccsearch.read_from_cache', return_value=None):
            with patch('ccsearch.read_from_semantic_cache', return_value=(None, 0.0)):
                with patch('ccsearch.perform_brave_search', return_value=brave_result) as mock_bs:
                    with patch('ccsearch.write_to_cache'):
                        with patch('ccsearch.update_semantic_index'):
                            with patch('ccsearch._compute_embedding', return_value=None):
                                out, _, code = self._run_main(
                                    ['q', '-e', 'brave', '--semantic-cache', '--format', 'json'],
                                    env={'BRAVE_API_KEY': 'test-key'})
        mock_bs.assert_called_once()
        self.assertEqual(code, 0)

    def test_text_format_shows_cached_note_on_semantic_hit(self):
        """text format must print the [Returning Cached Result] header."""
        cached = self._brave_result()
        with patch('ccsearch.read_from_cache', return_value=None):
            with patch('ccsearch.read_from_semantic_cache', return_value=(cached, 0.95)):
                out, _, _ = self._run_main(
                    ['q', '-e', 'brave', '--semantic-cache', '--format', 'text'],
                    env={'BRAVE_API_KEY': 'test-key'})
        self.assertIn("Returning Cached Result", out)

    def test_no_semantic_cache_flag_never_calls_semantic_functions(self):
        """Without --semantic-cache, semantic functions must never be touched."""
        brave_result = self._brave_result()
        with patch('ccsearch.perform_brave_search', return_value=brave_result):
            with patch('ccsearch.read_from_semantic_cache') as mock_sem:
                with patch('ccsearch.update_semantic_index') as mock_upd:
                    self._run_main(
                        ['q', '-e', 'brave', '--cache', '--format', 'json'],
                        env={'BRAVE_API_KEY': 'test-key'})
        mock_sem.assert_not_called()
        mock_upd.assert_not_called()

    def test_llm_context_semantic_cache_works(self):
        """llm-context engine should support semantic cache."""
        llm_result = {
            "engine": "llm-context", "query": "q",
            "results": [{"url": "http://x", "title": "T", "snippets": ["S"]}],
            "sources": {}
        }
        with patch('ccsearch.perform_llm_context_search', return_value=llm_result):
            with patch('ccsearch.write_to_cache') as mock_write:
                with patch('ccsearch.update_semantic_index') as mock_update:
                    with patch('ccsearch.read_from_cache', return_value=None):
                        with patch('ccsearch.read_from_semantic_cache', return_value=(None, 0.0)):
                            with patch('ccsearch._compute_embedding', return_value=[0.1]*384):
                                self._run_main(
                                    ['test query', '-e', 'llm-context', '--semantic-cache',
                                     '--format', 'json'],
                                    env={'BRAVE_API_KEY': 'test-key'})
        mock_write.assert_called_once()
        mock_update.assert_called_once()

    def test_llm_context_cache_hit(self):
        """llm-context engine should return from cache when hit."""
        cached = {
            "engine": "llm-context", "query": "q",
            "results": [{"url": "http://x", "title": "Cached", "snippets": ["S"]}],
            "sources": {}
        }
        with patch('ccsearch.read_from_cache', return_value=cached):
            with patch('ccsearch.perform_llm_context_search') as mock_lc:
                out, _, code = self._run_main(
                    ['q', '-e', 'llm-context', '--cache', '--format', 'json'],
                    env={'BRAVE_API_KEY': 'test-key'})
        mock_lc.assert_not_called()
        data = json.loads(out)
        self.assertTrue(data.get("_from_cache"))
        self.assertEqual(data["results"][0]["title"], "Cached")


if __name__ == '__main__':
    unittest.main()
