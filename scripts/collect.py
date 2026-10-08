#!/usr/bin/env python3
"""
특징주 뉴스 자동 수집기 (LLM 정제 · 하루 1회 · 비용 상한형)
────────────────────────────────────────────────────────
비용 안전장치 3중:
  1) 하루 1회 실행 (워크플로우 cron) — 20분마다 아님
  2) 중복 스킵(seen.json) — 이미 본 기사는 LLM에 안 보냄
  3) 하루 LLM 처리 상한(MAX_LLM_CALLS) — 넘으면 그날은 중단

품질:
  - LLM에 "그 종목이 '왜' 움직였는지 이유가 담긴 기사만" 요청
  - 시황성 문장("동반 강세" 등)은 버리라고 명시
  - 하루·종목당 1이슈(발행시각·기사 URL 기준 결정)

환경변수: NAVER_CLIENT_ID, NAVER_CLIENT_SECRET, ANTHROPIC_API_KEY
"""
import os
import re
import sys
import json
import html
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

# Support script execution and importlib-based offline tests.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from regular_close import KST, TrustedEvidenceSource, validate_evidence

API_URL = "https://naverapihub.apigw.ntruss.com/search/v1/news"
NAVER_ID = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

MODEL = "claude-sonnet-5-5"
QUERY = "특징주"
DISPLAY = 100
PAGES = 3
MAX_LLM_CALLS = 100          # ★ 하루 LLM 호출 상한 (비용 상한). 넘으면 중단.
DATA_DIR = "data"
TICKERS_FILE = "tickers.json"
INDEX_FILE = "index.json"
SEEN_FILE = "seen.json"
SEEN_MAX = 8000
PENDING_FILE = "pending_closes.json"
MAX_CLOSE_LOOKUPS = 100
COLLECTOR_VERSION = "regular-close-v1"

UP_WORDS = re.compile(r"(급등|상승|강세|상한가|신고가|급반등|반등|폭등)")
DOWN_WORDS = re.compile(r"(급락|하락|약세|하한가|폭락|급감)")


def strip_tags(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def load_tickers():
    obj = load_json(TICKERS_FILE, None)
    if not obj:
        print(f"{TICKERS_FILE} 없음. build_tickers.py 먼저 실행.", file=sys.stderr)
        sys.exit(1)
    return obj.get("map", {})


def fetch_news():
    if not NAVER_ID or not NAVER_SECRET:
        print("NAVER 키 환경변수 필요.", file=sys.stderr); sys.exit(1)
    items = []
    for p in range(PAGES):
        start = 1 + p * DISPLAY
        if start > 1000:
            break
        qs = urllib.parse.urlencode({"query": QUERY, "display": DISPLAY,
                                     "start": start, "sort": "date"})
        req = urllib.request.Request(f"{API_URL}?{qs}")
        req.add_header("X-NCP-APIGW-API-KEY-ID", NAVER_ID)
        req.add_header("X-NCP-APIGW-API-KEY", NAVER_SECRET)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            print(f"뉴스 요청 실패(start={start}): {e}", file=sys.stderr); break
        batch = data.get("items", [])
        if not batch:
            break
        items.extend(batch)
        time.sleep(0.3)
    return items


PROBLEMS_FILE = "llm_problems.txt"   # 실패·거절 기록 → 워크플로 마지막 단계가 보고 실패 알림
problems = []


def llm_extract(title, desc):
    """기사에서 '이유 있는' 종목 이슈만 추출.
    반환: [{name, reason}] / 거절이면 [] / API 오류면 None(다음 실행에서 재시도)"""
    system = (
        "너는 한국 증시 '특징주' 기사에서 핵심만 뽑는 추출기다. "
        "기사 제목과 요약을 보고, 이 기사가 다루는 '주인공 종목'만 골라라. "
        "가장 중요한 규칙: 그 종목이 '왜' 오르거나 내렸는지 '구체적 이유'가 담긴 것만 뽑아라. "
        "'동반 강세', '코스피 상승 속', '반도체주 상승' 같은 이유 없는 시황성 문장은 버려라(빈 배열). "
        "단순히 뒤에 나열·비교로 언급된 종목도 제외한다. "
        "출력은 JSON 배열로만(설명·마크다운 금지):\n"
        '[{"name":"종목명","reason":"이유가 담긴 한 줄"}]\n'
        "reason은 기자명·매체·날짜·시각 빼고 '무엇 때문에 움직였는지'만 20~35자로. "
        "기사의 등락률·주가·퍼센트는 추출하지 마라. "
        "장중 움직임이면 '장중:'을, 상장 이후·여러 날의 누적 움직임이면 '누적:'을 reason 앞에 붙여라. "
        "기사 시점의 움직임을 당일 종가 움직임으로 단정하지 말고 원인·재료를 중립적으로 써라. "
        "이유가 불명확하거나 특징주가 아니면 반드시 빈 배열 []."
    )
    user = f"제목: {title}\n요약: {desc}"
    payload = json.dumps({
        # Sonnet 5.5는 thinking이 기본 켜짐 → 단순 추출이라 effort low로 비용·지연 최소화,
        # thinking 토큰도 max_tokens에 포함되므로 JSON이 잘리지 않게 여유를 둠
        "model": MODEL, "max_tokens": 1024, "system": system,
        "output_config": {"effort": "low"},
        "messages": [{"role": "user", "content": user}],
    }).encode("utf-8")
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=payload)
    req.add_header("x-api-key", ANTHROPIC_KEY)
    req.add_header("anthropic-version", "2023-06-01")
    req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        print(f"LLM 호출 실패 HTTP {e.code}: {body}", file=sys.stderr)
        problems.append(f"[API 오류 {e.code}] {title} | {body}")
        return None
    except Exception as e:
        print(f"LLM 호출 실패: {e}", file=sys.stderr)
        problems.append(f"[API 오류] {title} | {e}")
        return None
    if resp.get("stop_reason") == "refusal":
        cat = (resp.get("stop_details") or {}).get("category")
        problems.append(f"[거절 {cat}] {title}")
        return []
    try:
        text = "".join(b.get("text", "") for b in resp.get("content", [])
                       if b.get("type") == "text").strip().strip("`")
        text = re.sub(r"^json\s*", "", text)
        arr = json.loads(text)
        if not isinstance(arr, list):
            return []
        return [{"name": item.get("name"), "reason": item.get("reason")}
                for item in arr if isinstance(item, dict)]
    except Exception:
        return []


