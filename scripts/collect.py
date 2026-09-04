#!/usr/bin/env python3
"""
특징주 뉴스 자동 수집기 (규칙 기반, LLM 없음 · 비용 0)
────────────────────────────────────────────────────────
흐름:
  1) 네이버 API HUB 뉴스 검색에서 "특징주" 기사 수집
  2) 이미 처리한 기사(seen.json)는 건너뜀
  3) 규칙(정규식)으로 [종목명 + 사유 + 등락률] 정제
     - 종목명: 제목에 있는 종목만 인정(오매칭 방지) + tickers.json 대조
     - 사유: 기자명/매체/날짜/시각 등 상투구 제거 후 사유절만
     - 등락률: +9.5% / 12% 패턴, 하락 키워드면 음수
     - 사유가 부실하거나 종목이 제목에 없으면 스킵(질 우선)
  4) data/<코드>.json 병합(중복 제거, 최신순, pct) + index.json 갱신
  5) 변경 코드 목록 changed_codes.txt

환경변수: NAVER_CLIENT_ID, NAVER_CLIENT_SECRET
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

QUERY = "특징주"
DISPLAY = 100
PAGES = 3
DATA_DIR = "data"
TICKERS_FILE = "tickers.json"
INDEX_FILE = "index.json"
SEEN_FILE = "seen.json"
SEEN_MAX = 5000

UP_WORDS = re.compile(r"(급등|상승|강세|상한가|신고가|급반등|반등|폭등|오름세?|올라|치솟)")
DOWN_WORDS = re.compile(r"(급락|하락|약세|하한가|폭락|내림세?|떨어|급감|미끄)")

# 사유에서 걷어낼 상투구(앞부분 잡음)
NOISE_PATTERNS = [
    r"^\[특징주[^\]]*\]\s*",
    r"[가-힣]+\s*=?\s*[가-힣]{2,4}\s*기자\s*[|｜]\s*",   # "이투데이=임하은 기자 |"
    r"[가-힣]{2,4}\s*기자\s*[|｜=]\s*",
    r"\d+일\s*(한국거래소|코스피|코스닥|유가증권시장)에?\s*따르면\s*",
    r"오[전후]\s*\d+시\s*\d*분?\s*(기준|현재)?\s*",
    r"\d+일\s*(오[전후])?\s*(장\s*(초반|중|마감|막판))?\s*",
    r"^\s*[가-힣A-Za-z0-9]+\s*\(\d{6}\)\s*[은는이가]\s*",   # "삼성전자(005930)은 "
]


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


def parse_pct(text):
    m = re.search(r"([+\-]?\d+(?:\.\d+)?)\s*%", text)
    if not m:
        return None
    val = float(m.group(1))
    if not re.match(r"[+\-]", m.group(1)) and DOWN_WORDS.search(text):
        val = -abs(val)
    return val


def find_title_stocks(title, name_to_code):
    """제목에 등장한 종목만 인정 (오매칭 방지)."""
    found = {}
    # 코드 동반
    for m in re.finditer(r"([가-힣A-Za-z0-9]+)\s*\((\d{6})\)", title):
        found[m.group(2)] = m.group(1)
    # 이름 매칭 (3글자 이상만, 제목 안에서만)
    for name, code in name_to_code.items():
        if len(name) < 3 or code in found:
            continue
        if name in title:
            found[code] = name
    return list(found.items())


def clean_reason(text, name):
    t = text
    for pat in NOISE_PATTERNS:
        t = re.sub(pat, "", t)
    t = re.sub(rf"{re.escape(name)}\s*\(\d{{6}}\)", name, t)  # 코드 괄호 제거(이름은 유지)
    t = re.sub(r"\(\d{6}\)", "", t)
    t = re.sub(r"\[특징주[^\]]*\]|특징주", "", t)
    t = re.sub(r"[+\-]?\d+(?:\.\d+)?\s*%\s*(대|가량|안팎|이상|가까이)?", "", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" ·-,·|")
    # 첫 문장만 사용 (사유는 대개 첫 문장에)
    t = re.split(r"(?<=[다\.])\s", t)[0].strip()
    return t


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


def _absp(v):
    return abs(v) if isinstance(v, (int, float)) else -1

def merge_issue(code, name, date, text, pct):
    """
    하루·종목당 이슈 1개 원칙.
    같은 (종목, 날짜)에 이미 auto 이슈가 있으면:
      - 새 이슈의 |등락률|이 더 크면 교체, 아니면 무시
      - 단 manual(수동 입력) 이슈는 절대 건드리지 않음
    """
    obj = load_json(os.path.join(DATA_DIR, f"{code}.json"), None) or {"name": name, "issues": []}
    if not obj.get("name"):
        obj["name"] = name

    # 같은 날 auto 이슈 탐색
    same_day_auto_idx = None
    for i, it in enumerate(obj["issues"]):
        if it["date"] == date and it.get("src") == "auto":
            same_day_auto_idx = i
            break

    new_issue = {"date": date, "text": text, "src": "auto"}
    if pct is not None:
        new_issue["pct"] = pct

    if same_day_auto_idx is not None:
        cur = obj["issues"][same_day_auto_idx]
        # 완전 동일하면 무시
        if cur["text"] == text and cur.get("pct") == pct:
            return False
        # 새 것의 |등락률|이 더 크면 교체, 아니면 유지
        if _absp(pct) > _absp(cur.get("pct")):
            obj["issues"][same_day_auto_idx] = new_issue
            _sort(obj["issues"]); save_data(code, obj); return True
        return False

    # 같은 날 auto 없음 → 추가 (단 manual과 완전 중복이면 skip)
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
    name_to_code = load_tickers()
    index = load_json(INDEX_FILE, {})
    seen = load_json(SEEN_FILE, {})
    items = fetch_news()
    print(f"수집 기사 {len(items)}건")

    changed = set()
    index_changed = False

    for it in items:
        link = it.get("originallink") or it.get("link") or ""
        if not link or link in seen:
            continue
        title = strip_tags(it.get("title", ""))
        desc = strip_tags(it.get("description", ""))
        seen[link] = datetime.now().isoformat(timespec="seconds")

        if "특징주" not in title:
            continue

        stocks = find_title_stocks(title, name_to_code)   # 제목에 있는 종목만
        if not stocks:
            continue

        pub = it.get("pubDate", "")
        try:
            date = datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %z").strftime("%Y.%m.%d")
        except Exception:
            date = datetime.now().strftime("%Y.%m.%d")

        pct = parse_pct(f"{title} {desc}")
        reason = clean_reason(desc or title, stocks[0][1])
        if len(reason) < 6:            # 사유 부실하면 제목으로 대체 시도
            reason = clean_reason(title, stocks[0][1])
        if len(reason) < 8:            # 너무 짧으면 스킵(질 우선)
            continue
        # 쓰레기 사유 필터: 출처성 조각, 사유형 아닌 문장 제외
        if re.search(r"(딜링룸|증권\s*리서치|편집자|무단\s*전재|저작권|기자\s*=|앵커)", reason):
            continue
        if not (UP_WORDS.search(reason) or DOWN_WORDS.search(reason) or "%" in (title+desc)):
            continue  # 상승/하락 단서도 등락률도 없으면 특징주 사유로 부적합

        for code, name in stocks:
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

    print(f"변경 종목 {len(changed)}개: {', '.join(sorted(changed)) or '(없음)'}")
    print(f"index.json 변경: {'예' if index_changed else '아니오'}")


if __name__ == "__main__":
    main()
