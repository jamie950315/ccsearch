"""Regression coverage for early validation and truthful failure reporting."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

import ccsearch


class CoreReviewTests(unittest.TestCase):
    def setUp(self):
        self.config = ccsearch.load_config('/nonexistent/config.ini')

    def test_bad_option_is_isolated_from_valid_batch_item(self):
        invalid = [{"cache_ttl": value} for value in (None, "bad", [], True, float('nan'))]
        invalid += [{"offset": "bad"}, {"result_limit": 1.5},
                    {"semantic_threshold": None}, {"semantic_threshold": 10**400}, {"cache": "false"},
                    {"semantic_cache": []}, {"flaresolverr": "false"},
                    {"include_hosts": [None]}]
        for options in invalid:
            with self.subTest(options=options), patch('ccsearch.execute_engine', return_value={"results": []}), patch('ccsearch.prune_cache'):
                result = ccsearch.execute_batch([
                    {"query": "bad", "engine": "brave", **options},
                    {"query": "good", "engine": "brave"},
                ], self.config)
                self.assertEqual(result['error_count'], 1)
                self.assertEqual(result['success_count'], 1)
                self.assertIn('error', result['results'][0])

    def test_invalid_fetch_url_rejected_before_cache_or_network(self):
        for query in ('httpwhatever', 'http://', 'https://host:bad', 'http://[::1',
                      'https://host:99999', 'https://a b/', 'https://host/\npath', 42):
            with self.subTest(query=query), patch('ccsearch.prune_cache') as prune, patch('ccsearch.execute_engine') as run:
                with self.assertRaises(ValueError):
                    ccsearch.execute_query(query, 'fetch', self.config, cache=True)
                prune.assert_not_called()
                run.assert_not_called()

    def test_valid_ipv6_and_uppercase_url(self):
        for query in ('http://[::1]:8888/', 'HTTPS://example.com/'):
            self.assertIsNone(ccsearch.validate_query(query, 'fetch'))

    def test_failed_response_not_cached_or_embedded(self):
        for result in ({'error': 'failed'}, {'brave_error': 'failed', 'perplexity_answer': 'ok'}):
            with patch('ccsearch.prune_cache'), patch('ccsearch._exact_cache_lookup', return_value=None), patch('ccsearch._semantic_cache_lookup', return_value=None), patch('ccsearch.execute_engine', return_value=result), patch('ccsearch.write_to_cache') as write, patch('ccsearch.update_semantic_index') as embed:
                ccsearch.execute_query('q', 'both', self.config, semantic_cache=True)
                write.assert_not_called()
                embed.assert_not_called()

    def test_both_engines_failed_is_not_success(self):
        with patch('ccsearch.perform_brave_search', side_effect=RuntimeError('Brave unavailable')), patch('ccsearch.perform_perplexity_search', side_effect=RuntimeError('Perplexity unavailable')):
            self.assertIn('error', ccsearch.perform_both_search('q', 'dummy', 'dummy', self.config))

    def test_invalid_perplexity_content_raises(self):
        for content in (None, '', '  ', [], {}):
            with patch('ccsearch.retry_request') as request:
                request.return_value.json.return_value = {'choices': [{'message': {'content': content}}]}
                with self.assertRaisesRegex(RuntimeError, 'no valid answer'):
                    ccsearch.perform_perplexity_search('q', 'dummy', self.config)

    def test_negative_retries_rejected_before_request(self):
        with patch('ccsearch.requests.get') as get:
            with self.assertRaises(ValueError):
                ccsearch.retry_request('GET', 'https://example.com', -1)
            get.assert_not_called()

    def test_invalid_request_configuration_does_not_retry(self):
        with patch('ccsearch.requests.get', side_effect=ccsearch.requests.exceptions.InvalidHeader('bad header')) as get, patch('ccsearch.time.sleep') as sleep:
            with self.assertRaises(ccsearch.requests.exceptions.InvalidHeader):
                ccsearch.retry_request('GET', 'https://example.com', 2)
            self.assertEqual(get.call_count, 1)
            sleep.assert_not_called()

    def test_search_error_payload_not_reported_as_empty_success(self):
        for fn in (ccsearch.perform_brave_search, ccsearch.perform_llm_context_search):
            for payload in ([], {'error': {'message': 'denied'}}):
                with self.subTest(fn=fn.__name__, payload=payload), patch('ccsearch.retry_request') as request:
                    request.return_value.json.return_value = payload
                    with self.assertRaises(RuntimeError):
                        fn('q', 'dummy', self.config)

    def test_falsey_batch_defaults_rejected(self):
        for defaults in (False, [], '', 0):
            with self.assertRaises(ValueError):
                ccsearch.execute_batch([{'query': 'q'}], self.config, defaults=defaults)

    def test_cli_fetch_failure_sets_nonzero_exit(self):
        with patch.object(sys, 'argv', ['ccsearch', 'https://example.com', '-e', 'fetch']), patch('ccsearch.execute_query', return_value={'error': 'HTTP 404'}), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as exc:
                ccsearch.main()
            self.assertEqual(exc.exception.code, 1)

    def test_cli_preserves_batch_file_host_defaults(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json') as batch:
            json.dump({'defaults': {'include_hosts': ['example.com']}, 'requests': [{'query': 'q'}]}, batch)
            batch.flush()
            with patch.object(sys, 'argv', ['ccsearch', '--batch-file', batch.name]), patch('ccsearch.execute_batch', return_value={'results': []}) as run, contextlib.redirect_stdout(io.StringIO()):
                ccsearch.main()
            self.assertEqual(run.call_args.kwargs['defaults']['include_hosts'], ['example.com'])
