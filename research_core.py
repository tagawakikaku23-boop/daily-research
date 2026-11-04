"""Separate reporting primitives; no network or portfolio access on import."""
import ast
import hashlib
import html
import json
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

JST = timezone(timedelta(hours=9))


def legacy_constant(name):
    # Reuse existing feed/discovery configuration WITHOUT importing daily_report:
    # that module reads holdings and credentials at import time.
    if name not in {"NEWS_FEEDS", "DISCOVERY_POOL", "INDICES"}:
        raise ValueError("Unsupported constant")
    tree = ast.parse(Path(__file__).with_name("daily_report.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError("Missing legacy configuration")


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
        except (ValueError, TypeError, OverflowError):
            return None
    return dt if dt.tzinfo else None


def canonical_url(value):
    try:
        u = urlsplit(value)
        if u.scheme not in {"https", "http"} or not u.hostname or u.username or u.password:
            return None
        if u.port not in {None, 80, 443}:
            return None
        query = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True)
                 if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
        return urlunsplit((u.scheme.lower(), u.netloc.lower(), u.path, urlencode(sorted(query)), ""))
    except (ValueError, TypeError):
        return None


def clean(value, limit=700):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", "", str(value or "")))).strip()[:limit]


def article(entry, category, source, now, max_days=3):
    published = timestamp(entry.get("published") or entry.get("dc_date") or "")
    url = canonical_url(entry.get("link", ""))
    title = clean(entry.get("title"), 240)
    if not published or not url or not title:
        return None
    if not timedelta(0) <= now - published <= timedelta(days=max_days):
        return None
    aggregator = urlsplit(url).hostname == "news.google.com"
    # Aggregator URLs are never mislabeled as original publisher URLs.
    source_name = clean((entry.get("source") or {}).get("title") or source, 100)
    return {"title": title, "url": url, "original_url": None if aggregator else url,
            "url_kind": "検索経由・原文URL未確認" if aggregator else "配信元記事URL",
            "source": source_name, "category": category,
            "published_at": published.isoformat(), "retrieved_at": now.isoformat(),
            "excerpt": clean(entry.get("summary") or entry.get("description")),
            "id": hashlib.sha256(url.encode()).hexdigest()}


def scheduled_slot(stream, now, requested_date=None):
    if stream not in {"news", "investment"} or now.tzinfo is None:
        raise ValueError("Invalid stream or timezone")
    now = now.astimezone(JST)
    if requested_date:
        day = datetime.strptime(requested_date, "%Y-%m-%d").date()
    else:
        day = now.date()
    slot = datetime.combine(day, datetime.min.time(), JST).replace(hour=7, minute=30 if stream == "investment" else 0)
    if not requested_date and slot > now:
        slot -= timedelta(days=1)
    if stream == "investment" and slot.weekday() >= 5:
        if requested_date:
            raise ValueError("Investment report date must be a weekday")
        while slot.weekday() >= 5:
            slot -= timedelta(days=1)
    if slot > now or now - slot > timedelta(hours=48):
        raise ValueError("Outside delivery window; explicit review required")
    return slot


def rich(text):
    text = str(text)
    return [{"type": "text", "text": {"content": text[i:i+1800]}} for i in range(0, len(text), 1800)]


def block(text, kind="paragraph"):
    return {"object": "block", "type": kind, kind: {"rich_text": rich(text)}}


def block_text(b):
    kind = b["type"]
    return "".join(r.get("plain_text", r.get("text", {}).get("content", "")) for r in b[kind].get("rich_text", []))


def signature(blocks):
    # Reports deliberately have only flat text and code blocks.
    return hashlib.sha256(json.dumps([(b["type"], block_text(b)) for b in blocks],
                                    ensure_ascii=False).encode()).hexdigest()


def manifest_block(data):
    return {"object": "block", "type": "code",
            "code": {"language": "json", "rich_text": rich(json.dumps(data, ensure_ascii=False, separators=(",", ":")))}}


def read_manifest(blocks):
    found = []
    for b in blocks:
        if b.get("type") == "code":
            try:
                obj = json.loads(block_text(b))
            except (ValueError, KeyError):
                continue
            if obj.get("format") == "daily-research-v2":
                found.append(obj)
    if len(found) != 1:
        raise ValueError("Report manifest missing or ambiguous")
    result = found[0]
    body = [b for b in blocks if b.get("type") != "code"]
    if signature(body) != result.get("body_hash"):
        raise ValueError("Stored report content does not match manifest")
    return result


def complete_blocks(body, manifest):
    if any(b["type"] == "code" for b in body):
        raise ValueError("Reserved manifest block")
    result = body + [manifest_block(dict(manifest, format="daily-research-v2", body_hash=signature(body)))]
    if len(result) > 100 or len(json.dumps(result).encode()) > 450000:
        raise ValueError("Report exceeds atomic publication limit")
    return result


TIP_TOPICS = [
    ("PER", "株価÷1株利益。低PERでも利益が景気の山なら割安とは限らない。実績と予想を区別する。"),
    ("PBR", "株価÷1株純資産。1倍割れだけで判断せず、資産の質と収益性を見る。"),
    ("ROE", "自己資本に対してどれだけ純利益を生んだか。借入増でも上がるため財務と合わせて読む。"),
    ("ROA", "総資産に対する利益の割合。利益の定義がサイトにより違うため比較条件を合わせる。"),
    ("配当性向", "配当総額÷純利益。赤字や一時利益の年には単純な比率評価を避ける。"),
    ("総還元性向", "配当と自社株買いを利益と比較する。単年度の大型買戻しを継続前提にしない。"),
    ("フリーキャッシュフロー", "営業活動で得た資金から設備投資等を差し引く考え方。定義と投資周期を確認する。"),
    ("自社株買い", "発行株数の減少は1株指標に影響する。取得枠の発表と実際の取得は別に確認する。"),
    ("配当利回り", "1株配当÷株価。予想・実績、特別配当、株式分割の調整を揃えて比較する。"),
    ("配当成長", "増配年数だけでなく利益と現金収入の成長を確認する。無理な還元は持続しにくい。"),
    ("インカムと値上がり益", "配当と売却益を合わせた総収益で比較する。配当だけでは資産全体の成果は分からない。"),
    ("金利と株価", "割引率の上昇は将来利益の現在価値を押し下げ得る。業種・負債構成で影響は違う。"),
    ("為替と投信", "為替ヘッジなし海外資産は現地価格と為替の両方で円評価額が変わる。"),
    ("分散投資", "商品数より中身の重複を見る。S&P500と全世界株には米国大型株の重なりがある。"),
    ("評価倍率", "PERやPBRは成長・リスク・資本効率を反映する。同業他社と過去水準を比較する。"),
    ("営業キャッシュフロー", "利益が出ていても売掛金や在庫増で現金は減り得る。数年の流れを見る。"),
    ("自己資本比率", "自己資本÷総資産。金融業は構造が違い、製造業の基準をそのまま適用しない。"),
    ("ネット有利子負債", "有利子負債から現預金等を引く。使用制限のある現金やリース負債にも注意する。"),
    ("特別配当", "一時的な配当を恒常配当と区別する。翌年の利回りにそのまま使わない。"),
    ("株式分割", "株価・株数・1株利益・配当を同じ分割基準に揃えないと異常な利回りになる。"),
    ("景気循環", "資源・海運などでは好況期にPERが低く見えることがある。平常時利益を考える。"),
    ("営業利益率", "売上に対する営業利益の比率。値上げ・数量・原価の変化を分けて読む。"),
    ("売上成長", "増収でも利益率低下や買収だけの成長かもしれない。既存事業の変化を確認する。"),
    ("減損", "資産価値の見直しが利益を押し下げる。現金流出時期とは異なるが経済的損失を無視しない。"),
    ("権利落ち", "配当権利を失う日には価格調整が起き得る。配当取りだけで利益は保証されない。"),
    ("ETFの分配", "ETFの分配金と企業の配当は仕組みが異なる。組入れ・費用・通貨も確認する。"),
    ("為替ヘッジ", "為替変動を抑える仕組みにはコストがある。ヘッジ有無を投信名だけで推測しない。"),
    ("リバランス", "目標配分への調整。売却だけでなく積立先の変更でも偏りを抑えられる。"),
    ("取得単価", "取得通貨・手数料・分割調整を確認する。円の取得額とドル株価は直接比較できない。"),
    ("情報の基準日", "取得時刻と価格・決算の基準日は別。今日取得しても元データが古い場合がある。"),
    ("予想の修正", "会社予想と市場予想を区別する。上方修正でも株価が期待済みなら下がることがある。"),
    ("集中リスク", "業種・通貨・地域・共通顧客まで見る。保有確認日が古いと比率計算も不確かになる。"),
]


def choose_tip(history, now):
    cutoff = now - timedelta(days=30)
    recent = {h.get("tip_id") for h in history
              if timestamp(h.get("retrieved_at")) and timestamp(h["retrieved_at"]) >= cutoff}
    start = now.date().toordinal() % len(TIP_TOPICS)
    for offset in range(len(TIP_TOPICS)):
        i = (start + offset) % len(TIP_TOPICS)
        if str(i) not in recent:
            title, text = TIP_TOPICS[i]
            return {"id": str(i), "title": title, "text": text}
    return {"id": None, "title": "復習", "text": "直近の学習項目を確認。新しいテーマは次回更新。"}

