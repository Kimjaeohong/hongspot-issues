# hongspot-issues

## Stock-name mapping refresh

`python scripts/build_tickers.py` builds `tickers.json` using the same public
FinanceDataReader KRX cache, without a KRX account or Python package installs.
It checks UTC today's dated CSV first, then the previous seven calendar days,
newest first. Missing or invalid snapshots are skipped. If none is safe, the
command fails and keeps the existing file unchanged.

A snapshot must have valid codes/names, all three market IDs (KOSPI/KOSDAQ/KONEX),
no duplicate names/codes, at least 2,000 mappings, and at least 90% of the
existing mapping count. A dated existing snapshot cannot be replaced by an older
one. These conservative checks detect obvious truncation; they cannot prove the
upstream source is complete or correct. Updates use atomic file replacement.

- `updated`: UTC generation time, **not** the underlying data date
- `source_date`: date of the selected upstream cache file
- `stale_days`: UTC calendar-day age at generation time
- `source_url`: selected public cache URL
- `source`: `fdr-cache`

An older cache emits a GitHub Actions warning even when the build succeeds.
A cache filename is the upstream snapshot date, not a guarantee that every row
changed that day. Codes remain six-character strings, preserving leading zeros
and uppercase letters (for example, `005930`, `0015G0`, and `00680K`). Both the
ticker builder and name-change tracker accept these codes. The collector uses
the generated `map` directly to write `data/<code>.json` and `index.json`.

KRX expanded alphanumeric issuer codes from January 1, 2024; preferred shares
already used letters in their final character. See the
[KRX code-system notice](https://isin.krx.co.kr/info/notice.do?method=noticeView&contnId=112&pageIndex=1).
Validation checks the cache's six-character ASCII code format, not whether a
code has actually been issued; listing membership comes from the upstream cache.

Run all offline tests with `python -m unittest discover -v`. The refresh workflow
runs these tests before fetching a live snapshot. No collector rerun or change
to `seen.json` is needed for the tests.
