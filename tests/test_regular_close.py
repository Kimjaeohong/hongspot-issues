"""Strict offline intake contract; all examples are synthetic, not market data."""
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import unittest

PATH = Path(__file__).parents[1] / 'scripts/regular_close.py'
SOURCE = None
if PATH.exists():
    spec = importlib.util.spec_from_file_location('regular_close', PATH)
    SOURCE = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(SOURCE)
NOW = datetime(2026, 10, 8, 8, tzinfo=timezone.utc)


def evidence(code='0161M0', day='2026-10-08', previous='2026-10-07'):
    return {'code': code, 'source_code': code, 'previous_source_code': code,
            'date': day, 'previous_date': previous, 'reference_date': previous,
            'session': 'KRX_REGULAR', 'previous_session': 'KRX_REGULAR',
            'currency': 'KRW', 'close': '12000', 'previous_close': '13210',
            'reference_price': '13210', 'reported_change': '-1210',
            'reported_pct': '-9.16', 'high': '13400', 'low': '11920',
            'session_close_at': day + 'T15:30:00+09:00',
            'previous_session_close_at': previous + 'T15:30:00+09:00',
            'finalized_at': day + 'T15:50:00+09:00',
            'previous_finalized_at': previous + 'T15:50:00+09:00',
            'source': {'provider': 'synthetic-test',
                       'url': 'https://example.test/current-original',
                       'previous_url': 'https://example.test/previous-original',
                       'regular_session_contract': 'https://example.test/regular-close-contract',
                       'retrieved_at': day + 'T16:00:00+09:00',
                       'previous_retrieved_at': previous + 'T16:00:00+09:00',
                       'evidence_sha256': 'a' * 64, 'previous_evidence_sha256': 'b' * 64,
                       'verification': 'independent-regular-session-review'}}