def save_data(code, obj):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, f"{code}.json"), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def _sort(issues):
    issues.sort(key=lambda x: x["date"], reverse=True)


def utc_now():
    return datetime.now(timezone.utc)


def _managed(issue):
    return (issue.get("src") == "auto"
            and issue.get("collector_version") == COLLECTOR_VERSION)


def scoped_reason(reason, title, desc):
    """Classify the selected price narrative, not the date of a business event."""
    def signals(text):
        cumulative = bool(re.search(
            r"(?:누적\s*(?:주가\s*)?(?:상승|하락|수익률|등락률))|"
            r"(?:상장\s*(?:이후|후)|공모가\s*대비|전고점\s*대비)"
            r"\s*(?:약\s*)?[+-]?\d+(?:\.\d+)?\s*%\s*(?:상승|하락|급등|급락)|"
            r"(?:상장\s*(?:이후|후)|공모가\s*대비|전고점\s*대비|연초\s*이후|올해|지난달|"
            r"최근\s*\d+\s*(?:일|거래일))"
            r"\s*(?:주가|주식|수익률).{0,12}(?:상승|하락|급등|급락)", text))
        intraday = bool(re.search(r"장중|\d+시.*현재", text))
        return cumulative, intraday

    prefixes = {"누적:": "cumulative", "장중:": "intraday", "복합:": "mixed"}
    explicit = next((scope for prefix, scope in prefixes.items()
                     if reason.startswith(prefix)), None)
    body = re.sub(r"^(?:누적|장중|복합):\s*", "", reason)
    cumulative, intraday = signals(body)
    if explicit == "cumulative":
        cumulative = True
    elif explicit == "intraday":
        intraday = True
    if explicit == "mixed":
        scope = "mixed"
    elif cumulative or intraday:
        scope = "mixed" if cumulative and intraday else "cumulative" if cumulative else "intraday"
    else:
        # A neutral selected reason may inherit an unambiguous article scope.
        # Both price narratives require an explicit mixed marker, never a guess.
        cumulative, intraday = signals(title + " " + desc)
        scope = "mixed" if cumulative and intraday else "cumulative" if cumulative else "intraday" if intraday else "unspecified"
    prefix = {"cumulative": "누적: ", "intraday": "장중: ", "mixed": "복합: "}.get(scope, "")
    return prefix + body, scope



