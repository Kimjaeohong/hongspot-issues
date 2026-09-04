#!/usr/bin/env python3
"""
전종목 이름→코드 변환표 생성 (KOSPI + KOSDAQ + KONEX)
- FinanceDataReader 사용 (pykrx는 KRX 로그인 요구로 Actions에서 불안정)
- fdr.StockListing('KRX') → GitHub 데이터 캐시 경유라 로그인 없이 동작
- 결과: tickers.json  { updated, count, map: {종목명: 코드} }
"""
import json
import sys
from datetime import datetime

try:
    import FinanceDataReader as fdr
except ImportError:
    print("FinanceDataReader가 필요합니다: pip install finance-datareader", file=sys.stderr)
    sys.exit(1)


def build():
    try:
        df = fdr.StockListing("KRX")   # 코스피+코스닥+코넥스 전종목
    except Exception as e:
        print(f"StockListing 실패: {e}", file=sys.stderr)
        sys.exit(1)

    # 컬럼명이 버전에 따라 다를 수 있어 유연하게 탐색
    cols = {c.lower(): c for c in df.columns}
    code_col = cols.get("code") or cols.get("symbol")
    name_col = cols.get("name")
    if not code_col or not name_col:
        print(f"코드/이름 컬럼을 찾지 못했습니다. 컬럼: {list(df.columns)}", file=sys.stderr)
        sys.exit(1)

    name_to_code = {}
    for _, row in df.iterrows():
        code = str(row[code_col]).strip().zfill(6)
        name = str(row[name_col]).strip()
        if not name or name == "nan" or not code.isdigit():
            continue
        name_to_code[name] = code

    if not name_to_code:
        print("종목을 하나도 가져오지 못했습니다.", file=sys.stderr)
        sys.exit(1)

    out = {
        "updated": datetime.now().isoformat(timespec="seconds"),
        "count": len(name_to_code),
        "map": name_to_code,
    }
    with open("tickers.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"tickers.json 생성 완료: {len(name_to_code)}종목")


if __name__ == "__main__":
    build()
