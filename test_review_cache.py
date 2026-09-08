"""Regression coverage for concurrent caches and rate limiting."""
import configparser
import json
import multiprocessing
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import ccsearch


class CacheReviewTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache_patch = patch('ccsearch.get_cache_dir', return_value=self.directory.name)
        self.cache_patch.start()
        self.addCleanup(self.cache_patch.stop)
        self.config = configparser.ConfigParser()
        self.config['Brave'] = {'requests_per_second': '1'}

    def test_saturated_key_does_not_block_other_keys(self):
        clock = [100.0]
        ccsearch._wait_for_brave_rate_limit(self.config, 'a', now_fn=lambda: clock[0])
        errors = []

        def sleep(seconds):
            finished = threading.Event()

            def other_key():
                try:
                    ccsearch._wait_for_brave_rate_limit(self.config, 'b', now_fn=lambda: clock[0])
                    finished.set()
                except Exception as exc:
                    errors.append(exc)

            worker = threading.Thread(target=other_key, daemon=True)
            worker.start()
            self.assertTrue(finished.wait(2), 'idle key blocked by another key sleeping')
            worker.join(2)
            clock[0] += seconds

        ccsearch._wait_for_brave_rate_limit(self.config, 'a', now_fn=lambda: clock[0], sleep_fn=sleep)
        self.assertEqual(errors, [])

    def test_invalid_rate_config_is_not_silently_replaced(self):
        for value in ['0', '-1', 'nan', 'inf', 'broken']:
            with self.subTest(value=value):
                self.config['Brave']['requests_per_second'] = value
                with self.assertRaises(ValueError):
                    ccsearch._brave_requests_per_second(self.config)

    def test_failures_are_neither_written_nor_reused(self):
        for field in ['error', 'brave_error', 'perplexity_error']:
            with self.subTest(field=field):
                result = {'engine': 'both', field: 'upstream unavailable'}
                ccsearch.write_to_cache(field, 'both', 0, result)
                path = os.path.join(self.directory.name, ccsearch.get_cache_key(field, 'both', 0))
                self.assertFalse(os.path.exists(path))
                with open(path, 'w') as handle:
                    json.dump(result, handle)
                self.assertIsNone(ccsearch.read_from_cache(field, 'both', 0, 10))

    def test_cache_does_not_swallow_programming_errors(self):
        with self.assertRaises(TypeError):
            ccsearch.write_to_cache('query', 'brave', 0, {'bad': object()})
        with patch('ccsearch._get_embedding_model') as get_model:
            get_model.return_value.embed.side_effect = RuntimeError('model broken')
            with self.assertRaisesRegex(RuntimeError, 'model broken'):
                ccsearch._compute_embedding('query')

    def test_meaningful_url_components_remain_distinct(self):
        pairs = [
            ('https://example.com/path', 'https://example.com/path/'),
            ('https://example.com/a/b', 'https://example.com/a//b'),
            ('https://example.com/path;x=1', 'https://example.com/path;x=2'),
            ('https://example.com/?sort=a&sort=b', 'https://example.com/?sort=b&sort=a'),
            ('https://user:one@example.com/', 'https://user:two@example.com/'),
        ]
        for first, second in pairs:
            with self.subTest(first=first):
                self.assertNotEqual(ccsearch.get_cache_key(first, 'fetch', None),
                                    ccsearch.get_cache_key(second, 'fetch', None))
        self.assertEqual(ccsearch.normalize_fetch_cache_url('https://[::1]:443/path'),
                         'https://[::1]/path')

    def test_no_embedding_work_when_no_matching_engine_exists(self):
        key = ccsearch.get_cache_key('query', 'brave', 0)[:-5]
        index = {key: {'engine': 'brave', 'offset': 0, 'embedding': [1.0]}}
        with patch('ccsearch._load_semantic_index', return_value=index), \
             patch('ccsearch._compute_embedding') as embed:
            self.assertEqual(ccsearch.read_from_semantic_cache('query', 'perplexity', 0, 10, 0.9),
                             (None, 0.0))
        embed.assert_not_called()

    @unittest.skipIf(ccsearch.fcntl is None, 'requires cross-process flock')
    def test_concurrent_process_semantic_updates_preserve_every_entry(self):
        context = multiprocessing.get_context('fork')
        start = context.Event()

        def worker(number):
            ccsearch.get_cache_dir = lambda: self.directory.name
            ccsearch._compute_embedding = lambda text: [1.0, 0.0]
            original_load = ccsearch._load_semantic_index

            def delayed_load():
                value = original_load()
                time.sleep(0.02)
                return value

            ccsearch._load_semantic_index = delayed_load
            start.wait(3)
            query = f'query-{number}'
            ccsearch.update_semantic_index(query, 'brave', 0, ccsearch.get_cache_key(query, 'brave', 0))

        processes = [context.Process(target=worker, args=(number,)) for number in range(6)]
        try:
            for process in processes:
                process.start()
            start.set()
            for process in processes:
                process.join(5)
            self.assertTrue(all(process.exitcode == 0 for process in processes))
            self.assertEqual(len(ccsearch._load_semantic_index()), 6)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                process.join()


if __name__ == '__main__':
    unittest.main()
