#!/usr/bin/env python3
"""
전종목 이름→코드 변환표 생성 (KOSPI + KOSDAQ + KONEX)
- 1차: FinanceDataReader (fdr.StockListing('KRX'))
- 2차 폴백: pykrx (1차가 404 등으로 실패/빈 결과일 때 자동 전환)
- 둘 다 실패할 때만 에러 종료
- 결과: tickers.json  { updated, count, source, map: {종목명: 코드} }
"""
import json
import sys
from datetime import datetime


def build_via_fdr():
    """FinanceDataReader로 {종목명: 코드} 생성. 실패 시 예외 전파."""
    import FinanceDataReader as fdr
    df = fdr.StockListing("KRX")   # 코스피+코스닥+코넥스 전종목
    cols = {c.lower(): c for c in df.columns}
    code_col = cols.get("code") or cols.get("symbol")
    name_col = cols.get("name")
    if not code_col or not name_col:
        raise RuntimeError(f"코드/이름 컬럼을 찾지 못했습니다. 컬럼: {list(df.columns)}")
    m = {}
    for _, row in df.iterrows():
        code = str(row[code_col]).strip().zfill(6)
        name = str(row[name_col]).strip()
        if not name or name == "nan" or not code.isdigit():
            continue
        m[name] = code
    return m


def build_via_pykrx():
    """pykrx로 {종목명: 코드} 생성 (폴백). 실패 시 예외 전파."""
    from pykrx import stock
    today = datetime.now().strftime("%Y%m%d")
    m = {}
    for market in ("KOSPI", "KOSDAQ", "KONEX"):
        try:
            tickers = stock.get_market_ticker_list(today, market=market)
        except Exception:
            # 장 시작 전/휴일이면 날짜 인자 없이 최근 영업일로 재시도
            tickers = stock.get_market_ticker_list(market=market)
        for code in tickers:
            try:
                name = stock.get_market_ticker_name(code)
            except Exception:
                continue
            code = str(code).strip().zfill(6)
            name = (name or "").strip()
            if not name or not code.isdigit():
                continue
            m[name] = code
    return m


def build():
    name_to_code, source = {}, None

    # 1차: FinanceDataReader
    try:
        name_to_code = build_via_fdr()
        source = "fdr"
    except Exception as e:
        print(f"[1차] FinanceDataReader 실패: {e}", file=sys.stderr)

    # 2차: pykrx 폴백
    if not name_to_code:
        try:
            name_to_code = build_via_pykrx()
            source = "pykrx"
        except Exception as e:
            print(f"[2차] pykrx 폴백 실패: {e}", file=sys.stderr)

    if not name_to_code:
        print("두 소스(FDR·pykrx) 모두 실패. 종목을 가져오지 못했습니다.", file=sys.stderr)
        sys.exit(1)

    out = {
        "updated": datetime.now().isoformat(timespec="seconds"),
        "count": len(name_to_code),
        "source": source,
        "map": name_to_code,
    }
    with open("tickers.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"tickers.json 생성 완료: {len(name_to_code)}종목 (source={source})")


if __name__ == "__main__":
    build()
