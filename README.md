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


## Collector daily-return safety

The collector extracts a reason only. It ignores every article/LLM percentage,
including intraday, rounded, and cumulative numbers. The selected narrative
retains a matching cumulative/intraday prefix; mixed narratives use `복합:`.
Dates of earnings or contracts alone do not imply cumulative stock returns. Reasons are selected by the
lowest `(KST publication timestamp, article URL, reason)` key, independently of
price magnitude. Malformed publication dates are skipped; weekend articles are
skipped; a holiday article is never silently moved to another trading day.

**Automatic market-price completion is currently unavailable.** There is no
accepted live price adapter. With the normal configuration, new issues contain
`close_status: "pending"` and no `pct`. This is intentional, and reason collection
still runs. `pending_closes.json` retries only explicitly queued rows created by
this collector version, at most 100 code/date lookups per run. Failed attempts
rotate behind deferred work in persisted queue order so later rows get a turn.
Pending reasons distinguish unconfigured source, missing/invalid/conflicting
evidence, source unavailability, and budget deferral. It does not scan
or fill old blank percentages. Manual rows, pre-existing legacy auto rows, and
already validated returns remain protected. Seen articles need no new LLM call
for a pending close retry. The workflow tests before collecting and commits the
pending queue with ordinary collector outputs.

When independently reviewed regular-session evidence becomes available, set
`KRX_TRUSTED_CLOSE_FILE` to a local JSON file described in
[the trusted evidence contract](docs/regular-close-evidence.md). This is an
opt-in operator-trusted intake, **not** a source-verification or scraping API.
The producer must verify the original source and actual previous trading date.
The validator cannot prove those external facts from a label or a hash alone.

After validation, `pct` is calculated with Decimal and rounded to two decimal
places (half up) from the current regular close and verified previous regular
reference. The article URL/time/scope and source evidence are retained as
additional fields; existing `date`, `text`, `src`, and numeric `pct` fields remain
compatible with clients. A missing, wrong-date, wrong-symbol, unknown-session,
nonfinite, or inconsistent value stays pending. No article fallback is used.
