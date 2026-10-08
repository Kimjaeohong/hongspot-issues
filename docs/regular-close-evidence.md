# Trusted regular-close evidence contract

## Current limitation

No live public adapter has been accepted. As of the 2026-10-08 investigation:

- Anonymous KRX STAT reads returned HTTP 400 and public page reads returned 403;
  current [PyKrx instructions](https://github.com/sharebook-kr/pykrx#readme)
  require KRX login for authenticated APIs. No access restriction was bypassed
- The public [FinanceData KRX cache](https://github.com/FinanceData/fdr_krx_data_cache)
  uses exact `trdDd` with `MDCSTAT01501`, but snapshots are overwritten intraday
  and contain no embedded final-session confirmation. Its Oct7 Neo snapshot's
  close was below its low, and it conflicted with Oct8's reported reference
- Yahoo's Oct8 daily close was null during the smoke read; its regular-session
  end metadata was 15:00 KST, inconsistent with the normal KRX 15:30 close
- A filename, a commit made after close, an exchange label, or a current quote
  alone cannot establish regular-session provenance. Naver `/price` daily
  history and AlphaSquare session labels are not used

These checks were read-only, with no LLM request and no collector dataset write.
They demonstrate the source blocker, not a successful public adapter.

The [KRX trading rules](https://regulation.krx.co.kr/contents/RGL/03/03010100/RGL03010100T1.jsp)
separate regular 09:00–15:30 sessions from after-hours sessions. Exceptional
session schedules must be verified explicitly by the evidence producer.

## Opt-in intake

Set `KRX_TRUSTED_CLOSE_FILE` only for evidence produced by a trusted operator or
pipeline that has independently reviewed the original source. The file is local,
read-only, at most 2 MB, and must have this envelope:

```json
{"schema_version": 1, "rows": []}
```

Each row requires the fields below. `tests/test_regular_close.py:evidence` is a
complete **synthetic test fixture**, not market data and not deployable evidence.

- `code`, `source_code`, `previous_source_code`: exact six-character KRX codes,
  including leading zeros and uppercase letters
- `date`, `previous_date`, `reference_date`: ISO dates. `date` must exactly match
  the issue's KST publication date. The other two must agree and precede it
- `session`, `previous_session`: `KRX_REGULAR`; `currency`: `KRW`
- `close`, `previous_close`, `reference_price`: positive finite numeric strings.
  Previous close and reference price must agree. Corporate-action adjusted
  references or non-adjacent/stale prior rows require further verification and
  are not silently inferred or substituted
- `reported_change`, `reported_pct`: independently sourced daily change and
  percentage for arithmetic consistency checks, never an article number
- Optional paired `high`, `low`: finite same-date regular-session prices; require
  `0 < low <= close <= high`. The collector makes no unverified high inference
- `session_close_at`, `previous_session_close_at`: actual verified regular end
  timestamps with timezone offsets, on each price's date and no earlier than
  15:30 KST. A delayed exceptional session must use its actual later close
- `finalized_at`, `previous_finalized_at`: original source's verified finalization
  times, after the corresponding regular end
- `source`: the following producer-attested provenance fields:
  - `provider`: source provider name
  - `url`, `previous_url`: HTTPS links to the relevant original records/export
  - `regular_session_contract`: HTTPS link documenting source session semantics
  - `retrieved_at`, `previous_retrieved_at`: offset-bearing timestamps, no earlier
    than corresponding finalization and no later than the collector's clock
  - `evidence_sha256`, `previous_evidence_sha256`: lowercase SHA-256 digests of
    retained original evidence. They are traceability references; the intake
    does not download originals or validate their bytes against these digests
  - `verification`: `independent-regular-session-review`

The producer is responsible for proving exact instrument identity, source
regular-session semantics, actual immediately preceding trading date (including
holidays/suspensions), unchanged reference basis, finalization, and truthful
provenance. A hand-written session label or fabricated metadata does not meet
this contract. Do not configure an unreviewed article/quote converter as a
trusted producer.

Validation rejects inconsistent prices/references, future or weekend dates,
unknown/after-hours sessions, premature timestamps, missing provenance, malformed
or nonfinite numbers, and conflicting duplicate code/date rows. Identical
repeated records are safe. Values are cached per code/date during one run.

The displayed return is independently calculated as
`(close / reference_price - 1) * 100`, rounded with Decimal `ROUND_HALF_UP` to
2 decimal places. `reported_pct` is only a consistency check. With no valid
trusted evidence, reasons and retryable pending status remain available.


## Pending diagnostics and fair retries

New managed rows retain additive `close_pending_reason` categories:
`no_configured_source`, `missing_evidence`, `invalid_evidence`,
`duplicate_conflict`, `source_unavailable`, or `budget_deferred`.
`awaiting_evidence` is the initial state before the first bounded intake check.
Successful validation removes the pending reason. No raw source errors are
stored in this field.

Pending queue order is durable. Each run checks at most 100 code/date entries,
then puts unsuccessful attempted entries behind work deferred by the budget.
New rows join the tail. Thus unresolved older rows cannot permanently starve
later entries, and no historic blank fields are discovered or enrolled.
