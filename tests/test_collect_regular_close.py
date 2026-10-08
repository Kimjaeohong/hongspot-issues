"""Collector regression tests run only inside temporary directories."""
import contextlib
from copy import deepcopy
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tests.test_collect_tickers import collector
from tests.test_regular_close import evidence, NOW


def article(link='one', pub='Thu, 08 Oct 2026 10:00:00 +0900'):
    return {'originallink': 'https://example.test/' + link,
            'title': '[특징주] 네오사피엔스 상장 이후 66% 하락',
            'description': '장중 10% 하락 및 상장 이후 누적 하락', 'pubDate': pub}


class CollectorCloseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.chdir = contextlib.chdir(self.tmp.name); self.chdir.__enter__()
        self.addCleanup(self.chdir.__exit__, None, None, None)
        Path('tickers.json').write_text(json.dumps({'map': {'네오사피엔스': '0161M0'}}))

    def run_main(self, articles=None, extracted=None, rows=None):
        if rows is not None:
            Path('trusted.json').write_text(json.dumps({'schema_version': 1, 'rows': rows}))
        with patch.object(collector, 'ANTHROPIC_KEY', 'offline'), \
             patch.object(collector, 'fetch_news', return_value=[article()] if articles is None else articles), \
             patch.object(collector, 'llm_extract', return_value=extracted or [
                 {'name': '네오사피엔스', 'reason': '누적 하락: 상장 초기 차익실현 부담', 'pct': -66}]), \
             patch.dict('os.environ', {'KRX_TRUSTED_CLOSE_FILE': 'trusted.json' if rows is not None else ''}), \
             patch.object(collector, 'utc_now', return_value=NOW, create=True), \
             patch.object(collector, 'problems', []), \
             patch('urllib.request.urlopen', side_effect=AssertionError('Unexpected network call')), \
             contextlib.redirect_stdout(io.StringIO()):
            collector.main()

    def issue(self):
        return json.loads(Path('data/0161M0.json').read_text())['issues'][0]

    def test_cumulative_66_never_persisted_without_valid_close(self):
        self.run_main()
        self.assertNotIn('pct', self.issue())
        self.assertEqual(self.issue()['close_status'], 'pending')
        self.assertEqual(self.issue()['article']['url'], article()['originallink'])

    def test_intraday_and_rounded_article_percentages_ignored(self):
        for pct in (-10, '-9%', 'NaN', 29.9):
            with self.subTest(pct=pct):
                Path('seen.json').unlink(missing_ok=True)
                self.run_main(extracted=[{'name': '네오사피엔스', 'reason': '장중: 계약 발표 기대에 상승', 'pct': pct}])
                self.assertNotIn('pct', self.issue())

    def test_validated_lower_absolute_close_is_used_instead_of_article_value(self):
        self.run_main(rows=[evidence()])
        self.assertEqual(self.issue()['pct'], -9.16)
        self.assertEqual(self.issue()['close_status'], 'validated')
        self.assertEqual(self.issue()['price_evidence']['reference_price'], '13210')

    def test_pending_retry_only_targets_rows_this_collector_created(self):
        Path('data').mkdir()
        historic = [{'date': f'2026.09.{d:02d}', 'text': '과거 설명 그대로 보존', 'src': 'auto'}
                    for d in range(6, 0, -1)]
        Path('data/0161M0.json').write_text(json.dumps({'name': '네오사피엔스', 'issues': historic}))
        self.run_main()
        self.run_main(articles=[], rows=[evidence()])
        obj = json.loads(Path('data/0161M0.json').read_text())
        self.assertEqual(obj['issues'][1:], historic)
        self.assertEqual(obj['issues'][0]['pct'], -9.16)
        self.assertEqual(json.loads(Path('pending_closes.json').read_text()), [])

    def test_missing_or_invalid_evidence_stays_pending_without_crashing(self):
        row = evidence(); row['reference_price'] = '12750'
        self.run_main(rows=[row])
        self.assertNotIn('pct', self.issue())
        self.assertTrue(json.loads(Path('pending_closes.json').read_text()))

    def test_manual_same_date_is_preserved_and_not_queued(self):
        Path('data').mkdir()
        manual = {'name': '네오사피엔스', 'issues': [{'date': '2026.10.08',
                   'text': '수동 이유 그대로 보존', 'src': 'manual', 'pct': -66}]}
        original = json.dumps(manual)
        Path('data/0161M0.json').write_text(original)
        self.run_main(rows=[evidence()])
        self.assertEqual(Path('data/0161M0.json').read_text(), original)
        self.assertEqual(json.loads(Path('pending_closes.json').read_text()), [])

    def test_duplicate_articles_choose_deterministic_reason_without_pct_ranking(self):
        extracted = [{'name': '네오사피엔스', 'reason': '차익실현 부담에 장중 하락', 'pct': -66}]
        self.run_main(articles=[article('z'), article('a'), article('a')], extracted=extracted)
        self.assertEqual(len(json.loads(Path('data/0161M0.json').read_text())['issues']), 1)
        self.assertEqual(self.issue()['article']['url'], 'https://example.test/a')
        self.assertNotIn('pct', self.issue())

    def test_bad_publication_date_is_skipped_instead_of_using_today(self):
        self.run_main(articles=[article(pub='not a date')])
        self.assertFalse(Path('data/0161M0.json').exists())

    def test_weekend_article_is_skipped_and_holiday_close_never_reassigned(self):
        self.run_main(articles=[article(pub='Sat, 10 Oct 2026 10:00:00 +0900')])
        self.assertFalse(Path('data/0161M0.json').exists())
        self.run_main(articles=[article('holiday', pub='Fri, 09 Oct 2026 10:00:00 +0900')], rows=[evidence()])
        self.assertNotIn('pct', self.issue())
        self.assertEqual(self.issue()['date'], '2026.10.09')

    def test_validated_close_survives_source_outage_and_reason_change(self):
        self.run_main(rows=[evidence()])
        original = self.issue()['price_evidence']
        self.run_main(articles=[article('earlier', pub='Thu, 08 Oct 2026 09:00:00 +0900')])
        self.assertEqual(self.issue()['pct'], -9.16)
        self.assertEqual(self.issue()['price_evidence'], original)

    def test_old_magnitude_cannot_block_a_smaller_validated_close(self):
        self.run_main()
        obj = json.loads(Path('data/0161M0.json').read_text())
        obj['issues'][0]['pct'] = -66
        Path('data/0161M0.json').write_text(json.dumps(obj))
        self.run_main(articles=[], rows=[evidence()])
        self.assertEqual(self.issue()['pct'], -9.16)

    def test_manual_duplicate_blocks_merge_even_after_managed_auto(self):
        self.run_main()
        obj = json.loads(Path('data/0161M0.json').read_text())
        obj['issues'].append({'date': '2026.10.08', 'text': '수동 이유 그대로', 'src': 'manual'})
        original = json.dumps(obj)
        Path('data/0161M0.json').write_text(original)
        self.run_main(articles=[article('earlier', pub='Thu, 08 Oct 2026 09:00:00 +0900')])
        self.assertEqual(Path('data/0161M0.json').read_text(), original)

    def test_cumulative_and_intraday_scope_is_explicit_even_if_llm_omits_prefix(self):
        self.run_main(extracted=[{'name': '네오사피엔스', 'reason': '차익실현 부담에 하락'}])
        self.assertTrue(self.issue()['text'].startswith('복합: '))
        self.assertEqual(self.issue()['article']['time_scope'], 'mixed')

    def test_malformed_trusted_file_does_not_stop_reasons(self):
        Path('trusted.json').write_text('{broken')
        with patch.dict('os.environ', {'KRX_TRUSTED_CLOSE_FILE': 'trusted.json'}):
            with patch.object(collector, 'ANTHROPIC_KEY', 'offline'), \
                 patch.object(collector, 'fetch_news', return_value=[article()]), \
                 patch.object(collector, 'llm_extract', return_value=[{'name': '네오사피엔스', 'reason': '차익실현 부담에 하락'}]), \
                 patch.object(collector, 'utc_now', return_value=NOW), \
                 contextlib.redirect_stdout(io.StringIO()):
                collector.main()
        self.assertNotIn('pct', self.issue())

    def test_pending_retry_budget_does_not_drop_unattempted_work(self):
        self.run_main()
        with patch.object(collector, 'MAX_CLOSE_LOOKUPS', 0):
            self.run_main(articles=[], rows=[evidence()])
        self.assertNotIn('pct', self.issue())
        self.assertEqual(json.loads(Path('pending_closes.json').read_text()),
                         [{'code': '0161M0', 'date': '2026.10.08'}])

    def test_repeated_bounded_runs_eventually_attempt_later_pending_rows(self):
        Path('data').mkdir()
        queue = []
        for i in range(1, 102):
            code = f'{i:06d}'
            row = {'date': '2026.10.08', 'text': '장중: 공급계약 기대', 'src': 'auto',
                   'collector_version': collector.COLLECTOR_VERSION, 'close_status': 'pending',
                   'article': {'url': 'https://example.test/' + code,
                               'published_at': '2026-10-08T10:00:00+09:00'}}
            Path(f'data/{code}.json').write_text(json.dumps({'name': code, 'issues': [row]}))
            queue.append({'code': code, 'date': '2026.10.08'})
        Path('pending_closes.json').write_text(json.dumps(queue))
        self.run_main(articles=[], rows=[evidence('000101')])
        self.run_main(articles=[], rows=[evidence('000101')])
        issue = json.loads(Path('data/000101.json').read_text())['issues'][0]
        self.assertEqual(issue.get('pct'), -9.16)
        self.assertEqual(len(json.loads(Path('pending_closes.json').read_text())), 100)

    def test_scope_classifies_price_narrative_not_business_event_dates(self):
        cases = [
            ('지난달 신규 계약 소식에 상승', '[특징주] 지난달 맺은 공급 계약 이행에 강세', '',
             'unspecified', '지난달 신규 계약 소식에 상승'),
            ('올해 실적 개선 기대에 장중 상승', '[특징주] 올해 실적 기대', '',
             'intraday', '장중: 올해 실적 개선 기대에 장중 상승'),
            ('올해 30% 증가한 영업이익 기대', '[특징주] 올해 30% 증가한 영업이익', '',
             'unspecified', '올해 30% 증가한 영업이익 기대'),
            ('장중: 차익실현 부담에 하락', '[특징주] 상장 이후 66% 하락', '장중 10% 하락',
             'intraday', '장중: 차익실현 부담에 하락'),
            ('차익실현 부담에 하락', '[특징주] 상장 이후 66% 하락', '장중 10% 하락',
             'mixed', '복합: 차익실현 부담에 하락'),
            ('상장 이후 주가 하락에 따른 부담', '[특징주] 공급계약', '',
             'cumulative', '누적: 상장 이후 주가 하락에 따른 부담')]
        for reason, title, desc, scope, expected in cases:
            with self.subTest(reason=reason):
                self.assertEqual(collector.scoped_reason(reason, title, desc), (expected, scope))

    def test_pending_diagnostics_cover_unconfigured_missing_invalid_and_conflict(self):
        invalid = evidence(); invalid['reference_price'] = '12750'
        conflict = evidence(); conflict['close'] = '11000'
        cases = [(None, 'no_configured_source'), ([], 'missing_evidence'),
                 ([invalid], 'invalid_evidence'), ([evidence(), conflict], 'duplicate_conflict')]
        for rows, expected in cases:
            with self.subTest(expected=expected):
                Path('seen.json').unlink(missing_ok=True)
                self.run_main(rows=rows)
                self.assertEqual(self.issue().get('close_pending_reason'), expected)

    def test_budget_deferred_reason_is_persisted_and_cleared_after_validation(self):
        self.run_main()
        with patch.object(collector, 'MAX_CLOSE_LOOKUPS', 0):
            self.run_main(articles=[], rows=[evidence()])
        self.assertEqual(self.issue().get('close_pending_reason'), 'budget_deferred')
        self.run_main(articles=[], rows=[evidence()])
        self.assertEqual(self.issue()['pct'], -9.16)
        self.assertNotIn('close_pending_reason', self.issue())

    def test_llm_contract_emits_reason_only_even_if_model_returns_pct(self):
        response = {'content': [{'type': 'text', 'text': json.dumps([
            {'name': '네오사피엔스', 'reason': '누적 하락: 차익실현 부담', 'pct': -66}])}]}
        captured = []
        def open_response(request, **kwargs):
            captured.append(json.loads(request.data))
            return io.BytesIO(json.dumps(response).encode())
        with patch('urllib.request.urlopen', side_effect=open_response):
            rows = collector.llm_extract('상장 이후 66% 하락', '장중 10% 하락')
        self.assertNotIn('pct', rows[0])
        self.assertIn('장중', captured[0]['system'])
        self.assertIn('누적', captured[0]['system'])


if __name__ == '__main__':
    unittest.main()
