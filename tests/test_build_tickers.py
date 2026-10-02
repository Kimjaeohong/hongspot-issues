import contextlib
import csv
from datetime import date
import importlib.util
import http.client
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

SPEC = importlib.util.spec_from_file_location('build_tickers', Path(__file__).parents[1] / 'scripts/build_tickers.py')
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


def fixture_csv(count=2400, markets=('STK', 'KSQ', 'KNX')):
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(['Code', 'Name', 'MarketId'])
    for i in range(count):
        writer.writerow([f'{i:06}', f'Stock {i}', markets[i % len(markets)]])
    return stream.getvalue().encode('utf-8-sig')


class CacheBuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name) / 'tickers.json'
        self.old = {'updated': '2026-10-01T01:55:03', 'count': 2400,
                    'source': 'fdr', 'map': {f'Stock {i}': f'{i:06}' for i in range(2400)}}
        self.output.write_text(json.dumps(self.old), encoding='utf-8')
        self.before = self.output.read_bytes()

    def run_build(self, responses):
        calls = []
        def fetch(url, timeout):
            calls.append(url)
            key = url.rsplit('/', 1)[-1]
            result = responses.get(key, HTTPError(url, 404, 'Missing', {}, None))
            if isinstance(result, Exception):
                raise result
            return io.BytesIO(result)
        with patch('urllib.request.urlopen', side_effect=fetch), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            builder.build(output_path=self.output, today=date(2026, 10, 2))
        return json.loads(self.output.read_text()), calls

    def test_today_missing_uses_yesterday_with_honest_source_date(self):
        result, calls = self.run_build({'2026-10-01.csv': fixture_csv()})
        self.assertEqual(result['source_date'], '2026-10-01')
        self.assertEqual(result['stale_days'], 1)
        self.assertEqual(result['source'], 'fdr-cache')
        self.assertEqual(result['count'], 2400)
        self.assertTrue(result['source_url'].endswith('/2026-10-01.csv'))
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(result['updated'], self.old['updated'])

    def test_today_valid_does_not_fetch_older_files(self):
        result, calls = self.run_build({'2026-10-02.csv': fixture_csv()})
        self.assertEqual(result['stale_days'], 0)
        self.assertEqual(result['source_date'], '2026-10-02')
        self.assertEqual(len(calls), 1)

    def test_invalid_latest_uses_next_valid_snapshot(self):
        result, _ = self.run_build({'2026-10-02.csv': b'<html>bad</html>', '2026-10-01.csv': fixture_csv()})
        self.assertEqual(result['source_date'], '2026-10-01')

    def test_total_failure_preserves_existing_bytes(self):
        with self.assertRaises(SystemExit):
            self.run_build({})
        self.assertEqual(self.output.read_bytes(), self.before)

    def test_partial_empty_malformed_and_missing_market_preserve_existing(self):
        for payload in (b'', fixture_csv(20), fixture_csv(markets=('STK', 'KSQ')), fixture_csv().replace(b'000001', b'bad-code', 1)):
            with self.subTest(payload_size=len(payload)):
                with self.assertRaises(SystemExit):
                    self.run_build({'2026-10-02.csv': payload})
                self.assertEqual(self.output.read_bytes(), self.before)

    def test_large_drop_is_rejected_even_above_minimum(self):
        with self.assertRaises(SystemExit):
            self.run_build({'2026-10-02.csv': fixture_csv(2100)})
        self.assertEqual(self.output.read_bytes(), self.before)

    def test_conflicting_duplicate_name_rejected(self):
        payload = fixture_csv() + b'999999,Stock 1,STK\n'
        with self.assertRaises(SystemExit):
            self.run_build({'2026-10-02.csv': payload})
        self.assertEqual(self.output.read_bytes(), self.before)

    def test_write_failure_preserves_existing_bytes(self):
        with patch('os.replace', side_effect=OSError('write failed')):
            with self.assertRaises(OSError):
                self.run_build({'2026-10-02.csv': fixture_csv()})
        self.assertEqual(self.output.read_bytes(), self.before)

    def test_no_regression_behind_existing_source_date(self):
        self.old['source_date'] = '2026-10-02'
        self.output.write_text(json.dumps(self.old))
        before = self.output.read_bytes()
        with self.assertRaises(SystemExit):
            self.run_build({'2026-10-01.csv': fixture_csv()})
        self.assertEqual(self.output.read_bytes(), before)

    def test_missing_cache_is_bounded_and_network_failure_preserves_map(self):
        seen = []
        def missing(url, timeout):
            seen.append(url)
            raise URLError('offline')
        with patch('urllib.request.urlopen', side_effect=missing), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                builder.build(output_path=self.output, today=date(2026, 10, 2))
        self.assertEqual([url.rsplit('/', 1)[-1] for url in seen],
                         [f'2026-10-{day:02}.csv' for day in (2, 1)] +
                         [f'2026-09-{day}.csv' for day in (30, 29, 28, 27, 26, 25)])
        self.assertEqual(self.output.read_bytes(), self.before)

    def test_incomplete_http_response_falls_back(self):
        try:
            result, _ = self.run_build({'2026-10-02.csv': http.client.IncompleteRead(b'partial'),
                                        '2026-10-01.csv': fixture_csv()})
        except http.client.IncompleteRead:
            self.fail('Incomplete HTTP response bypassed the validated cache fallback')
        self.assertEqual(result['source_date'], '2026-10-01')

    def test_seven_day_boundary_is_accepted(self):
        result, calls = self.run_build({'2026-09-25.csv': fixture_csv()})
        self.assertEqual(result['stale_days'], 7)
        self.assertEqual(len(calls), 8)

    def test_eight_day_snapshot_is_not_used(self):
        with self.assertRaises(SystemExit):
            self.run_build({'2026-09-24.csv': fixture_csv()})
        self.assertEqual(self.output.read_bytes(), self.before)

    def test_existing_numeric_only_contract_is_preserved(self):
        result, _ = self.run_build({'2026-10-02.csv': fixture_csv() + b'00680K,Preferred share,STK\n'})
        self.assertNotIn('Preferred share', result['map'])
        self.assertEqual(result['count'], 2400)


if __name__ == '__main__':
    unittest.main()
