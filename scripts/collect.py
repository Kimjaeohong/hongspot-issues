#!/usr/bin/env python3
"""
특징주 뉴스 자동 수집기
────────────────────────────────────────────────────────
흐름:
  1) 네이버 API HUB 뉴스 검색에서 "특징주" 기사 수집
  2) 기사(제목+요약)에서 [종목명 + 등락률 + 사유] 규칙 파싱
  3) 종목명을 tickers.json 으로 코드 변환
  4) data/<코드>.json 에 병합 (중복 제거, 최신순, pct 저장)
  5) 변경된 종목 코드 목록을 changed_codes.txt 로 남김 (워크플로우가 CDN 퍼지에 사용)

환경변수:
  NAVER_CLIENT_ID     - API HUB Client ID
  NAVER_CLIENT_SECRET - API HUB Client Secret

주의: 커밋/푸시/퍼지는 이 스크립트가 하지 않고 GitHub Actions 워크플로우가 담당.
"""
import os
import re
import sys
import json
import html
import time
import glob
import urllib.parse
import urllib.request
from datetime import datetime

API_URL = "https://naverapihub.apigw.ntruss.com/search/v1/news"
CLIENT_ID = os.environ.get("NAVER_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")

QUERY = "특징주"          # 검색어
DISPLAY = 100             # 한 번에 최대 100건
PAGES = 3                 # 최대 300건까지 훑기 (start=1,101,201)
DATA_DIR = "data"
TICKERS_FILE = "tickers.json"

# 상승/하락 키워드 (등락률 부호 보정, 텍스트 색상 판단용)
UP_WORDS = re.compile(r"(급등|상승|강세|상한가|신고가|급반등|반등|폭등|오름|올라|↑)")
DOWN_WORDS = re.compile(r"(급락|하락|약세|하한가|폭락|내림|떨어|급감|↓)")


def strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)          # <b> 등 제거
    return html.unescape(s).strip()


def load_tickers():
    if not os.path.exists(TICKERS_FILE):
        print(f"{TICKERS_FILE} 없음. 먼저 build_tickers.py 실행 필요.", file=sys.stderr)
        sys.exit(1)
    with open(TICKERS_FILE, encoding="utf-8") as f:
        obj = json.load(f)
    return obj.get("map", {})


def fetch_news():
    if not CLIENT_ID or not CLIENT_SECRET:
        print("NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 환경변수가 필요합니다.", file=sys.stderr)
        sys.exit(1)
    items = []
    for p in range(PAGES):
        start = 1 + p * DISPLAY
        if start > 1000:
            break
        qs = urllib.parse.urlencode({
            "query": QUERY, "display": DISPLAY, "start": start, "sort": "date",
        })
        req = urllib.request.Request(f"{API_URL}?{qs}")
        req.add_header("X-NCP-APIGW-API-KEY-ID", CLIENT_ID)
        req.add_header("X-NCP-APIGW-API-KEY", CLIENT_SECRET)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            print(f"뉴스 요청 실패(start={start}): {e}", file=sys.stderr)
            break
        batch = data.get("items", [])
        if not batch:
            break
        items.extend(batch)
        time.sleep(0.3)  # 예의상 지연
    return items


def parse_pct(text):
    """텍스트에서 등락률(%)을 추출. 부호 없으면 상/하락 키워드로 보정."""
    m = re.search(r"([+\-]?\d+(?:\.\d+)?)\s*%", text)
    if not m:
        return None
    val = float(m.group(1))
    if not re.match(r"[+\-]", m.group(1)):
        if DOWN_WORDS.search(text):
            val = -abs(val)
    return val


def find_stocks(text, name_to_code):
    """
    텍스트에서 종목을 찾아 [(code, name)] 반환.
    1) "종목명(005930)" 코드 동반 패턴 → 최우선, 무조건 인정
    2) 이름만 등장 → 전종목 표 매칭. 단 3글자 미만 이름은 오매칭 위험 커서 코드 동반일 때만.
    """
    found = {}  # code -> name

    # 1) 코드 동반 패턴: 종목명(123456)
    for m in re.finditer(r"([가-힣A-Za-z0-9]+)\s*\((\d{6})\)", text):
        nm, code = m.group(1), m.group(2)
        found[code] = nm

    # 2) 이름 매칭 (3글자 이상만, 오매칭 억제)
    for name, code in name_to_code.items():
        if len(name) < 3:
            continue
        if code in found:
            continue
        if name in text:
            found[code] = name

    return list(found.items())


