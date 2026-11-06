import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path
import re
import unicodedata

ASSET_COLUMNS = {
    "日付": "日付",
    "合計(円)": "合計",
    "株式(現物)(円)": "株式ETF",
    "投資信託(円)": "投資信託",
    "年金(円)": "年金",
    "ポイント(円)": "ポイント",
}
REVIEW_COLUMNS = {
    f"{name}(円)" for name in
    ("預金・現金・暗号資産", "債券", "FX", "保険", "不動産", "その他の資産")
}
HOLDING_COLUMNS = ("証券会社", "口座区分", "市場", "コード", "株数",
                   "取得単価", "取得単価通貨", "基準日")


def clean(value):
    return unicodedata.normalize("NFKC", value).strip()


def number(value, *, integer=False):
    value = clean(value)
    if not re.fullmatch(r"-?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?", value):
        raise ValueError("数値欄が空欄または形式外")
    amount = Decimal(value.replace(",", ""))
    if integer and amount != amount.to_integral_value():
        raise ValueError("円の金額に小数")
    return int(amount) if integer else format(amount.normalize(), "f")


def day(value):
    return date.fromisoformat(clean(value).replace("/", "-")).isoformat()


def read_rows(path, delimiter, encoding="utf-8-sig"):
    with path.open(encoding=encoding, newline="") as file:
        reader = csv.reader(file, delimiter=delimiter, strict=True)
        headers = [clean(x) for x in next(reader, [])]
        if not headers or "" in headers or len(headers) != len(set(headers)):
            raise ValueError("ヘッダーが空欄または重複")
        for line, values in enumerate(reader, 2):
            if not values or not any(x.strip() for x in values):
                continue
            yield line, dict(zip(headers, values)) if len(values) == len(headers) else None


def assets(row):
    if not {"日付", "合計(円)"} <= row.keys():
        raise ValueError("日付・合計（円）が必要")
    stamp = day(row["日付"])
    values = {ASSET_COLUMNS[k]: number(v, integer=True)
              for k, v in row.items() if k in ASSET_COLUMNS and k != "日付" and v.strip()}
    if "合計" not in values:
        raise ValueError("合計が空欄")
    return {"key": f"MF|JPY|{stamp}", "date": stamp, "values": values,
            "review_columns": sorted(row.keys() & REVIEW_COLUMNS),
            "unknown_columns": sorted(row.keys() - ASSET_COLUMNS.keys() - REVIEW_COLUMNS)}


def holding(row):
    if set(row) != set(HOLDING_COLUMNS):
        raise ValueError("正規化済みの保有明細ヘッダーが必要")
    broker, account, market, code, qty, cost, currency, stamp = (
        clean(row[k]) for k in HOLDING_COLUMNS
    )
    market, code, currency = market.upper(), code.upper(), currency.upper()
    if (broker not in {"SBI", "楽天"} or account not in
            {"特定", "一般", "NISA成長", "NISAつみたて", "旧NISA", "旧つみたてNISA"}
            or market not in {"JP", "US"} or not re.fullmatch(r"[A-Z0-9][A-Z0-9.-]*", code)
            or currency not in {"JPY", "USD"}):
        raise ValueError("証券会社・口座・市場・銘柄・通貨を確認")
    quantity, price = number(qty), number(cost)
    if Decimal(quantity) <= 0 or Decimal(price) < 0:
        raise ValueError("株数または取得単価を確認")
    return {"key": f"{broker}|{account}|{market}|{code}", "date": day(stamp),
            "values": {"株数": quantity, "取得単価": price, "通貨": currency},
            "security": f"{market}|{code}"}


