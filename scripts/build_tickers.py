#!/usr/bin/env python3
"""
전종목 이름→코드 변환표 생성 (KOSPI + KOSDAQ)
- pykrx로 상장 종목을 받아 tickers.json 으로 저장
- 하루 1회 정도 갱신하면 충분 (신규 상장/상장폐지 반영)
- 오매칭 방지를 위해 3글자 미만 종목명은 '코드 동반일 때만' 인정하도록
  collect.py 에서 처리하므로 여기서는 전부 저장한다.
"""
import json
import sys
from datetime import datetime

try:
    from pykrx import stock
except ImportError:
    print("pykrx가 필요합니다: pip install pykrx", file=sys.stderr)
    sys.exit(1)


def build():
    name_to_code = {}
    today = datetime.now().strftime("%Y%m%d")

    for market in ("KOSPI", "KOSDAQ"):
        try:
            tickers = stock.get_market_ticker_list(today, market=market)
        except Exception:
            # 장 시작 전/휴일이면 날짜 인자 없이 최근 영업일로 시도
            tickers = stock.get_market_ticker_list(market=market)
        for code in tickers:
            try:
                name = stock.get_market_ticker_name(code)
            except Exception:
                continue
            if not name:
                continue
            # 우선주/스팩 등도 그대로 저장 (이름이 유니크하므로)
            name_to_code[name] = code

    if not name_to_code:
        print("종목을 가져오지 못했습니다. KRX 응답 확인 필요.", file=sys.stderr)
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
