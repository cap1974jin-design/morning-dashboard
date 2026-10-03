"""朝刊マーケットダッシュボード - データ取得モジュール"""

from __future__ import annotations

import calendar
import json
import logging
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.request import Request, urlopen

import feedparser
import yfinance as yf

logger = logging.getLogger("morning_dashboard")

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(exist_ok=True)

JST = timezone(timedelta(hours=9))

# --- 銘柄定義 ---
# TOPIX と TOPIX先物は Yahoo Finance（米国版）に銘柄が無い。
# TOPIX は Yahoo!ファイナンス日本版から取る（YAHOO_JP_SYMBOLS）。TOPIX先物は無料で安定した取得元が無いため載せない。
MARKET_SYMBOLS = {
    "株価指数": {
        "日経225": "^N225",
        "日経225先物": "NIY=F",
        "TOPIX": "998405.T",
        "S&P500": "^GSPC",
        "NASDAQ": "^IXIC",
        "ダウ平均": "^DJI",
    },
    "為替": {
        "USD/JPY": "USDJPY=X",
        "EUR/JPY": "EURJPY=X",
        "GBP/JPY": "GBPJPY=X",
    },
    "金利・リスク": {
        "米10年債利回り": "^TNX",
        "VIX": "^VIX",
    },
    "暗号資産": {
        "BTC/USD": "BTC-USD",
        "ETH/USD": "ETH-USD",
    },
    "コモディティ": {
        "金 (Gold)": "GC=F",
        "原油 (WTI)": "CL=F",
    },
}

YAHOO_JP_SYMBOLS = {"998405.T"}

NOTES = {
    "日経225先物": "CME・円建て",
}

# --- RSSフィード定義 ---
_GN = "https://news.google.com/rss"
_GN_JA = "hl=ja&gl=JP&ceid=JP:ja"
RSS_FEEDS = {
    "マーケット": [
        {"name": "Google News", "url": f"{_GN}/search?q=%E6%97%A5%E7%B5%8C%E5%B9%B3%E5%9D%87+OR+%E6%A0%AA%E5%BC%8F%E5%B8%82%E5%A0%B4+OR+%E7%82%BA%E6%9B%BF+when:1d&{_GN_JA}"},
        {"name": "Google News", "url": f"{_GN}/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRGx6TVdZU0FtcGhHZ0pLVUNnQVAB?{_GN_JA}"},
        {"name": "Google News", "url": f"{_GN}/search?q=%E9%87%91%E5%88%A9+OR+%E6%97%A5%E9%8A%80+OR+FRB+when:1d&{_GN_JA}"},
    ],
    "国内・政治経済": [
        {"name": "朝日新聞", "url": "https://www.asahi.com/rss/asahi/newsheadlines.rdf"},
        {"name": "時事通信", "url": "https://www.jiji.com/rss/ranking.rdf"},
        {"name": "東洋経済", "url": "https://toyokeizai.net/list/feed/rss"},
        {"name": "ダイヤモンド", "url": "https://diamond.jp/list/feed/rss/dol"},
        {"name": "Google News", "url": f"{_GN}/topics/CAAqIQgKIhtDQkFTRGdvSUwyMHZNRE5mTTJRU0FtcGhLQUFQAQ?{_GN_JA}"},
        {"name": "Yahoo経済", "url": "https://news.yahoo.co.jp/rss/topics/business.xml"},
    ],
    "国際・地政学": [
        {"name": "BBC日本語", "url": "https://feeds.bbci.co.uk/japanese/rss.xml"},
        {"name": "CNN", "url": "http://feeds.cnn.co.jp/rss/cnn/cnn.rdf"},
        {"name": "Google News", "url": f"{_GN}/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRGx1YlY4U0FtcGhHZ0pLVUNnQVAB?{_GN_JA}"},
        {"name": "Yahoo国際", "url": "https://news.yahoo.co.jp/rss/topics/world.xml"},
    ],
    "テック": [
        {"name": "ITmedia", "url": "https://rss.itmedia.co.jp/rss/2.0/itmedia_all.xml"},
        {"name": "GIGAZINE", "url": "https://gigazine.net/news/rss_2.0/"},
        {"name": "Impress Watch", "url": "https://www.watch.impress.co.jp/data/rss/1.0/ipw/feed.rdf"},
        {"name": "Yahoo IT", "url": "https://news.yahoo.co.jp/rss/topics/it.xml"},
    ],
}