def _reason_key(issue):
    article = issue.get("article", {})
    return (article.get("published_at", ""), article.get("url", ""), issue.get("text", ""))


def merge_issue(code, name, date, text, pct=None, *, article=None, close=None):
    """One deterministic reason per code/day; raw/article pct is ignored.

    Legacy and manual rows are deliberately outside the new pending scope.
    A close outage can never erase an already validated return.
    """
    if (not isinstance(code, str) or not re.fullmatch(r"[0-9A-Z]{6}", code)
            or not isinstance(article, dict) or not article):
        return False
    obj = load_json(os.path.join(DATA_DIR, f"{code}.json"), None) or {"name": name, "issues": []}
    matches = [(i, issue) for i, issue in enumerate(obj["issues"]) if issue["date"] == date]
    if len(matches) > 1 or any(not _managed(issue) for _, issue in matches):
        return False
    idx = matches[0][0] if matches else None
    new_issue = {"date": date, "text": text, "src": "auto",
                 "collector_version": COLLECTOR_VERSION, "article": article,
                 "close_status": "pending", "close_pending_reason": "awaiting_evidence"}
    validated = validate_evidence((close or {}).get("price_evidence"), code, date, now=utc_now())
    if validated:
        new_issue.update(validated)
        new_issue["close_status"] = "validated"
        new_issue.pop("close_pending_reason", None)
    if idx is not None:
        cur = obj["issues"][idx]
        # Choose independently of all price magnitudes, including article pct.
        if _reason_key(cur) <= _reason_key(new_issue):
            new_issue["text"] = cur["text"]
            new_issue["article"] = cur["article"]
        if not validated and cur.get("close_status") == "pending":
            new_issue["close_pending_reason"] = cur.get("close_pending_reason", "awaiting_evidence")
        if cur.get("close_status") == "validated":
            new_issue["pct"] = cur["pct"]
            new_issue["price_evidence"] = cur["price_evidence"]
            new_issue["close_status"] = "validated"
            new_issue.pop("close_pending_reason", None)
        if new_issue == cur:
            return False
        obj["issues"][idx] = new_issue
    else:
        obj["issues"].append(new_issue)
    _sort(obj["issues"]); save_data(code, obj)
    return True


def _pending_issue(code, date):
    if not isinstance(code, str) or not re.fullmatch(r"[0-9A-Z]{6}", code):
        return None
    obj = load_json(os.path.join(DATA_DIR, f"{code}.json"), {})
    rows = [row for row in obj.get("issues", []) if row.get("date") == date]
    if (len(rows) != 1 or not _managed(rows[0])
            or rows[0].get("close_status") != "pending"):
        return None
    return obj.get("name"), rows[0]



def _set_pending_reason(code, date, reason):
    current = _pending_issue(code, date)
    if not current or current[1].get("close_pending_reason") == reason:
        return False
    obj = load_json(os.path.join(DATA_DIR, f"{code}.json"), {})
    issue = next(row for row in obj["issues"] if row["date"] == date)
    issue["close_pending_reason"] = reason
    save_data(code, obj)
    return True


def save_index(idx):
    ordered = {k: idx[k] for k in sorted(idx.keys())}
    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(ordered, f, ensure_ascii=False, indent=2)