def preview(incoming, existing, kind):
    old = defaultdict(list)
    for item in existing:
        old[item["key"]].append(item)
    counts = Counter(item["key"] for item in incoming if "key" in item)
    months = Counter(item["date"][:7] for item in incoming if "key" in item)
    existing_months = {item["date"][:7] for item in existing}
    for item in incoming:
        if "key" not in item:
            continue
        key = item["key"]
        matches = old[key]
        if counts[key] > 1 or len(matches) > 1 or item.get("unknown_columns"):
            status, reason = "保留", "キー重複または未対応列"
        elif item.get("review_columns"):
            status, reason = "保留", "既存DBと分類が一致しない列"
        elif not matches:
            if kind == "assets" and (months[item["date"][:7]] > 1 or
                                     item["date"][:7] in existing_months):
                status, reason = "保留", "同月の採用日を確認"
            else:
                status, reason = "追加", ""
        else:
            before = matches[0]
            item["before"] = before
            if item["date"] < before["date"]:
                status, reason = "保留", "既存より古い基準日"
            elif item["date"] == before["date"]:
                status, reason = (("変更なし", "") if item["values"] == before["values"]
                                  else ("保留", "同日で値が異なる"))
            elif item["values"].keys() != before["values"].keys() or (
                    item["values"].get("通貨") != before["values"].get("通貨")):
                status, reason = "保留", "項目または通貨の差異"
            else:
                status, reason = "変更", "基準日更新"
        item.update(status=status, reason=reason)
    return incoming


def group_holdings(rows):
    groups = defaultdict(list)
    for row in rows:
        if row["status"] != "保留":
            groups[row["security"]].append(row)
    result = {}
    for key, members in groups.items():
        days = {row["date"] for row in members}
        quantity = (format(sum(Decimal(row["values"]["株数"]) for row in members), "f")
                    if len(days) == 1 else None)
        result[key] = {"株数": quantity, "基準日": min(days) if len(days) == 1 else None,
                       "対象口座数": len(members)}
    return result


def project_holdings(incoming, existing):
    """Apply proposed changes in memory, retaining accounts absent from the file."""
    projected = {row["key"]: row for row in existing}
    if len(projected) != len(existing):
        raise ValueError("既存保有明細のキーが重複")
    for row in incoming:
        if row["status"] in {"追加", "変更", "変更なし"}:
            projected[row["key"]] = row
    grouped = group_holdings([
        {**row, "security": key.split("|", 2)[2], "status": "変更なし"}
        for key, row in projected.items()
    ])
    for row in incoming:
        if row["status"] == "保留" and row.get("security") in grouped:
            group = grouped[row["security"]]
            group.update({"株数": None, "基準日": None})
    return grouped


def main():
    parser = argparse.ArgumentParser(description="ローカル取込差分確認。Notionへの書込みなし")
    parser.add_argument("kind", choices=("assets", "holdings"))
    parser.add_argument("input", type=Path)
    parser.add_argument("--existing", type=Path, help="正規化済み既存行のJSON配列")
    parser.add_argument("--headers-only", action="store_true", help="先頭行の列名だけ確認")
    parser.add_argument("--encoding", choices=("utf-8-sig", "cp932"), default="utf-8-sig")
    args = parser.parse_args()
    delimiter = "," if args.kind == "assets" else "\t"
    if args.headers_only:
        with args.input.open(encoding=args.encoding, newline="") as file:
            headers = [clean(value) for value in next(csv.reader(file, delimiter=delimiter), [])]
        print(json.dumps({"headers": headers}, ensure_ascii=True, indent=2))
        return
    existing = (json.loads(args.existing.read_text(encoding="utf-8-sig"))
                if args.existing else [])
    convert = assets if args.kind == "assets" else holding
    incoming = []
    for line, raw in read_rows(args.input, delimiter, args.encoding):
        try:
            if raw is None:
                raise ValueError("列数がヘッダーと一致しない")
            incoming.append({"line": line, **convert(raw)})
        except ValueError as error:
            incoming.append({"line": line, "status": "保留", "reason": str(error)})
    output = {"rows": preview(incoming, existing, args.kind),
              "comparison": "既存行と比較" if args.existing else "既存行未指定の仮判定"}
    if args.kind == "holdings":
        grouped = project_holdings(incoming, existing)
        output["反映後の銘柄合算案" if args.existing else "入力分のみの銘柄合算"] = grouped
        output["株価・配当・ニュースの共通取得キー候補"] = sorted(grouped)
    print(json.dumps(output, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
