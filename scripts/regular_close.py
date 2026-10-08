"""Fail-closed validation of independently reviewed regular-session evidence.

This module deliberately has no live provider adapter. A producer must verify
source semantics and actual trading-day adjacency before supplying the opt-in
file; our arithmetic checks cannot establish those external facts by themselves.
"""
from copy import deepcopy
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, DecimalException, ROUND_HALF_UP
import json
import os
import re
from urllib.parse import urlparse

KST = timezone(timedelta(hours=9))
MAX_EVIDENCE_BYTES = 2_000_000
MAX_EVIDENCE_ROWS = 10_000


def _number(value):
    if isinstance(value, bool) or value is None:
        raise ValueError('Missing or boolean price')
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError('Nonfinite price')
    return result


def _timestamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError('Timestamp lacks offset')
    return result.astimezone(KST)


def _https(value):
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return parsed.scheme == 'https' and bool(parsed.hostname) and not parsed.username


def validate_evidence(row, code, day, now=None):
    """Return calculated pct + traceable evidence, or None for any uncertainty.

    `day` is the article's KST date. Neither a latest quote nor yesterday's
    calendar date is silently substituted. Source contracts and original-file
    hashes are required attestations from an independently trusted producer,
    not a claim that this function downloaded or authenticated those files.
    """
    try:
        now = (now or datetime.now(timezone.utc)).astimezone(KST)
        target = datetime.strptime(day, '%Y.%m.%d').date()
        previous = datetime.strptime(row['previous_date'], '%Y-%m-%d').date()
        if (not re.fullmatch(r'[0-9A-Z]{6}', code)
                or row['code'] != code or row['source_code'] != code
                or row['previous_source_code'] != code
                or row['date'] != target.isoformat() or target > now.date()
                or target.weekday() >= 5 or previous.weekday() >= 5
                or previous >= target or row['reference_date'] != previous.isoformat()
                or row['session'] != 'KRX_REGULAR'
                or row['previous_session'] != 'KRX_REGULAR' or row['currency'] != 'KRW'):
            return None
        source = row['source']
        if (not isinstance(source['provider'], str) or not source['provider'].strip()
                or source['verification'] != 'independent-regular-session-review'
                or not all(_https(source[k]) for k in
                           ('url', 'previous_url', 'regular_session_contract'))
                or not all(re.fullmatch(r'[0-9a-f]{64}', source[k]) for k in
                           ('evidence_sha256', 'previous_evidence_sha256'))):
            return None
        for prefix, date in (('', target), ('previous_', previous)):
            end = _timestamp(row[prefix + 'session_close_at'])
            final = _timestamp(row[prefix + 'finalized_at'])
            retrieved = _timestamp(source[prefix + 'retrieved_at'])
            # Standard sessions and delayed exceptional sessions are safe only
            # after their asserted actual end. Never accept an earlier end.
            if (end.date() != date or end.time() < time(15, 30)
                    or not end <= final <= retrieved <= now):
                return None
        close = _number(row['close']); prior = _number(row['previous_close'])
        reference = _number(row['reference_price'])
        if close <= 0 or prior <= 0 or prior != reference:
            return None
        change = close - reference
        pct = (change / reference * 100).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        if (_number(row['reported_change']) != change
                or _number(row['reported_pct']) != pct):
            return None
        if ('high' in row) != ('low' in row):
            return None
        if 'high' in row:
            high = _number(row['high']); low = _number(row['low'])
            if not 0 < low <= close <= high:
                return None
        normalized = deepcopy(row)
        for key in ('close', 'previous_close', 'reference_price', 'reported_change',
                    'reported_pct', 'high', 'low'):
            if key in normalized:
                normalized[key] = str(_number(normalized[key]))
        # Keep only the defined contract, so arbitrary input cannot leak into rows.
        keys = ('code', 'source_code', 'previous_source_code', 'date', 'previous_date',
                'reference_date', 'session', 'previous_session', 'currency', 'close',
                'previous_close', 'reference_price', 'reported_change', 'reported_pct',
                'high', 'low', 'session_close_at', 'previous_session_close_at',
                'finalized_at', 'previous_finalized_at', 'source')
        normalized = {k: normalized[k] for k in keys if k in normalized}
        normalized['source'] = {k: source[k] for k in (
            'provider', 'url', 'previous_url', 'regular_session_contract', 'retrieved_at',
            'previous_retrieved_at', 'evidence_sha256', 'previous_evidence_sha256', 'verification')}
        return {'pct': float(pct), 'price_evidence': normalized}
    except (KeyError, TypeError, ValueError, DecimalException, OverflowError):
        return None


class TrustedEvidenceSource:
    """Small opt-in, offline intake; default is empty, never an article fallback."""
    def __init__(self, rows=(), now=None, unavailable_reason=None):
        self.now = now
        self.rows = {}
        self.cache = {}
        self.reasons = {}
        self.unavailable_reason = unavailable_reason
        if not isinstance(rows, list) or len(rows) > MAX_EVIDENCE_ROWS:
            self.unavailable_reason = 'invalid_evidence'
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = (str(row.get('code')), str(row.get('date')))
            if key in self.rows and self.rows[key] != row:
                self.rows[key] = None  # Conflicting duplicate remains invalid.
            elif key not in self.rows:
                self.rows[key] = row

    @classmethod
    def from_environment(cls, now=None):
        path = os.environ.get('KRX_TRUSTED_CLOSE_FILE', '')
        if not path:
            return cls([], now=now, unavailable_reason='no_configured_source')
        try:
            with open(path, 'rb') as handle:
                payload = handle.read(MAX_EVIDENCE_BYTES + 1)
            if len(payload) > MAX_EVIDENCE_BYTES:
                raise ValueError('Evidence file exceeds size limit')
            obj = json.loads(payload)
            if not isinstance(obj, dict) or obj.get('schema_version') != 1:
                raise ValueError('Unsupported evidence schema')
            return cls(obj.get('rows'), now=now)
        except OSError as error:
            print(f'Trusted close evidence unavailable: {error}')
            return cls([], now=now, unavailable_reason='source_unavailable')
        except (ValueError, TypeError) as error:
            print(f'Trusted close evidence invalid: {error}')
            return cls([], now=now, unavailable_reason='invalid_evidence')

    def get(self, code, day):
        key = (code, day)
        if key not in self.cache:
            evidence_key = (code, day.replace('.', '-'))
            row = self.rows.get(evidence_key)
            self.cache[key] = validate_evidence(row, code, day, now=self.now)
            if self.cache[key]:
                reason = None
            elif self.unavailable_reason:
                reason = self.unavailable_reason
            elif evidence_key not in self.rows:
                reason = 'missing_evidence'
            elif row is None:
                reason = 'duplicate_conflict'
            else:
                reason = 'invalid_evidence'
            self.reasons[key] = reason
        return deepcopy(self.cache[key])

    def pending_reason(self, code, day):
        """A bounded diagnostic category; no raw source exception/data is stored."""
        self.get(code, day)
        return self.reasons[(code, day)]