def main():
    if not ANTHROPIC_KEY:
        print("ANTHROPIC_API_KEY 필요.", file=sys.stderr); sys.exit(1)
    name_to_code = load_tickers()
    index = load_json(INDEX_FILE, {})
    seen = load_json(SEEN_FILE, {})
    now = utc_now()
    close_source = TrustedEvidenceSource.from_environment(now=now)
    # Only explicit entries created by this collector may be retried. Do not
    # scan historical data for blank pct fields.
    pending = load_json(PENDING_FILE, [])
    if not isinstance(pending, list):
        pending = []
    pending_order = []
    pending_keys = set()
    for row in pending:
        if (isinstance(row, dict) and isinstance(row.get("code"), str)
                and isinstance(row.get("date"), str)):
            key = (row["code"], row["date"])
            if key not in pending_keys:
                pending_order.append(key)
                pending_keys.add(key)
    items = fetch_news()
    print(f"수집 기사 {len(items)}건")

    changed = set()
    index_changed = False
    llm_calls = 0
    api_fail_streak = 0
    hit_cap = False

    for it in items:
        link = it.get("originallink") or it.get("link") or ""
        if not link or link in seen:
            continue
        title = strip_tags(it.get("title", ""))
        desc = strip_tags(it.get("description", ""))
        if "특징주" not in title:
            seen[link] = datetime.now().isoformat(timespec="seconds")
            continue

        # ★ 하루 상한 도달 시 중단 (seen에 기록 안 함 → 다음 실행에서 재시도 가능)
        if llm_calls >= MAX_LLM_CALLS:
            hit_cap = True
            break

        seen[link] = datetime.now().isoformat(timespec="seconds")

        pub = it.get("pubDate", "")
        try:
            pub_dt = parsedate_to_datetime(pub)
            if pub_dt.tzinfo is None:
                continue
            pub_dt = pub_dt.astimezone(KST)
        except (TypeError, ValueError, OverflowError):
            continue
        if pub_dt.weekday() >= 5:   # 토·일 기사는 거래일이 아니라 날짜가 틀어짐 → LLM 호출 전에 스킵
            continue
        date = pub_dt.strftime("%Y.%m.%d")

        stocks = llm_extract(title, desc)
        llm_calls += 1
        if stocks is None:          # API 오류: 본 것으로 치지 않고 다음 실행에서 재시도
            seen.pop(link, None)
            api_fail_streak += 1
            if api_fail_streak >= 5:  # 키 만료·크레딧 소진 등 → 계속 두드리지 말고 중단
                problems.append("[중단] API 오류 5회 연속 — 키/크레딧/장애 확인 필요")
                break
            continue
        api_fail_streak = 0

        for s in stocks:
            if not isinstance(s, dict):   # LLM이 문자열 등 예상외 형태로 준 경우 방어
                continue
            name = str(s.get("name", "")).strip()
            reason = s.get("reason")
            reason = reason.strip() if isinstance(reason, str) else ""
            if not name or len(reason) < 6:
                continue
            code = name_to_code.get(name)
            if not code:
                continue
            reason, scope = scoped_reason(reason, title, desc)
            article = {"url": link, "published_at": pub_dt.isoformat(),
                       "title": title, "time_scope": scope}
            if merge_issue(code, name, date, reason, article=article):
                changed.add(code)
                if index.get(name) != code:
                    index[name] = code
                    index_changed = True
            if _pending_issue(code, date):
                pending_keys.add((code, date))

    # Keep persisted order. Failed attempted rows rotate behind unattempted
    # work so a long outage cannot permanently starve later pending rows.
    queued = set(pending_order)
    pending_order.extend(sorted(pending_keys - queued, key=lambda key: (key[1], key[0])))
    attempted, deferred = [], []
    lookups = 0
    for code, date in pending_order:
        current = _pending_issue(code, date)
        if not current:
            continue
        name, issue = current
        did_attempt = lookups < MAX_CLOSE_LOOKUPS
        if did_attempt:
            lookups += 1
            try:
                close = close_source.get(code, date)
                reason = close_source.pending_reason(code, date)
            except Exception as error:
                print(f"종가 검증 보류 {code} {date}: {error}", file=sys.stderr)
                close, reason = None, "source_unavailable"
            if close and merge_issue(code, name, date, issue["text"],
                                     article=issue["article"], close=close):
                changed.add(code)
            reason = reason or "invalid_evidence"
        else:
            reason = "budget_deferred"
        if _pending_issue(code, date):
            if _set_pending_reason(code, date, reason):
                changed.add(code)
            (attempted if did_attempt else deferred).append({"code": code, "date": date})
    remaining = deferred + attempted
    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump(remaining, f, ensure_ascii=False, indent=2)
    print(f"종가 검증 보류 {len(remaining)}건 (검증된 근거 없으면 pct 미기입)")

    if index_changed:
        save_index(index)
    if len(seen) > SEEN_MAX:
        seen = dict(sorted(seen.items(), key=lambda x: x[1], reverse=True)[:SEEN_MAX])
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2)
    with open("changed_codes.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(sorted(changed)))
    with open(PROBLEMS_FILE, "w", encoding="utf-8") as f:   # 비어 있으면 정상
        f.write("\n".join(problems))
    if problems:
        print(f"⚠ LLM 실패·거절 {len(problems)}건 (워크플로 마지막 단계에서 실패 알림)")

    print(f"LLM 호출 {llm_calls}건" + (" (하루 상한 도달)" if hit_cap else ""))
    print(f"변경 종목 {len(changed)}개: {', '.join(sorted(changed)) or '(없음)'}")
    print(f"index.json 변경: {'예' if index_changed else '아니오'}")


if __name__ == "__main__":
    main()
