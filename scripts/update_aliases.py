#!/usr/bin/env python3
"""사명 변경 추적.

FDR 공개 캐시의 날짜별 상장 목록에서 같은 종목코드의 이름이 바뀐 지점을 찾아
aliases.json 에 옛 사명을 기록하고, index.json·data/<코드>.json 의 이름을 현재 사명으로 맞춘다.

aliases.json 형식 (former 는 오래된 순서):
  {"242040": {"name": "나무AX", "former": ["교보비엔케이스팩", "나무기술", "나무에이엑스"]}}
캐시는 2026-03-08 부터라 그 이전 사명은 former 에 직접 추가하면 그대로 유지된다.

사용:
  python scripts/update_aliases.py                    # 최근 14일 (매일 실행용)
  python scripts/update_aliases.py --since 2026-03-08 # 전체 이력 백필
"""
import argparse
import csv
import io
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

CACHE_BASE = ('https://raw.githubusercontent.com/FinanceData/'
              'fdr_krx_data_cache/refs/heads/master/data/listing/krx')
ALIASES_FILE = 'aliases.json'
INDEX_FILE = 'index.json'
DATA_DIR = 'data'


def fetch_snapshot(day):
    """해당 날짜 상장 목록 {코드: 이름}. 파일이 없으면 None."""
    url = f'{CACHE_BASE}/{day.isoformat()}.csv'
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            raw = r.read().decode('utf-8-sig')
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    rows = csv.DictReader(io.StringIO(raw))
    return {row['Code'].strip(): row['Name'].strip() for row in rows
            if (row.get('Code') or '').strip().isdigit() and (row.get('Name') or '').strip()}


def detect_renames(snapshots):
    """snapshots: 날짜순 [(date, {code: name})] → {code: (옛 이름 목록, 현재 이름)}.

    - 중간에 하루만 나타났다 사라진 이름(데이터 오류)은 무시한다.
    - 목록에서 빠졌다가 다시 나타난 코드는 다른 종목(코드 재사용)으로 보고 잇지 않는다.
    """
    runs = {}      # code -> [[name, count], ...] 현재 연속 구간
    last_seen = {}  # code -> 마지막으로 등장한 스냅샷 순번
    for i, (_, snap) in enumerate(snapshots):
        for code, name in snap.items():
            if code in last_seen and last_seen[code] != i - 1:
                runs[code] = []          # 공백 후 재등장 → 새 종목으로 취급
            seq = runs.setdefault(code, [])
            if seq and seq[-1][0] == name:
                seq[-1][1] += 1
            else:
                seq.append([name, 1])
            last_seen[code] = i
    result = {}
    for code, seq in runs.items():
        last = seq[-1][0]
        former = []
        for k, (name, count) in enumerate(seq[:-1]):
            # 첫 구간은 조회 기간 시작에서 잘렸을 수 있으므로 하루짜리여도 인정
            if (count >= 2 or k == 0) and name != last and name not in former:
                former.append(name)
        if former:
            result[code] = (former, last)
    return result


def is_spac(name):
    """스팩(기업인수목적회사) 이름은 검색용 옛 사명으로 쓰지 않는다."""
    return '스팩' in name


def merge_aliases(aliases, renames, latest):
    """기존 aliases 에 새로 찾은 옛 사명을 덧붙인다 (기존 순서·수동 입력 유지, 스팩 제외).

    latest 로 현재 사명을 갱신하고, 옛 사명이 하나도 안 남은 항목은 지운다. 바뀌었으면 True.
    """
    before = json.dumps(aliases, sort_keys=True, ensure_ascii=False)
    for code, (former, current) in renames.items():
        names = [n for n in former if n != current and not is_spac(n)]
        if is_spac(current) or not names:
            continue
        entry = aliases.setdefault(code, {'name': current, 'former': []})
        for n in names:
            if n not in entry['former']:
                entry['former'].append(n)
    for code in list(aliases):
        entry = aliases[code]
        if latest.get(code):
            entry['name'] = latest[code]
        entry['former'] = [n for n in entry['former'] if n != entry['name'] and not is_spac(n)]
        if is_spac(entry['name']) or not entry['former']:
            del aliases[code]
    return json.dumps(aliases, sort_keys=True, ensure_ascii=False) != before


def apply_to_index(index, aliases):
    """index.json 에 옛 사명으로 등록된 종목을 현재 사명으로 바꾼다. 바뀐 코드 목록 반환."""
    renamed = []
    for code, entry in aliases.items():
        for old in entry['former']:
            if index.get(old) == code:
                del index[old]
                index[entry['name']] = code
                renamed.append(code)
    return renamed


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--since', help='YYYY-MM-DD (기본: 14일 전)')
    args = ap.parse_args(argv)
    today = datetime.now(timezone.utc).date()
    since = date.fromisoformat(args.since) if args.since else today - timedelta(days=14)

    snapshots = []
    day = since
    try:
        while day <= today:
            snap = fetch_snapshot(day)
            if snap:
                snapshots.append((day, snap))
            day += timedelta(days=1)
    except (urllib.error.URLError, OSError) as e:
        print(f'::warning::상장 목록 조회 실패({day}): {e} — 이번 실행은 건너뜀')
        return 0
    if len(snapshots) < 2:
        print('::warning::비교할 상장 목록이 부족해 건너뜀')
        return 0

    aliases = json.load(open(ALIASES_FILE, encoding='utf-8')) if os.path.exists(ALIASES_FILE) else {}
    renames = detect_renames(snapshots)
    changed = merge_aliases(aliases, renames, snapshots[-1][1])

    index = json.load(open(INDEX_FILE, encoding='utf-8'))
    renamed = apply_to_index(index, aliases)
    for code in set(renamed) | set(aliases):
        path = os.path.join(DATA_DIR, f'{code}.json')
        if not os.path.exists(path):
            continue
        obj = json.load(open(path, encoding='utf-8'))
        if obj.get('name') in aliases[code]['former']:
            obj['name'] = aliases[code]['name']
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(obj, f, ensure_ascii=False, indent=2); f.write('\n')
            renamed.append(code)

    if changed:
        ordered = {k: aliases[k] for k in sorted(aliases)}
        with open(ALIASES_FILE, 'w', encoding='utf-8') as f:
            json.dump(ordered, f, ensure_ascii=False, indent=2); f.write('\n')
    if renamed:
        with open(INDEX_FILE, 'w', encoding='utf-8') as f:
            json.dump({k: index[k] for k in sorted(index)}, f, ensure_ascii=False, indent=2); f.write('\n')
    with open('changed_codes.txt', 'a', encoding='utf-8') as f:
        f.write(''.join(f'{c}\n' for c in sorted(set(renamed))))

    print(f'상장 목록 {len(snapshots)}일치 비교 ({snapshots[0][0]} ~ {snapshots[-1][0]})')
    print(f'사명 변경 감지 {len(renames)}건 | aliases.json 변경: {"예" if changed else "아니오"} '
          f'| 현재 사명으로 바꾼 종목 {len(set(renamed))}개')
    return 0


if __name__ == '__main__':
    sys.exit(main())
