"""Isolated secondary market provider worker; stdout is JSON only."""
import contextlib
import io
import json
import math
import sys
from datetime import datetime, timezone


def finite(v):
    if isinstance(v, bool):
        return None
    try:
        n = float(v)
        return n if math.isfinite(n) else None
    except (ValueError, TypeError):
        return None


def fetch(ticker):
    import yfinance as yf
    t = yf.Ticker(ticker)
    # Unadjusted closes and provider previous close are labeled, not historical
    # adjusted values mixed with current dividend rates.
    h = t.history(period="1mo", auto_adjust=False, timeout=15)
    if h.empty:
        raise ValueError("No price")
    prices = h["Close"].dropna()
    if prices.empty:
        raise ValueError("No close")
    info = {}
    try:
        info = t.get_info()
    except Exception:
        pass
    stamp = info.get("regularMarketTime")
    price = finite(info.get("regularMarketPrice"))
    # Daily history date is a session date, NOT an exact price timestamp.
    session = str(prices.index[-1].date())
    price_asof = datetime.fromtimestamp(stamp, timezone.utc).isoformat() if stamp and price else None
    if price is None:
        price = finite(prices.iloc[-1])
    prev = finite(info.get("regularMarketPreviousClose"))
    if prev is None and len(prices) > 1:
        prev = finite(prices.iloc[-2])
    result = {"price": price, "price_asof": price_asof, "session_date": session,
              "change_pct": (price / prev - 1) * 100 if price and prev and prev > 0 else None,
              "currency": info.get("currency"), "asset_type": info.get("quoteType"),
              "sector": info.get("sector"), "overview": info.get("longBusinessSummary", "")[:500],
              "provider_note": "Yahoo Finance経由の参考値。遅延あり・一次資料未照合",
              "financial_period": datetime.fromtimestamp(info["lastFiscalYearEnd"], timezone.utc).date().isoformat()
                                  if info.get("lastFiscalYearEnd") else None}
    for key, field in {"per": "trailingPE", "pbr": "priceToBook", "roe": "returnOnEquity",
                       "payout": "payoutRatio", "annual_dps": "dividendRate",
                       "revenue_growth": "revenueGrowth", "earnings_growth": "earningsGrowth",
                       "free_cf": "freeCashflow", "operating_cf": "operatingCashflow",
                       "debt_to_equity": "debtToEquity"}.items():
        result[key] = finite(info.get(field))
    # No inferred dividend basis/as-of or yield rescaling.
    result["dividend_asof"] = None
    result["dividend_basis"] = "提供者の年間配当参考値・予想/実績未照合"
    try:
        cal = t.calendar
        dates = cal.get("Earnings Date", []) if isinstance(cal, dict) else []
        result["earnings_dates"] = [str(d) for d in dates]
    except Exception:
        result["earnings_dates"] = []
    return result


if __name__ == "__main__":
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = fetch(sys.argv[1])
        print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    except Exception:
        raise SystemExit(1)