def clean_reason(text, name):
    """사유 문장 정리: 종목명·코드·등락률·언론사 태그 등 노이즈 제거."""
    t = text
    t = re.sub(rf"{re.escape(name)}\s*\(\d{{6}}\)", "", t)   # 종목명(코드)
    t = re.sub(r"\(\d{6}\)", "", t)                          # (코드)
    t = re.sub(r"\[특징주\]|\[특징주 종합\]|특징주", "", t)   # 특징주 머리표
    t = re.sub(r"[+\-]?\d+(?:\.\d+)?\s*%\s*(대|가량|안팎|이상|가까이)?", "", t)  # 등락률(+조사)
    t = re.sub(r"\s{2,}", " ", t).strip(" ·-,")
    return t.strip()


def tone(text):
    if UP_WORDS.search(text):
        return "up"
    if DOWN_WORDS.search(text):
        return "down"
    return ""


INDEX_FILE = "index.json"


def load_index():
    if os.path.exists(INDEX_FILE):
        with open(INDEX_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_index(idx):
    # 종목명 가나다순 정렬해서 저장 (diff 깔끔하게)
    ordered = {k: idx[k] for k in sorted(idx.keys())}
    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(ordered, f, ensure_ascii=False, indent=2)


def load_data(code):
    path = os.path.join(DATA_DIR, f"{code}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return None


def save_data(code, obj):
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, f"{code}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def merge_issue(code, name, date, text, pct):
    """기존 data/<code>.json 에 새 이슈 병합. 변경 있으면 True."""
    obj = load_data(code) or {"name": name, "issues": []}
    if not obj.get("name"):
        obj["name"] = name

    key = f"{date}|{text}"
    for it in obj["issues"]:
        if f"{it['date']}|{it['text']}" == key:
            # 이미 있음. pct 비어있으면 채워주기
            if pct is not None and it.get("pct") is None:
                it["pct"] = pct
                _sort(obj["issues"])
                save_data(code, obj)
                return True
            return False

    issue = {"date": date, "text": text, "src": "auto"}
    if pct is not None:
        issue["pct"] = pct
    obj["issues"].append(issue)
    _sort(obj["issues"])
    save_data(code, obj)
    return True


def _sort(issues):
    issues.sort(key=lambda x: x["date"], reverse=True)


def main():
    name_to_code = load_tickers()
    index = load_index()
    items = fetch_news()
    print(f"수집 기사 {len(items)}건")

    changed = set()
    index_changed = False
    for it in items:
        title = strip_tags(it.get("title", ""))
        desc = strip_tags(it.get("description", ""))
        blob = f"{title} {desc}"

        # 특징주 기사만 (제목에 특징주 없으면 스킵 → 잡음 억제)
        if "특징주" not in title:
            continue

        pub = it.get("pubDate", "")
        try:
            dt = datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %z")
            date = dt.strftime("%Y.%m.%d")
        except Exception:
            date = datetime.now().strftime("%Y.%m.%d")

        pct = parse_pct(blob)
        stocks = find_stocks(blob, name_to_code)
        if not stocks:
            continue

        for code, name in stocks:
            reason = clean_reason(desc or title, name)
            if len(reason) < 5:      # 사유가 너무 짧으면 스킵
                continue
            if merge_issue(code, name, date, reason, pct):
                changed.add(code)
                if index.get(name) != code:
                    index[name] = code
                    index_changed = True

    if index_changed:
        save_index(index)

    # 변경된 종목 코드 기록 (워크플로우가 퍼지에 사용)
    with open("changed_codes.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(sorted(changed)))
    print(f"변경된 종목 {len(changed)}개: {', '.join(sorted(changed)) or '(없음)'}")
    print(f"index.json 변경: {'예' if index_changed else '아니오'}")


if __name__ == "__main__":
    main()
