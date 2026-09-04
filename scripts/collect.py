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
  - 하루·종목당 1이슈(등락률 큰 것)

환경변수: NAVER_CLIENT_ID, NAVER_CLIENT_SECRET, ANTHROPIC_API_KEY
"""
import os
import re
import sys
import json
import html
import time
import urllib.parse
import urllib.request
from datetime import datetime

API_URL = "https://naverapihub.apigw.ntruss.com/search/v1/news"
NAVER_ID = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

MODEL = "claude-sonnet-5"
QUERY = "특징주"
DISPLAY = 100
PAGES = 3
MAX_LLM_CALLS = 100          # ★ 하루 LLM 호출 상한 (비용 상한). 넘으면 중단.
DATA_DIR = "data"
TICKERS_FILE = "tickers.json"
INDEX_FILE = "index.json"
SEEN_FILE = "seen.json"
SEEN_MAX = 8000

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


def llm_extract(title, desc):
    """기사에서 '이유 있는' 종목 이슈만 추출. 반환: [{name, reason, pct}]"""
    system = (
        "너는 한국 증시 '특징주' 기사에서 핵심만 뽑는 추출기다. "
        "기사 제목과 요약을 보고, 이 기사가 다루는 '주인공 종목'만 골라라. "
        "가장 중요한 규칙: 그 종목이 '왜' 오르거나 내렸는지 '구체적 이유'가 담긴 것만 뽑아라. "
        "'동반 강세', '코스피 상승 속', '반도체주 상승' 같은 이유 없는 시황성 문장은 버려라(빈 배열). "
        "단순히 뒤에 나열·비교로 언급된 종목도 제외한다. "
        "출력은 JSON 배열로만(설명·마크다운 금지):\n"
        '[{"name":"종목명","reason":"이유가 담긴 한 줄(예: 40조원 자사주 매입 발표에 급등)","pct":등락률숫자또는null}]\n'
        "reason은 기자명·매체·날짜·시각 빼고 '무엇 때문에 움직였는지'만 20~35자로. "
        "pct는 상승 양수/하락 음수, 없으면 null. "
        "이유가 불명확하거나 특징주가 아니면 반드시 빈 배열 []."
    )
    user = f"제목: {title}\n요약: {desc}"
    payload = json.dumps({
        "model": MODEL, "max_tokens": 400, "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode("utf-8")
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=payload)
    req.add_header("x-api-key", ANTHROPIC_KEY)
    req.add_header("anthropic-version", "2023-06-01")
    req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"LLM 호출 실패: {e}", file=sys.stderr)
        return []
    try:
        text = "".join(b.get("text", "") for b in resp.get("content", [])).strip().strip("`")
        text = re.sub(r"^json\s*", "", text)
        arr = json.loads(text)
        return arr if isinstance(arr, list) else []
    except Exception:
        return []


def save_data(code, obj):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, f"{code}.json"), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def _sort(issues):
    issues.sort(key=lambda x: x["date"], reverse=True)


def _absp(v):
    return abs(v) if isinstance(v, (int, float)) else -1


def merge_issue(code, name, date, text, pct):
    """하루·종목당 auto 이슈 1개(등락률 큰 것). manual은 안 건드림."""
    obj = load_json(os.path.join(DATA_DIR, f"{code}.json"), None) or {"name": name, "issues": []}
    if not obj.get("name"):
        obj["name"] = name
    idx = None
    for i, it in enumerate(obj["issues"]):
        if it["date"] == date and it.get("src") == "auto":
            idx = i; break
    new_issue = {"date": date, "text": text, "src": "auto"}
    if pct is not None:
        new_issue["pct"] = pct
    if idx is not None:
        cur = obj["issues"][idx]
        if cur["text"] == text and cur.get("pct") == pct:
            return False
        if _absp(pct) > _absp(cur.get("pct")):
            obj["issues"][idx] = new_issue
            _sort(obj["issues"]); save_data(code, obj); return True
        return False
    for it in obj["issues"]:
        if it["date"] == date and it["text"] == text:
            return False
    obj["issues"].append(new_issue)
    _sort(obj["issues"]); save_data(code, obj)
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
    items = fetch_news()
    print(f"수집 기사 {len(items)}건")

    changed = set()
    index_changed = False
    llm_calls = 0
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
        stocks = llm_extract(title, desc)
        llm_calls += 1

        pub = it.get("pubDate", "")
        try:
            date = datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %z").strftime("%Y.%m.%d")
        except Exception:
            date = datetime.now().strftime("%Y.%m.%d")

        for s in stocks:
            name = str(s.get("name", "")).strip()
            reason = str(s.get("reason", "")).strip()
            pct = s.get("pct", None)
            if not name or len(reason) < 6:
                continue
            code = name_to_code.get(name)
            if not code:
                continue
            if isinstance(pct, str):
                m = re.search(r"[+\-]?\d+(?:\.\d+)?", pct)
                pct = float(m.group()) if m else None
            if merge_issue(code, name, date, reason, pct):
                changed.add(code)
                if index.get(name) != code:
                    index[name] = code
                    index_changed = True

    if index_changed:
        save_index(index)
    if len(seen) > SEEN_MAX:
        seen = dict(sorted(seen.items(), key=lambda x: x[1], reverse=True)[:SEEN_MAX])
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2)
    with open("changed_codes.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(sorted(changed)))

    print(f"LLM 호출 {llm_calls}건" + (" (하루 상한 도달)" if hit_cap else ""))
    print(f"변경 종목 {len(changed)}개: {', '.join(sorted(changed)) or '(없음)'}")
    print(f"index.json 변경: {'예' if index_changed else '아니오'}")


if __name__ == "__main__":
    main()