# 朝刊に要らない記事（生活・買い物・個別銘柄ページ・入門講座など）を見出しの言葉で外す
NOISE_PATTERNS = [
    "株価・株式情報", "購入レビュー", "実食レビュー", "買ってよかった", "キャンペーンまとめ",
    "ニュースな本", "| ライフ |", "| キャリア・教育 |", "【ワークマン】", "って何？", "を理解する",
]

# 会員にならないと読めない媒体は載せない（2026-10-03 本人指示）
# Google ニュース経由で入ってくる分は媒体名で外す。記事ごとに有料・無料が混ざる媒体（朝日・東洋経済・ダイヤモンド等）は対象外
EXCLUDED_SOURCES = {
    "日本経済新聞", "日経xTECH", "日経ビジネス", "日経クロストレンド", "日経クロステック", "日経BP",
    "Bloomberg", "Bloomberg.com", "bloomberg.com", "ブルームバーグ",
    "ウォール・ストリート・ジャーナル", "WSJ", "The Wall Street Journal", "Financial Times", "FT",
    "NewsPicks", "週刊東洋経済プラス", "日刊工業新聞",
}

MAX_NEWS_PER_FEED = 12
MAX_NEWS_PER_CATEGORY = 20
MAX_AGE_HOURS = 48
# 見出しの文字2つ組の重なり。0.40/0.15 は 2026-10-03 の実データで、同じ出来事の7組がまとまり
# 別件の情報漏えい同士（0.36）はまとまらない境目として決めた
SAME_STORY_OVERLAP = 0.40
SAME_STORY_JACCARD = 0.15
HISTORY_DAYS = 22  # 推移グラフ用（約1か月の営業日）


def _fetch_yahoo_jp(symbol: str) -> Optional[dict]:
    """Yahoo!ファイナンス日本版のページに埋め込まれた現在値・前日比を読む（履歴は取らない）"""
    req = Request(f"https://finance.yahoo.co.jp/quote/{symbol}", headers={"User-Agent": "Mozilla/5.0"})
    html = urlopen(req, timeout=20).read().decode("utf-8", errors="ignore")
    m = re.search(
        r'"code":"%s".*?"price":"([\d,.]+)".*?"changePrice":"([-+\d,.]+)","changePriceRate":"([-+\d,.]+)"'
        % re.escape(symbol),
        html,
        re.S,
    )
    if not m:
        return None
    num = lambda x: float(x.replace(",", ""))
    return {"price": num(m.group(1)), "change": num(m.group(2)), "change_pct": num(m.group(3)), "history": []}


def fetch_market_data() -> dict:
    """全銘柄の現在値・前日比・変動率・直近の終値推移を取得"""
    results = {}
    for category, symbols in MARKET_SYMBOLS.items():
        for name, symbol in symbols.items():
            try:
                if symbol in YAHOO_JP_SYMBOLS:
                    item = _fetch_yahoo_jp(symbol)
                    if item is None:
                        logger.warning(f"{name} ({symbol}): ページから値を読めず")
                        continue
                else:
                    hist = yf.Ticker(symbol).history(period="2mo")
                    if len(hist) < 2:
                        logger.warning(f"{name} ({symbol}): データ不足")
                        continue
                    closes = [float(c) for c in hist["Close"].tolist()]
                    current, previous = closes[-1], closes[-2]
                    change = current - previous
                    item = {
                        "price": current,
                        "change": change,
                        "change_pct": change / previous * 100,
                        "history": [round(c, 4) for c in closes[-HISTORY_DAYS:]],
                    }
                results.setdefault(category, {})[name] = {
                    "symbol": symbol,
                    "price": round(item["price"], 2),
                    "change": round(item["change"], 2),
                    "change_pct": round(item["change_pct"], 2),
                    "history": item["history"],
                    "note": NOTES.get(name, ""),
                }
            except Exception as e:
                logger.error(f"{name} ({symbol}) 取得失敗: {e}")
    return results


def _entry_to_article(entry, feed_name: str) -> Optional[dict]:
    title = entry.get("title", "").strip()
    if any(p in title for p in NOISE_PATTERNS):
        return None
    source = feed_name
    # Google News は見出しの末尾に「 - 媒体名」が付くので、媒体名を出典に回す
    if feed_name == "Google News" and " - " in title:
        title, source = title.rsplit(" - ", 1)
    if source in EXCLUDED_SOURCES:
        return None
    # 東洋経済は「 | 分野 | 東洋経済オンライン」が付く
    title = re.sub(r"\s*\|[^|]*\|\s*東洋経済オンライン$", "", title)
    title = re.sub(r"^\[ITmedia [^\]]+\]\s*", "", title)
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    time_jst = ""
    if parsed:
        time_jst = datetime.fromtimestamp(calendar.timegm(parsed), JST).isoformat(timespec="minutes")
    return {
        "title": title,
        "link": entry.get("link", ""),
        "source": source,
        "published": entry.get("published") or entry.get("updated", ""),
        "time_jst": time_jst,
    }


