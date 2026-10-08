from datetime import date, timedelta
import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('update_aliases', Path(__file__).parents[1] / 'scripts/update_aliases.py')
ua = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ua)


def snaps(*names_by_day, code='242040'):
    """하루씩 이어지는 스냅샷. None 이면 그날 목록에서 빠짐."""
    start = date(2026, 6, 1)
    out = []
    for i, name in enumerate(names_by_day):
        snap = {'000001': 'Other'}
        if name is not None:
            snap[code] = name
        out.append((start + timedelta(days=i), snap))
    return out


class FetchSnapshotTests(unittest.TestCase):
    def test_numeric_and_alphanumeric_codes_survive_snapshot_fetch(self):
        raw = ('Code,Name,MarketId\n005930,삼성전자,STK\n'
               '0015G0,그린광학,KSQ\n00680K,미래에셋증권2우B,STK\n').encode('utf-8-sig')
        with patch('urllib.request.urlopen', return_value=io.BytesIO(raw)):
            result = ua.fetch_snapshot(date(2026, 10, 7))
        self.assertEqual(result, {'005930': '삼성전자', '0015G0': '그린광학',
                                  '00680K': '미래에셋증권2우B'})

    def test_malformed_codes_are_not_used_for_aliases(self):
        invalid = ('12345', '1234567', '0015g0', '0015-G', '../abc',
                   '００１５Ｇ０', '١٢٣٤٥٦')
        raw = ('Code,Name,MarketId\n005930,삼성전자,STK\n' +
               ''.join(f'{code},Invalid {i},STK\n' for i, code in enumerate(invalid)))
        with patch('urllib.request.urlopen', return_value=io.BytesIO(raw.encode('utf-8-sig'))):
            self.assertEqual(ua.fetch_snapshot(date(2026, 10, 7)), {'005930': '삼성전자'})

    def test_alphanumeric_rename_survives_fetch_detect_merge_and_index_update(self):
        snapshots = []
        for day, name in ((6, '옛이름'), (7, '새이름')):
            raw = f'Code,Name,MarketId\n0015G0,{name},KSQ\n'.encode('utf-8-sig')
            with patch('urllib.request.urlopen', return_value=io.BytesIO(raw)):
                snapshots.append((date(2026, 10, day), ua.fetch_snapshot(date(2026, 10, day))))
        aliases = {}
        ua.merge_aliases(aliases, ua.detect_renames(snapshots), snapshots[-1][1])
        index = {'옛이름': '0015G0'}
        self.assertEqual(ua.apply_to_index(index, aliases), ['0015G0'])
        self.assertEqual(index, {'새이름': '0015G0'})
        self.assertEqual(aliases, {'0015G0': {'name': '새이름', 'former': ['옛이름']}})


class DetectRenamesTests(unittest.TestCase):
    def test_chain_of_renames_in_order(self):
        s = snaps('나무기술', '나무기술', '나무에이엑스', '나무에이엑스', '나무AX')
        self.assertEqual(ua.detect_renames(s), {'242040': (['나무기술', '나무에이엑스'], '나무AX')})

    def test_no_change_reports_nothing(self):
        self.assertEqual(ua.detect_renames(snaps('A', 'A', 'A')), {})

    def test_single_day_glitch_in_middle_is_ignored(self):
        s = snaps('나무기술', '나무기술', '오타이름', '나무기술', '나무기술')
        self.assertEqual(ua.detect_renames(s), {})

    def test_first_run_cut_by_window_is_kept(self):
        # 조회 기간 첫날에만 옛 이름이 보이는 경우도 잡아야 함
        s = snaps('옛이름', '새이름', '새이름')
        self.assertEqual(ua.detect_renames(s), {'242040': (['옛이름'], '새이름')})

    def test_code_reuse_after_gap_is_not_linked(self):
        s = snaps('상폐종목', '상폐종목', None, '신규종목', '신규종목')
        self.assertEqual(ua.detect_renames(s), {})


class MergeAndApplyTests(unittest.TestCase):
    def test_merge_keeps_manual_older_names_first(self):
        aliases = {'242040': {'name': '나무기술', 'former': ['나무']}}
        changed = ua.merge_aliases(aliases, {'242040': (['나무기술', '나무에이엑스'], '나무AX')},
                                   {'242040': '나무AX'})
        self.assertTrue(changed)
        self.assertEqual(aliases['242040'], {'name': '나무AX',
                                             'former': ['나무', '나무기술', '나무에이엑스']})

    def test_spac_names_are_excluded(self):
        aliases = {'242040': {'name': '나무AX', 'former': ['교보비엔케이스팩', '나무기술']}}
        ua.merge_aliases(aliases, {'433530': (['하나27호스팩'], '신규상장사'),
                                   '999999': (['옛이름'], '미래에셋비전스팩9호')},
                         {'242040': '나무AX'})
        self.assertEqual(aliases, {'242040': {'name': '나무AX', 'former': ['나무기술']}})

    def test_merge_is_idempotent(self):
        aliases = {'242040': {'name': '나무AX', 'former': ['나무기술']}}
        changed = ua.merge_aliases(aliases, {'242040': (['나무기술'], '나무AX')}, {'242040': '나무AX'})
        self.assertFalse(changed)

    def test_renamed_back_drops_entry(self):
        aliases = {'111111': {'name': 'B', 'former': ['A']}}
        ua.merge_aliases(aliases, {}, {'111111': 'A'})
        self.assertEqual(aliases, {})

    def test_index_key_moves_to_current_name(self):
        index = {'나무기술': '242040', '삼성전자': '005930'}
        aliases = {'242040': {'name': '나무AX', 'former': ['나무기술']}}
        self.assertEqual(ua.apply_to_index(index, aliases), ['242040'])
        self.assertEqual(index, {'나무AX': '242040', '삼성전자': '005930'})


if __name__ == '__main__':
    unittest.main()
