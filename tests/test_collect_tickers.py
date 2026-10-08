"""Offline path from a cache snapshot to collector files, without API calls."""
import contextlib
from datetime import date
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.test_build_tickers import builder, fixture_csv

SPEC = importlib.util.spec_from_file_location('collect', Path(__file__).parents[1] / 'scripts/collect.py')
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)


class CollectorTickerTests(unittest.TestCase):
    def test_built_map_collects_numeric_and_alphanumeric_stocks_end_to_end(self):
        stocks = {'삼성전자': '005930', '그린광학': '0015G0',
                  '에스엔시스': '0008Z0', '에임드바이오': '0009K0',
                  '삼성에피스홀딩스': '0126Z0', '미래에셋증권2우B': '00680K'}
        extra = ''.join(f'{code},{name},STK\n' for name, code in stocks.items())
        payload = fixture_csv() + extra.encode('utf-8')
        article = {'originallink': 'https://example.test/news/offline-fixture',
                   'title': '[특징주] 테스트 기사', 'description': '오프라인 테스트 전용',
                   'pubDate': 'Wed, 07 Oct 2026 10:00:00 +0900'}
        extracted = [{'name': name, 'reason': '신규 공급계약 체결 소식에 상승', 'pct': 5.0}
                     for name in stocks]
        extracted.append({'name': '알 수 없는 종목', 'reason': '신규 공급계약 체결 소식에 상승', 'pct': 5.0})

        with tempfile.TemporaryDirectory() as directory, contextlib.chdir(directory):
            with patch('urllib.request.urlopen', return_value=io.BytesIO(payload)), \
                    contextlib.redirect_stdout(io.StringIO()):
                builder.build(today=date(2026, 10, 7))
            with patch.object(collector, 'ANTHROPIC_KEY', 'offline-test'), \
                    patch.object(collector, 'fetch_news', return_value=[article]), \
                    patch.object(collector, 'llm_extract', return_value=extracted), \
                    patch.object(collector, 'problems', []), \
                    patch('urllib.request.urlopen', side_effect=AssertionError('Unexpected network call')), \
                    contextlib.redirect_stdout(io.StringIO()):
                collector.main()

            self.assertEqual(json.loads(Path('index.json').read_text()), stocks)
            self.assertEqual(set(os.listdir('data')), {f'{code}.json' for code in stocks.values()})
            for name, code in stocks.items():
                with self.subTest(code=code):
                    result = json.loads(Path('data', f'{code}.json').read_text())
                    self.assertEqual(result['name'], name)
                    self.assertEqual(len(result['issues']), 1)
                    issue = result['issues'][0]
                    self.assertEqual(issue['date'], '2026.10.07')
                    self.assertEqual(issue['text'], '신규 공급계약 체결 소식에 상승')
                    self.assertEqual(issue['src'], 'auto')
                    self.assertEqual(issue['close_status'], 'pending')
                    self.assertNotIn('pct', issue)
            self.assertEqual(Path('changed_codes.txt').read_text().splitlines(), sorted(stocks.values()))
            self.assertIn(article['originallink'], json.loads(Path('seen.json').read_text()))


if __name__ == '__main__':
    unittest.main()