def _bigrams(title: str) -> set:
    t = unicodedata.normalize("NFKC", title).lower()
    t = re.sub(r"【[^】]*】|\[[^\]]*\]|〈[^〉]*〉", "", t)
    t = re.sub(r"[\s、。・,.!?「」『』():\-―—|…\"'“”‘’<>《》=]", "", t)
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _same_story(a: set, b: set) -> bool:
    if not a or not b:
        return False
    inter = len(a & b)
    return inter / min(len(a), len(b)) >= SAME_STORY_OVERLAP and inter / len(a | b) >= SAME_STORY_JACCARD


def fetch_news() -> dict:
    """RSSフィードからニュースを取得する。

    同じ出来事を報じた記事は1本にまとめ、報じた社数（coverage）が多いものを上に置く。
    社数が同じものは各フィードから交互に拾った順（1つの配信元に偏らない）のまま。
    """
    cutoff = datetime.now(JST) - timedelta(hours=MAX_AGE_HOURS)
    pool = []  # (分野の順番, 交互に拾った順番, 分野, 記事)
    for ci, (category, feeds) in enumerate(RSS_FEEDS.items()):
        per_feed = []
        for feed_info in feeds:
            try:
                feed = feedparser.parse(feed_info["url"])
                articles = (_entry_to_article(e, feed_info["name"]) for e in feed.entries[: MAX_NEWS_PER_FEED + 4])
                per_feed.append([
                    a for a in articles
                    if a and a["title"] and not (a["time_jst"] and datetime.fromisoformat(a["time_jst"]) < cutoff)
                ][:MAX_NEWS_PER_FEED])
            except Exception as e:
                logger.error(f"RSS取得失敗 ({feed_info['name']}): {e}")
        order = 0
        for i in range(MAX_NEWS_PER_FEED):
            for articles in per_feed:
                if i < len(articles):
                    pool.append((ci, order, category, articles[i]))
                    order += 1

    # 分野をまたいで同じ出来事をまとめる（union-find）
    grams = [_bigrams(item[3]["title"]) for item in pool]
    parent = list(range(len(pool)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(pool)):
        for j in range(i + 1, len(pool)):
            if _same_story(grams[i], grams[j]):
                parent[root(j)] = root(i)

    clusters = {}
    for i in range(len(pool)):
        clusters.setdefault(root(i), []).append(pool[i])

    results = {category: [] for category in RSS_FEEDS}
    for members in clusters.values():
        rep = min(members, key=lambda m: (m[0], m[1]))
        votes = {}
        for m in members:
            votes[m[2]] = votes.get(m[2], 0) + 1
        category = max(votes, key=lambda c: (votes[c], -list(RSS_FEEDS).index(c)))
        sources = []
        for m in sorted(members, key=lambda m: (m[0], m[1])):
            if m[3]["source"] not in sources:
                sources.append(m[3]["source"])
        rank = min(m[1] for m in members if m[2] == category)
        results[category].append((len(sources), rank, {**rep[3], "sources": sources, "coverage": len(sources)}))

    for category, items in results.items():
        items.sort(key=lambda x: (-x[0], x[1]))
        results[category] = [item for _, _, item in items[:MAX_NEWS_PER_CATEGORY]]
    return results


def fetch_all() -> dict:
    """全データを取得してJSONに保存"""
    logger.info("データ取得開始...")

    market = fetch_market_data()
    news = fetch_news()

    now = datetime.now(JST)
    data = {
        "fetched_at": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "market": market,
        "news": news,
    }

    filepath = DATA_DIR / f"morning_{now.strftime('%Y-%m-%d')}.json"
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    logger.info(f"データ保存完了: {filepath}")
    return data


def load_latest() -> Optional[dict]:
    """最新のキャッシュJSONを読み込み"""
    files = sorted(DATA_DIR.glob("morning_*.json"), reverse=True)
    if not files:
        return None

    with open(files[0], "r", encoding="utf-8") as f:
        return json.load(f)