class RegularCloseTests(unittest.TestCase):
    def validate(self, row=None, code='0161M0', day='2026.10.08', now=NOW):
        self.assertIsNotNone(SOURCE, 'Strict regular-close validator is missing')
        return SOURCE.validate_evidence(row or evidence(), code, day, now=now)

    def test_dated_regular_close_and_actual_prior_produce_decimal_2dp_return(self):
        result = self.validate()
        self.assertEqual(result['pct'], -9.16)
        self.assertEqual(result['price_evidence']['previous_date'], '2026-10-07')
        self.assertEqual(result['price_evidence']['close'], '12000')
        self.assertEqual(result['price_evidence']['source']['url'], 'https://example.test/current-original')

    def test_stale_wrong_date_wrong_symbol_unknown_and_afterhours_rejected(self):
        for key, value in [('date', '2026-10-07'), ('source_code', '005930'),
                           ('previous_source_code', '005930'), ('session', 'UNKNOWN'),
                           ('previous_session', 'NXT_AFTERHOURS'), ('currency', 'USD')]:
            with self.subTest(key=key):
                row = evidence(); row[key] = value
                self.assertIsNone(self.validate(row))

    def test_today_intraday_or_not_finalized_rejected(self):
        self.assertIsNone(self.validate(now=datetime(2026, 10, 8, 5, tzinfo=timezone.utc)))
        for key in ('finalized_at', 'session_close_at'):
            row = evidence(); row[key] = '2026-10-08T13:30:00+09:00'
            self.assertIsNone(self.validate(row))

    def test_conflicting_reference_price_or_date_rejected(self):
        for key, value in [('reference_price', '12750'), ('reference_date', '2026-10-06'),
                           ('reported_change', '-66'), ('reported_pct', '-66')]:
            with self.subTest(key=key):
                row = evidence(); row[key] = value
                self.assertIsNone(self.validate(row))

    def test_weekend_and_holiday_date_mismatch_do_not_roll_forward(self):
        self.assertIsNone(self.validate(evidence(day='2026-10-09'), day='2026.10.10'))
        self.assertIsNone(self.validate(evidence(day='2026-10-08'), day='2026.10.09'))

    def test_actual_prior_date_can_cross_weekend_without_guessing_yesterday(self):
        row = evidence(day='2026-10-12', previous='2026-10-08')
        result = self.validate(row, day='2026.10.12', now=datetime(2026, 10, 12, 8, tzinfo=timezone.utc))
        self.assertEqual(result['price_evidence']['previous_date'], '2026-10-08')

    def test_malformed_nonfinite_negative_zero_and_inconsistent_prices_rejected(self):
        for key, value in [('close', 'nan'), ('previous_close', 'Infinity'),
                           ('close', True), ('close', None), ('close', '-1'),
                           ('previous_close', '0'), ('close', '12,000oops'),
                           ('low', '12830'), ('high', '11000')]:
            with self.subTest(key=key, value=value):
                row = evidence(); row[key] = value
                self.assertIsNone(self.validate(row))

    def test_unverified_label_only_and_missing_provenance_rejected(self):
        for field in ('verification', 'regular_session_contract', 'evidence_sha256', 'previous_url'):
            row = evidence(); row['source'].pop(field)
            self.assertIsNone(self.validate(row))

    def test_leadingzeros_and_alphanumeric_identity_are_exact(self):
        for code in ('0161M0', '005930'):
            self.assertEqual(self.validate(evidence(code), code=code)['pct'], -9.16)
        self.assertIsNone(self.validate(evidence('005930'), code='5930'))

    def test_extreme_numeric_values_fail_closed_instead_of_crashing(self):
        for key, value in [('close', '1e1000000'), ('previous_close', '1e-1000000')]:
            row = evidence(); row[key] = value
            self.assertIsNone(self.validate(row))

    def test_per_code_date_validation_is_cached_and_returns_independent_copy(self):
        self.assertIsNotNone(SOURCE)
        from unittest.mock import patch
        intake = SOURCE.TrustedEvidenceSource([evidence()], now=NOW)
        with patch.object(SOURCE, 'validate_evidence', wraps=SOURCE.validate_evidence) as validate:
            result = intake.get('0161M0', '2026.10.08')
            result['pct'] = -66
            self.assertEqual(intake.get('0161M0', '2026.10.08')['pct'], -9.16)
            self.assertEqual(validate.call_count, 1)

    def test_url_field_types_fail_closed_without_raising(self):
        for field in ('url', 'previous_url', 'regular_session_contract'):
            for value in (123, ['https://example.test'], {'url': 'https://example.test'}):
                with self.subTest(field=field, value=value):
                    row = evidence(); row['source'][field] = value
                    self.assertIsNone(self.validate(row))

    def test_intake_distinguishes_missing_invalid_and_conflicting_evidence(self):
        self.assertIsNotNone(SOURCE)
        invalid = evidence(); invalid['reference_price'] = '12750'
        conflict = evidence(); conflict['close'] = '11000'
        cases = [([], 'missing_evidence'), ([invalid], 'invalid_evidence'),
                 ([evidence(), conflict], 'duplicate_conflict')]
        for rows, expected in cases:
            with self.subTest(expected=expected):
                intake = SOURCE.TrustedEvidenceSource(rows, now=NOW)
                self.assertIsNone(intake.get('0161M0', '2026.10.08'))
                self.assertTrue(hasattr(intake, 'pending_reason'), 'Missing intake diagnostics')
                self.assertEqual(intake.pending_reason('0161M0', '2026.10.08'), expected)

    def test_intake_duplicate_conflict_fails_closed(self):
        self.assertIsNotNone(SOURCE, 'Trusted evidence intake is missing')
        rows = [evidence(), evidence()]
        rows[1]['close'] = '10000'
        intake = SOURCE.TrustedEvidenceSource(rows, now=NOW)
        self.assertIsNone(intake.get('0161M0', '2026.10.08'))


if __name__ == '__main__':
    unittest.main()
