"""Pure investment calculations. No credentials, I/O, or portfolio defaults."""
import math
import re
import unicodedata
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))


def number(value, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= minimum else None


def require_fx(value):
    result = number(value)
    if result is None or result <= 0:
        raise ValueError("為替が未確認のため円換算を停止しました")
    return result


def convert_jpy(value, currency, fx=None):
    value = number(value)
    if value is None:
        raise ValueError("金額が不正です")
    if currency == "JPY":
        return value
    if currency == "USD":
        return value * require_fx(fx)
    raise ValueError("未対応または未確認の通貨です")


def annual_dividend(shares, per_share, currency, fx=None):
    shares, per_share = number(shares), number(per_share)
    if shares is None or per_share is None:
        raise ValueError("株数または一株年間配当が未確認です")
    return convert_jpy(shares * per_share, currency, fx)


def yield_percent(per_share, price):
    per_share, price = number(per_share), number(price)
    if per_share is None or price is None or price <= 0:
        return None
    result = per_share / price * 100
    # Outliers require source review; do not silently rescale them.
    return result if result <= 30 else None


def gain_percent(price, price_currency, cost, cost_currency, fx=None):
    cost = number(cost)
    if cost is None or cost == 0:
        return None
    try:
        current = convert_jpy(price, price_currency, fx)
        basis = convert_jpy(cost, cost_currency, fx)
    except ValueError:
        return None
    return (current / basis - 1) * 100


def company_matches(title, aliases):
    """Only explicit verified aliases; no automatically shortened company names."""
    normalize = lambda s: unicodedata.normalize("NFKC", s).casefold()
    text = normalize(title)
    for alias in aliases:
        name = normalize(alias.strip())
        if len(name) < 3:
            continue
        if name.isascii():
            if re.search(r"(?<![a-z0-9])" + re.escape(name) + r"(?![a-z0-9])", text):
                return True
        elif name in text:
            return True
    return False


def fresh(asof, now, days):
    try:
        stamp = datetime.fromisoformat(asof)
        if stamp.tzinfo is None:
            return False
        age = now - stamp
        return timedelta(0) <= age <= timedelta(days=days)
    except (TypeError, ValueError):
        return False


def candidate(row, now):
    """Conservative common-stock gate. Special asset classes need separate rules."""
    missing = []
    if row.get("asset_type") != "common_stock":
        missing.append("資産種別の専用分析が必要")
    for field, days in (("price_asof", 7), ("financial_asof", 400)):
        if not fresh(row.get(field), now, days):
            missing.append("基準日未確認または期限切れ: " + field)
    if not row.get("source_url") or not row.get("sector"):
        missing.append("出典・業種未確認")
    for field in ("price", "annual_dps", "payout_ratio_pct", "net_income", "operating_cf", "free_cf", "max_price", "target_yield_pct"):
        if number(row.get(field)) is None:
            missing.append("未確認: " + field)
    if type(row.get("dividend_cut_5y")) is not bool:
        missing.append("減配履歴未確認")
    if type(row.get("sector_room")) is not bool:
        missing.append("業種配分未確認")
    if row.get("currency") not in {"JPY", "USD"}:
        missing.append("通貨未確認")
    if row.get("dividend_currency") != row.get("currency"):
        missing.append("価格と配当の通貨が不一致")
    if row.get("dividend_basis") != "forward_annual" or not fresh(row.get("dividend_asof"), now, 400):
        missing.append("予想年間配当の基準日・期間が未確認")
    if missing:
        return {"eligible": False, "reasons": missing, "yield_pct": None}
    dy = yield_percent(row["annual_dps"], row["price"])
    checks = [
        (dy is not None and dy >= row["target_yield_pct"] > 0, "希望利回り未達または異常値"),
        (0 < row["payout_ratio_pct"] <= 70, "配当性向を要確認"),
        (all(row[k] > 0 for k in ("net_income", "operating_cf", "free_cf")), "利益・CFを要確認"),
        (not row["dividend_cut_5y"], "過去5年に減配あり"),
        (row["sector_room"], "業種配分上限"),
        (0 < row["price"] <= row["max_price"], "購入上限価格超過"),
    ]
    reasons = [reason for ok, reason in checks if not ok]
    return {"eligible": not reasons, "reasons": reasons or ["設定条件を充足・一次資料の最終確認が必要"], "yield_pct": dy}


def weekly_key(now):
    if now.tzinfo is None:
        raise ValueError("Timezone required")
    year, week, _ = now.astimezone(JST).isocalendar()
    return f"investment-home:{year}-W{week:02d}"


def dashboard(rows, goal, now, fx=None, fx_asof=None):
    if now.tzinfo is None:
        raise ValueError("Timezone required")
    total, count, held = 0., 0, 0
    alerts, eligible = [], []
    valid_fx = fx if fresh(fx_asof, now, 7) else None
    for row in rows:
        name = row.get("name", "名称未設定")
        if row.get("kind") == "holding":
            held += 1
            try:
                if not fresh(row.get("dividend_asof"), now, 400) or row.get("dividend_basis") != "forward_annual":
                    raise ValueError("年間配当の期間・基準日が未確認です")
                total += annual_dividend(row.get("shares"), row.get("annual_dps"), row.get("dividend_currency"), valid_fx)
                count += 1
            except ValueError as error:
                alerts.append(f"{name}: {error}")
        result = candidate(row, now)
        if result["eligible"]:
            eligible.append({"name": name, "sector": row["sector"], **result})
        else:
            alerts.append(f"{name}: " + " / ".join(result["reasons"]))
    # One company per sector in TOP3. Alphabetic tie-break makes reruns stable.
    picks, sectors = [], set()
    for row in sorted(eligible, key=lambda r: (-r["yield_pct"], r["name"])):
        if row["sector"] not in sectors:
            picks.append(row)
            sectors.add(row["sector"])
        if len(picks) == 3:
            break
    complete = held > 0 and count == held
    goal = number(goal)
    return {"key": weekly_key(now), "annual_dividend_jpy": total if complete else None,
            "confirmed_subtotal_jpy": total, "coverage": f"{count}/{held}",
            "goal_progress_pct": total / goal * 100 if complete and goal and goal > 0 else None,
            "top3": picks, "alerts": alerts}
