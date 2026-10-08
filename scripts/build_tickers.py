#!/usr/bin/env python3
"""Build the stock-name/code map from FinanceDataReader's public KRX cache.

Try UTC today, then up to seven previous calendar days. Cache publication can
lag KRX's latest-date endpoint; do not require KRX credentials to bridge that
lag. Never replace the existing map with an empty, partial, or older snapshot.
"""
import csv
import io
import http.client
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
import urllib.error
import urllib.request

CACHE_BASE = ('https://raw.githubusercontent.com/FinanceData/'
              'fdr_krx_data_cache/refs/heads/master/data/listing/krx')
MAX_STALE_DAYS = 7
MIN_COUNT = 2000
MIN_RETAINED_FRACTION = 0.90
MARKETS = {'STK', 'KSQ', 'KNX'}
MAX_CACHE_BYTES = 5_000_000


def parse_snapshot(payload, previous_count):
    """Fail closed on malformed/partial data; preserve six-character KRX codes."""
    rows = csv.DictReader(io.StringIO(payload.decode('utf-8-sig')))
    if not {'Code', 'Name', 'MarketId'}.issubset(rows.fieldnames or []):
        raise ValueError('Cache is missing Code, Name, or MarketId columns')
    mapping, markets, codes = {}, set(), set()
    for row in rows:
        code = (row.get('Code') or '').strip()
        name = (row.get('Name') or '').strip()
        market = (row.get('MarketId') or '').strip()
        if (not re.fullmatch(r'[0-9A-Z]{6}', code) or not name
                or name.lower() == 'nan' or market not in MARKETS
                or None in row or any(value is None for value in row.values())):
            raise ValueError('Malformed cache row')
        if name in mapping or code in codes:
            raise ValueError('Duplicate stock name or code in cache')
        mapping[name] = code
        codes.add(code)
        markets.add(market)
    if markets != MARKETS:
        raise ValueError('Cache does not cover KOSPI, KOSDAQ, and KONEX')
    if len(mapping) < max(MIN_COUNT, previous_count * MIN_RETAINED_FRACTION):
        raise ValueError(f'Cache is unexpectedly small: {len(mapping)} stocks')
    return mapping


def build(output_path='tickers.json', today=None):
    output_path = Path(output_path)
    today = today or datetime.now(timezone.utc).date()
    previous = json.loads(output_path.read_text(encoding='utf-8')) if output_path.exists() else {}
    previous_count = len(previous.get('map', {}))
    previous_date = previous.get('source_date')
    if previous_date:
        # Validate metadata before using it as a no-regression boundary.
        previous_date = datetime.strptime(previous_date, '%Y-%m-%d').date()

    mapping = None
    for stale_days in range(MAX_STALE_DAYS + 1):
        source_date = today - timedelta(days=stale_days)
        if previous_date and source_date < previous_date:
            break
        source_url = f'{CACHE_BASE}/{source_date.isoformat()}.csv'
        try:
            with urllib.request.urlopen(source_url, timeout=20) as response:
                payload = response.read(MAX_CACHE_BYTES + 1)
            if len(payload) > MAX_CACHE_BYTES:
                raise ValueError('Cache exceeds size limit')
            mapping = parse_snapshot(payload, previous_count)
            break
        except (urllib.error.URLError, http.client.HTTPException,
                TimeoutError, OSError, ValueError, csv.Error) as error:
            print(f'Cache {source_date} unavailable or invalid: {error}', file=sys.stderr)
    if mapping is None:
        print('No validated recent cache; existing tickers.json preserved.', file=sys.stderr)
        raise SystemExit(1)

    out = {
        'updated': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'source_date': source_date.isoformat(),
        'stale_days': stale_days,
        'source_url': source_url,
        'count': len(mapping),
        'source': 'fdr-cache',
        'map': mapping,
    }
    # Serialize fully and atomically replace only after validation succeeds.
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=output_path.parent,
                                         prefix='.tickers-', suffix='.tmp', delete=False) as handle:
            temporary_path = Path(handle.name)
            json.dump(out, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
        os.replace(temporary_path, output_path)
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()
    if stale_days:
        print(f'::warning::Using {source_date} cache ({stale_days} UTC calendar days old); '
              'this is not a fresh-day snapshot.')
    print(f'tickers.json: {len(mapping)} stocks; source_date={source_date}; '
          f'stale_days={stale_days}; source=fdr-cache')


if __name__ == '__main__':
    build()
