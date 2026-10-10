"""Regression checks for manually reviewed LG Energy Solution issue history."""
import hashlib
import json
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import unittest
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parents[1]


class LgEnergySolutionHistoryTests(unittest.TestCase):
    def setUp(self):
        self.audit = json.loads((ROOT / 'audits/2026-10-10-lgensol-history.json').read_text())
        self.stock = json.loads((ROOT / 'data/373220.json').read_text())
        self.by_date = {row['date']: row for row in self.stock['issues']}

    def test_admitted_rows_match_reviewed_snapshot(self):
        self.assertEqual(self.stock['name'], 'LG에너지솔루션')
        self.assertEqual(len(self.audit['rows']), 14)
        self.assertEqual(len(self.stock['issues']), 20)
        for row in self.audit['rows']:
            with self.subTest(date=row['record_date']):
                self.assertEqual(self.by_date[row['record_date'].replace('-', '.')], row['issue'])
                self.assertEqual(row['issue']['src'], 'manual')
                self.assertEqual(row['price_evidence']['status'], 'corroborated_regular_close')
                self.assertTrue(row['price_evidence']['session_corroboration']['url'].startswith('https://'))

    def test_existing_rows_preserved_exactly(self):
        self.assertEqual(len(self.audit['preserved_rows']), 6)
        for row in self.audit['preserved_rows']:
            self.assertEqual(self.by_date[row['date']], row)
        self.assertEqual(self.by_date['2026.10.08']['pct'], 2.56)
        self.assertEqual(self.by_date['2025.07.30']['pct'], .26)
        self.assertEqual(self.by_date['2025.12.18']['pct'], -8.9)

    def test_date_order_and_no_duplicate_stock_dates(self):
        dates = [row['date'] for row in self.stock['issues']]
        self.assertEqual(dates, sorted(set(dates), reverse=True))

    def test_original_price_records_and_decimal_returns(self):
        source = ROOT / self.audit['source_provenance']['raw_file']
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), self.audit['source_provenance']['sha256'])
        # The provider's JSON field contains a JavaScript tuple list with a trailing comma.
        tuples = json.loads(source.read_text())['value'].rstrip().removesuffix(',')
        ohlc = json.loads('[' + tuples + ']')
        self.assertEqual(len(ohlc), 750)
        for row in self.audit['rows']:
            p = row['price_evidence']
            with self.subTest(date=row['record_date']):
                current_index = p['raw_row_index_zero_based']
                self.assertEqual(p['previous_raw_row_index_zero_based'], current_index - 1)
                self.assertEqual(ohlc[current_index], p['raw_row'])
                self.assertEqual(ohlc[current_index - 1], p['previous_raw_row'])
                for value, key in [(ohlc[current_index], 'date'), (ohlc[current_index - 1], 'previous_date')]:
                    day = datetime.fromtimestamp(value[0] / 1000, ZoneInfo('Asia/Seoul')).date().isoformat()
                    self.assertEqual(day, p[key])
                self.assertEqual(ohlc[current_index][4], p['close'])
                self.assertEqual(ohlc[current_index - 1][4], p['previous_close'])
                pct = ((Decimal(p['close']) / Decimal(p['previous_close']) - 1) * 100).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
                self.assertEqual(pct, Decimal(str(row['issue']['pct'])))
                self.assertLessEqual(p['low'], p['close'])
                self.assertGreaterEqual(p['high'], p['close'])

    def test_after_close_and_annual_release_dates(self):
        rows = {row['event_date']: row for row in self.audit['rows']}
        self.assertEqual(rows['2026-04-07']['record_date'], '2026-04-08')
        self.assertIn('전일 장 마감 후', rows['2026-04-07']['issue']['text'])
        self.assertNotIn('2026.04.07', self.by_date)
        self.assertIn('2024년 잠정 연간', self.by_date['2025.01.09']['text'])
        self.assertIn('2025년 잠정 연간', self.by_date['2026.01.09']['text'])
        self.assertIn('실적설명회', self.by_date['2023.10.25']['text'])

    def test_no_unverified_or_price_only_candidates_inserted(self):
        self.assertEqual(len(self.audit['holds']), 5)
        self.assertEqual(self.audit['requested_candidates'], 14 + 5 + len(self.audit['duplicate_candidates']))
        for held in self.audit['holds']:
            day = held['price_evidence']['date'].replace('-', '.')
            self.assertNotIn(day, self.by_date)
        self.assertIn('MOU', self.by_date['2026.10.01']['text'])
        self.assertIn('협의', self.by_date['2026.10.01']['text'])
        self.assertNotIn('공급 계약', self.by_date['2026.10.01']['text'])


if __name__ == '__main__':
    unittest.main()
