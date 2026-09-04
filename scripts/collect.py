#!/usr/bin/env python3
"""
특징주 뉴스 자동 수집기 (LLM 정제 버전)
────────────────────────────────────────────────────────
흐름:
  1) 네이버 API HUB 뉴스 검색에서 "특징주" 기사 수집
  2) 이미 처리한 기사(seen.json)는 건너뜀  ← 비용 절감 핵심
  3) 새 기사만 Claude Sonnet 에 보내 [종목명 | 사유 | 등락률] 정제
     - 기사 통짜 대신 HTS식 한 줄 요약
     - '이 기사의 주인공 종목'만 선별 (뒤에 나열된 종목 오매칭 방지)
     - 특징주가 아니거나 주인공 불명확하면 스킵
  4) 종목명을 tickers.json 으로 코드 변환
  5) data/<코드>.json 병합 (중복 제거, 최신순, pct 저장) + index.json 갱신
  6) 변경 코드 목록을 changed_codes.txt 로 남김 (워크플로우가 퍼지에 사용)

환경변수:
  NAVER_CLIENT_ID, NAVER_CLIENT_SECRET  - 네이버 API HUB
  ANTHROPIC_API_KEY                     - Claude API
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

MODEL = "claude-sonnet-5"      # 싼 도입가 소넷
QUERY = "특징주"
DISPLAY = 100
PAGES = 3                      # 최대 300건 훑기
DATA_DIR = "data"
TICKERS_FILE = "tickers.json"
INDEX_FILE = "index.json"
SEEN_FILE = "seen.json"        # 처리한 기사 URL 기록 (중복 스킵)
SEEN_MAX = 5000                # 오래된 기록은 잘라냄

UP_WORDS = re.compile(r"(급등|상승|강세|상한가|신고가|급반등|반등|폭등|오름|올라|↑)")
DOWN_WORDS = re.compile(r"(급락|하락|약세|하한가|폭락|내림|떨어|급감|↓)")


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
    """
    기사 제목+요약을 Claude Sonnet 에 보내 구조화 추출.
    반환: [{"name":..., "reason":..., "pct":float|None}]  (없으면 빈 리스트)
    """
    if not ANTHROPIC_KEY:
        print("ANTHROPIC_API_KEY 필요.", file=sys.stderr); sys.exit(1)

    system = (
        "너는 한국 증시 '특징주' 기사에서 핵심만 뽑는 추출기다. "
        "기사 제목과 요약을 보고, 이 기사가 다루는 '주인공 종목'만 골라라. "
        "단순히 뒤에 나열되거나 비교로 언급된 종목은 제외한다. "
        "각 주인공 종목에 대해 다음을 JSON 배열로만 출력한다(설명·마크다운 금지):\n"
        '[{"name":"종목명","reason":"한 줄 사유(20자 내외, ~에 급등/하락 형태)","pct":등락률숫자또는null}]\n'
        "규칙: reason은 기자명·매체명·날짜·상투구 빼고 사유만. "
        "pct는 상승이면 양수, 하락이면 음수. 등락률 없으면 null. "
        "특징주 기사가 아니거나 주인공 종목이 불명확하면 빈 배열 []만 출력."
    )
    user = f"제목: {title}\n요약: {desc}"

    payload = json.dumps({
        "model": MODEL,
        "max_tokens": 400,
        "system": system,
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

    # content[0].text 에서 JSON 파싱
    try:
        text = "".join(b.get("text", "") for b in resp.get("content", []))
        text = text.strip().strip("`")
        text = re.sub(r"^json\s*", "", text)
        arr = json.loads(text)
        if isinstance(arr, list):
            return arr
    except Exception:
        pass
    return []


def tone(text):
    if UP_WORDS.search(text): return "up"
    if DOWN_WORDS.search(text): return "down"
    return ""


def save_data(code, obj):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, f"{code}.json"), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def _sort(issues):
    issues.sort(key=lambda x: x["date"], reverse=True)


def merge_issue(code, name, date, text, pct):
    obj = load_json(os.path.join(DATA_DIR, f"{code}.json"), None) or {"name": name, "issues": []}
    if not obj.get("name"):
        obj["name"] = name
    key = f"{date}|{text}"
    for it in obj["issues"]:
        if f"{it['date']}|{it['text']}" == key:
            if pct is not None and it.get("pct") is None:
                it["pct"] = pct
                _sort(obj["issues"]); save_data(code, obj); return True
            return False
    issue = {"date": date, "text": text, "src": "auto"}
    if pct is not None:
        issue["pct"] = pct
    obj["issues"].append(issue)
    _sort(obj["issues"]); save_data(code, obj)
    return True


def save_index(idx):
    ordered = {k: idx[k] for k in sorted(idx.keys())}
    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(ordered, f, ensure_ascii=False, indent=2)


def main():
    name_to_code = load_tickers()
    index = load_json(INDEX_FILE, {})
    seen = load_json(SEEN_FILE, {})          # {url: iso처리시각}
    items = fetch_news()
    print(f"수집 기사 {len(items)}건")

    changed = set()
    index_changed = False
    llm_calls = 0

    for it in items:
        link = it.get("originallink") or it.get("link") or ""
        if not link or link in seen:
            continue                          # 이미 처리 → 스킵 (비용 절감)

        title = strip_tags(it.get("title", ""))
        desc = strip_tags(it.get("description", ""))
        seen[link] = datetime.now().isoformat(timespec="seconds")  # 결과와 무관하게 봤음 기록

        if "특징주" not in title:              # 1차 필터: 제목에 특징주 없으면 LLM도 안 부름
            continue

        pub = it.get("pubDate", "")
        try:
            date = datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %z").strftime("%Y.%m.%d")
        except Exception:
            date = datetime.now().strftime("%Y.%m.%d")

        stocks = llm_extract(title, desc)
        llm_calls += 1
        for s in stocks:
            name = str(s.get("name", "")).strip()
            reason = str(s.get("reason", "")).strip()
            pct = s.get("pct", None)
            if not name or len(reason) < 4:
                continue
            code = name_to_code.get(name)
            if not code:
                continue                      # 변환표에 없는 이름 → 스킵
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

    # seen 기록 정리 (최근 것만 유지)
    if len(seen) > SEEN_MAX:
        recent = sorted(seen.items(), key=lambda x: x[1], reverse=True)[:SEEN_MAX]
        seen = dict(recent)
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2)

    with open("changed_codes.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(sorted(changed)))

    print(f"LLM 호출 {llm_calls}건 / 변경 종목 {len(changed)}개: {', '.join(sorted(changed)) or '(없음)'}")
    print(f"index.json 변경: {'예' if index_changed else '아니오'}")


if __name__ == "__main__":
    main()
