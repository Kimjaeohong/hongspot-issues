from datetime import date, timedelta
import importlib.util
from pathlib import Path
import unittest

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
