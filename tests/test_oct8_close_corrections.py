"""Regression checks for the source-reviewed October 8 correction snapshot."""
import json
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import unittest

ROOT = Path(__file__).parents[1]
AUDIT = ROOT / 'audits' / '2026-10-08-close-corrections.json'


class October8ClosingCorrections(unittest.TestCase):
    def test_corrected_values_and_text_match_dated_evidence(self):
        audit = json.loads(AUDIT.read_text(encoding='utf-8'))
        self.assertEqual(len(audit['rows']), 21)
        for evidence in audit['rows']:
            with self.subTest(code=evidence['code']):
                stock = json.loads((ROOT / 'data' / (evidence['code'] + '.json')).read_text(encoding='utf-8'))
                rows = [row for row in stock['issues'] if row['date'] == evidence['date']]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]['pct'], evidence['after_pct'])
                self.assertEqual(rows[0]['text'], evidence['after_text'])
                calculated = (Decimal(evidence['close']) / Decimal(evidence['previous_close']) - 1) * 100
                self.assertEqual(calculated.quantize(Decimal('.01'), rounding=ROUND_HALF_UP), Decimal(str(evidence['after_pct'])))
                self.assertEqual(evidence['previous_date'], '2026-10-07')
                self.assertGreaterEqual(len(evidence['close_sources']), 2)

    def test_material_givebacks_use_the_actual_regular_high(self):
        audit = json.loads(AUDIT.read_text(encoding='utf-8'))
        annotated = [row for row in audit['rows'] if 'regular_high' in row]
        self.assertEqual({row['code'] for row in annotated}, {'000650', '005360', '0161M0', '323350'})
        for row in annotated:
            with self.subTest(code=row['code']):
                high = row['regular_high']
                prior = Decimal(row['previous_close'])
                high_return = (Decimal(high['price']) / prior - 1) * 100
                giveback = (Decimal(high['price']) - Decimal(row['close'])) / prior * 100
                self.assertGreater(high_return, 0)
                self.assertGreaterEqual(giveback, 5)
                self.assertEqual(high_return.quantize(Decimal('.01'), rounding=ROUND_HALF_UP), Decimal(str(high['return_pct'])))
                self.assertEqual(giveback.quantize(Decimal('.01'), rounding=ROUND_HALF_UP), Decimal(str(high['giveback_percentage_points'])))
                self.assertIn(f"+{high['return_pct']:.2f}%", row['after_text'])
                self.assertGreaterEqual(len(high['source_urls']), 2)


if __name__ == '__main__':
    unittest.main()
